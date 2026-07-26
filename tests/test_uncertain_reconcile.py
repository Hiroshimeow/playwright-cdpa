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
