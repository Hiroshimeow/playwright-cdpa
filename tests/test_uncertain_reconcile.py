from __future__ import annotations

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.errors import AmbiguousOutcomeError
from playwright_gpt_core.graph import graph_fingerprints
from playwright_gpt_core.models import SendProvenance, TurnIdentity, TurnRecord, TurnState
from playwright_gpt_core.service import ChatGPTCore
from tests.fixtures.graph_factory import graph, message


class Backend:
    def __init__(self, snapshot_graph):
        self.snapshot_graph = snapshot_graph

    async def snapshot(self, conversation_id: str):
        from playwright_gpt_core.monitor import MonitorSnapshot

        return MonitorSnapshot("COMPLETE", self.snapshot_graph)


@pytest.mark.asyncio
async def test_uncertain_existing_send_reconciles_one_graph_delta_without_resend(
    tmp_path,
) -> None:
    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-test",
        )
    )
    baseline = graph(
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final", "assistant", "root", text="old", turn="old-turn", request="old-request"
        ),
        current="old-final",
    )
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final", "assistant", "root", text="old", turn="old-turn", request="old-request"
        ),
        message(
            "new-user",
            "user",
            "old-final",
            text="unknown-body",
            turn="new-turn",
            request="new-request",
        ),
        message(
            "new-final",
            "assistant",
            "new-user",
            text="NEW",
            turn="new-turn",
            request="new-request",
        ),
        current="new-final",
    )
    record = TurnRecord.new(
        request_id="req-1",
        prompt="not persisted",
        target_kind="conversation",
        target_conversation_id="conversation-1",
    ).transition(TurnState.PREPARING)
    record = record.with_identity(
        TurnIdentity(
            conversation_id="conversation-1",
            pre_send_current_node="old-final",
        )
    ).with_baseline(graph_fingerprints(baseline))
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = record.transition(TurnState.UNKNOWN)
    record = core.store.save(record)
    core.coordination.claim("conversation-1", "req-1")
    identity = await core._ensure_monitorable_identity(record, Backend(snapshot))  # type: ignore[arg-type]
    assert identity.user_message_id == "new-user"
    assert identity.turn_exchange_id == "new-turn"
    assert identity.request_id == "new-request"
    assert core.store.load("req-1").state == TurnState.RUNNING


@pytest.mark.asyncio
async def test_uncertain_send_without_graph_anchor_cannot_reconcile(tmp_path) -> None:
    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-test",
        )
    )
    record = TurnRecord.new(request_id="req-1", prompt="not persisted")
    record = record.transition(TurnState.PREPARING)
    record = record.with_identity(TurnIdentity(conversation_id="conversation-1"))
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = core.store.save(record.transition(TurnState.UNKNOWN))
    with pytest.raises(Exception):
        await core._ensure_monitorable_identity(record, Backend({}))  # type: ignore[arg-type]


class _Context:
    pass


class _BrowserSession:
    def __init__(self, _config) -> None:
        self.context = None

    async def __aenter__(self):
        self.context = _Context()
        return self

    async def __aexit__(self, *_args) -> None:
        self.context = None


@pytest.mark.asyncio
@pytest.mark.parametrize(("operation", "drift"), [("watch", "changed"), ("recover", "missing")])
async def test_uncertain_recovery_rejects_changed_or_missing_baseline_without_binding(
    tmp_path, monkeypatch, operation: str, drift: str
) -> None:
    import playwright_gpt_core.service as service_module

    baseline = graph(
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final",
            "assistant",
            "root",
            text="old",
            turn="old-turn",
            request="old-request",
        ),
        current="old-final",
    )
    current_nodes = [message("root", "system", None, turn=None, request=None)]
    if drift == "changed":
        current_nodes.append(
            message(
                "old-final",
                "assistant",
                "root",
                text="changed",
                turn="old-turn",
                request="old-request",
            )
        )
        parent = "old-final"
    else:
        parent = "root"
    current_nodes.extend(
        [
            message(
                "new-user",
                "user",
                parent,
                text="foreign-or-unknown",
                turn="new-turn",
                request="new-request",
            ),
            message(
                "new-final",
                "assistant",
                "new-user",
                text="NEW",
                turn="new-turn",
                request="new-request",
            ),
        ]
    )
    snapshot = graph(*current_nodes, current="new-final")

    class ReconcileBackend(Backend):
        def __init__(self, _context) -> None:
            super().__init__(snapshot)

    monkeypatch.setattr(service_module, "BrowserSession", _BrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", ReconcileBackend)

    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-baseline-test",
            timeout=0.1,
            poll=0.001,
        )
    )
    record = TurnRecord.new(
        request_id="baseline-drift-request",
        prompt="not persisted",
        target_kind="conversation",
        target_conversation_id="conversation-1",
    ).transition(TurnState.PREPARING)
    record = record.with_identity(
        TurnIdentity(
            conversation_id="conversation-1",
            pre_send_current_node="old-final",
        )
    ).with_baseline(graph_fingerprints(baseline))
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = core.store.create(record.transition(TurnState.UNKNOWN))
    claim = core.coordination.claim("conversation-1", record.request_id)

    result = await getattr(core, operation)(record.request_id)

    persisted = core.store.load(record.request_id)
    assert result.failure is not None
    assert result.failure.category.value == "ambiguous_outcome"
    assert persisted.state == TurnState.UNKNOWN
    assert persisted.send_provenance == SendProvenance.RETRY_PROHIBITED
    assert persisted.identity is not None
    assert persisted.identity.user_message_id is None
    assert persisted.identity.turn_exchange_id is None
    assert persisted.helper_page_closed_at is None
    unchanged_claim = core.coordination.load("conversation-1")
    assert unchanged_claim.active_request_id == record.request_id
    assert unchanged_claim.revision == claim.revision


@pytest.mark.asyncio
async def test_uncertain_recovery_rejects_baseline_without_anchor_fingerprint(tmp_path) -> None:
    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-anchor-test",
        )
    )
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("old-final", "assistant", "root", text="old"),
        message("new-user", "user", "old-final", text="unknown"),
        current="new-user",
    )
    record = TurnRecord.new(
        request_id="missing-anchor",
        prompt="not persisted",
        target_kind="conversation",
        target_conversation_id="conversation-1",
    ).transition(TurnState.PREPARING)
    record = record.with_identity(
        TurnIdentity(conversation_id="conversation-1", pre_send_current_node="old-final")
    ).with_baseline({"root": graph_fingerprints(snapshot)["root"]})
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = core.store.create(record.transition(TurnState.UNKNOWN))
    core.coordination.claim("conversation-1", record.request_id)

    with pytest.raises(AmbiguousOutcomeError, match="anchor"):
        await core._ensure_monitorable_identity(record, Backend(snapshot))  # type: ignore[arg-type]

    persisted = core.store.load(record.request_id)
    assert persisted.identity is not None
    assert persisted.identity.user_message_id is None
    assert persisted.state == TurnState.UNKNOWN


class _PublicSnapshotBackend:
    snapshot_graph = None

    def __init__(self, _context) -> None:
        pass

    async def snapshot(self, _conversation_id: str):
        from playwright_gpt_core.monitor import MonitorSnapshot

        return MonitorSnapshot("COMPLETE", type(self).snapshot_graph)


def _uncertain_public_record(core: ChatGPTCore, baseline: dict) -> TurnRecord:
    record = TurnRecord.new(
        request_id="public-uncertain-request",
        prompt="not persisted",
        target_kind="conversation",
        target_conversation_id="conversation-1",
    ).transition(TurnState.PREPARING)
    record = record.with_identity(
        TurnIdentity(
            conversation_id="conversation-1",
            pre_send_current_node="old-final",
        )
    ).with_baseline(graph_fingerprints(baseline))
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    return core.store.create(record.transition(TurnState.UNKNOWN))


def _baseline_graph() -> dict:
    return graph(
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final",
            "assistant",
            "root",
            text="old",
            turn="old-turn",
            request="old-request",
        ),
        current="old-final",
    )


def _ambiguous_post_baseline_graph(kind: str) -> dict:
    nodes = [
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final",
            "assistant",
            "root",
            text="old",
            turn="old-turn",
            request="old-request",
        ),
        message(
            "first-user",
            "user",
            "old-final",
            text="first",
            turn="first-turn",
            request="first-request",
        ),
        message(
            "first-final",
            "assistant",
            "first-user",
            text="FIRST_RESULT",
            turn="first-turn",
            request="first-request",
        ),
    ]
    if kind == "sibling":
        second_parent = "old-final"
    elif kind == "current-chain":
        second_parent = "first-final"
    else:
        raise AssertionError(kind)
    nodes.extend(
        [
            message(
                "second-user",
                "user",
                second_parent,
                text="second",
                turn="second-turn",
                request="second-request",
            ),
            message(
                "second-final",
                "assistant",
                "second-user",
                text="SECOND_RESULT",
                turn="second-turn",
                request="second-request",
            ),
        ]
    )
    return graph(*nodes, current="second-final")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["watch", "recover"])
@pytest.mark.parametrize("kind", ["sibling", "current-chain"])
async def test_public_uncertain_recovery_rejects_multiple_post_baseline_users(
    tmp_path, monkeypatch, operation: str, kind: str
) -> None:
    import playwright_gpt_core.service as service_module

    snapshot = _ambiguous_post_baseline_graph(kind)
    _PublicSnapshotBackend.snapshot_graph = snapshot
    monkeypatch.setattr(service_module, "BrowserSession", _BrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", _PublicSnapshotBackend)

    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-multiple-test",
            timeout=0.05,
            poll=0.001,
            stable_seconds=0,
        )
    )
    record = _uncertain_public_record(core, _baseline_graph())
    claim = core.coordination.claim("conversation-1", record.request_id)

    result = await getattr(core, operation)(record.request_id)

    persisted = core.store.load(record.request_id)
    current_claim = core.coordination.load("conversation-1")
    assert result.state == TurnState.UNKNOWN
    assert result.response is None
    assert result.failure is not None
    assert result.failure.category.value == "graph_ambiguous"
    assert persisted.state == TurnState.UNKNOWN
    assert persisted.send_provenance == SendProvenance.RETRY_PROHIBITED
    assert persisted.identity is not None
    assert persisted.identity.user_message_id is None
    assert persisted.identity.turn_exchange_id is None
    assert persisted.helper_page_closed_at is None
    assert current_claim.active_request_id == record.request_id
    assert current_claim.revision == claim.revision


@pytest.mark.asyncio
async def test_public_uncertain_recovery_accepts_one_unique_post_baseline_user(
    tmp_path, monkeypatch
) -> None:
    import playwright_gpt_core.service as service_module

    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message(
            "old-final",
            "assistant",
            "root",
            text="old",
            turn="old-turn",
            request="old-request",
        ),
        message(
            "only-user",
            "user",
            "old-final",
            text="only",
            turn="only-turn",
            request="only-request",
        ),
        message(
            "only-final",
            "assistant",
            "only-user",
            text="ONLY_RESULT",
            turn="only-turn",
            request="only-request",
        ),
        current="only-final",
    )
    _PublicSnapshotBackend.snapshot_graph = snapshot
    monkeypatch.setattr(service_module, "BrowserSession", _BrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", _PublicSnapshotBackend)

    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="uncertain-unique-test",
            timeout=0.05,
            poll=0.001,
            stable_seconds=0,
        )
    )
    record = _uncertain_public_record(core, _baseline_graph())
    core.coordination.claim("conversation-1", record.request_id)

    result = await core.watch(record.request_id)

    persisted = core.store.load(record.request_id)
    assert result.success is True
    assert result.state == TurnState.COMPLETE
    assert result.response == "ONLY_RESULT"
    assert persisted.identity is not None
    assert persisted.identity.user_message_id == "only-user"
    assert persisted.identity.turn_exchange_id == "only-turn"
    assert core.coordination.load("conversation-1").active_request_id is None
