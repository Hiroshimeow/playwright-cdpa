from __future__ import annotations

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.models import TurnRecord, TurnState
from playwright_gpt_core.service import ChatGPTCore


@pytest.mark.asyncio
async def test_cancel_before_send_is_proven_without_browser(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.cancel("req-1")
    assert result.state == TurnState.CANCELLED


@pytest.mark.asyncio
async def test_watch_without_exact_identity_fails_closed_without_browser(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.watch("req-1")
    assert result.failure is not None
    assert result.failure.category.value == "identity_missing"


@pytest.mark.asyncio
async def test_duplicate_public_request_id_fails_before_browser_access(tmp_path) -> None:
    from playwright_gpt_core.errors import OwnershipConflictError

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.create(TurnRecord.new(request_id="duplicate", prompt="first"))
    with pytest.raises(OwnershipConflictError):
        await core.send("second", fresh=True, request_id="duplicate")


@pytest.mark.asyncio
async def test_preparing_cancel_blocks_stale_sender_before_click_boundary(tmp_path) -> None:
    from playwright_gpt_core.errors import ConcurrentStateError
    from playwright_gpt_core.models import SendProvenance

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    record = core.store.create(
        TurnRecord.new(
            request_id="preparing",
            prompt="draft",
            target_kind="conversation",
            target_conversation_id="conversation-1",
        )
    )
    core.store.claim_conversation("conversation-1", "preparing")
    preparing = core.store.save(
        record.transition(TurnState.PREPARING), expected_revision=record.revision
    )

    result = await core.cancel("preparing")

    assert result.state == TurnState.CANCELLED
    assert core.store.load_conversation("conversation-1").active_request_id is None
    with pytest.raises(ConcurrentStateError):
        core.store.save(
            preparing.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED),
            expected_revision=preparing.revision,
        )
    assert core.store.load("preparing").send_provenance == SendProvenance.NOT_ATTEMPTED


class _FakeCDPSession:
    def __init__(self, page) -> None:
        self.page = page

    async def send(self, method: str):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.page.target_id, "type": "page"}}

    async def detach(self) -> None:
        return None


class _FakePage:
    def __init__(self, target_id: str, *, goto_error: Exception | None = None) -> None:
        self.target_id = target_id
        self.goto_error = goto_error
        self.closed = False

    async def goto(self, *_args, **_kwargs) -> None:
        if self.goto_error is not None:
            raise self.goto_error

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _FakeContext:
    def __init__(self, page: _FakePage, extra_pages: list[_FakePage] | None = None) -> None:
        self.page = page
        self.pages = [*(extra_pages or []), page]
        self.request = None

    async def new_page(self) -> _FakePage:
        return self.page

    async def new_cdp_session(self, page: _FakePage) -> _FakeCDPSession:
        return _FakeCDPSession(page)


class _FakeBrowserSession:
    context_value = None

    def __init__(self, _config) -> None:
        self.context = None

    async def __aenter__(self):
        self.context = type(self).context_value
        return self

    async def __aexit__(self, *_args) -> None:
        self.context = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_error", "expected_category"),
    [
        pytest.param(
            __import__("playwright.async_api", fromlist=["TimeoutError"]).TimeoutError(
                "navigation timeout"
            ),
            "timeout",
            id="playwright-timeout",
        ),
        pytest.param(
            __import__("playwright.async_api", fromlist=["Error"]).Error(
                "navigation failed"
            ),
            "network",
            id="playwright-error",
        ),
    ],
)
async def test_pre_click_playwright_failure_releases_claim_and_closes_helper(
    tmp_path, monkeypatch, browser_error, expected_category
) -> None:
    import playwright_gpt_core.service as service_module

    page = _FakePage("target-timeout", goto_error=browser_error)
    _FakeBrowserSession.context_value = _FakeContext(page)
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    result = await core.send(
        "prompt",
        conversation="conversation-timeout",
        request_id="req-timeout",
    )

    persisted = core.store.load("req-timeout")
    assert result.state == TurnState.FAILED
    assert result.failure is not None
    assert result.failure.category.value == expected_category
    assert result.failure.external is True
    assert core.store.load_conversation("conversation-timeout").active_request_id is None
    assert persisted.helper_page_target_id == "target-timeout"
    assert persisted.helper_page_closed_at is not None
    assert page.closed is True

    claimed = core.store.claim_conversation("conversation-timeout", "next-request")
    assert claimed.active_request_id == "next-request"


@pytest.mark.asyncio
async def test_frontend_accepted_transient_backend_failure_then_watch_closes_exact_helper(
    tmp_path, monkeypatch
) -> None:
    import playwright_gpt_core.service as service_module
    from playwright_gpt_core.errors import BackendUnavailableError
    from playwright_gpt_core.models import TurnIdentity
    from playwright_gpt_core.monitor import MonitorSnapshot
    from playwright_gpt_core.transport import FrontendAcceptance, FrontendHandoff
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "conversation-recovery"
    helper = _FakePage("owned-helper-target")
    unrelated = _FakePage("unrelated-target")
    context = _FakeContext(helper, [unrelated])
    _FakeBrowserSession.context_value = context

    baseline = graph(
        message("root", "system", None, turn=None, request=None),
        message("old-final", "assistant", "root", text="OLD", turn="old", request="old"),
        current="old-final",
    )
    completed = graph(
        message("root", "system", None, turn=None, request=None),
        message("old-final", "assistant", "root", text="OLD", turn="old", request="old"),
        message(
            "user-new",
            "user",
            "old-final",
            text="prompt",
            turn="graph-turn",
            request="graph-request",
        ),
        message(
            "assistant-new",
            "assistant",
            "user-new",
            text="RECOVERED_OK",
            turn="graph-turn",
            request="graph-request",
        ),
        current="assistant-new",
    )

    class Backend:
        recovering = False
        send_calls = 0

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            if not type(self).recovering:
                type(self).send_calls += 1
                if type(self).send_calls == 1:
                    return MonitorSnapshot("COMPLETE", baseline)
                raise BackendUnavailableError("backend GET failed with HTTP 429")
            return MonitorSnapshot("COMPLETE", completed)

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def fake_send_real(_page, *, send_timeout, on_accepted):
        assert send_timeout > 0
        acceptance = FrontendAcceptance(
            200,
            "transport-request",
            "user-new",
            "old-final",
            None,
            None,
        )
        await on_accepted(acceptance)
        return FrontendHandoff(
            acceptance=acceptance,
            identity=TurnIdentity(
                conversation_id=conversation_id,
                transport_turn_exchange_id="transport-turn",
                stream_topic_id="topic-1",
                transport_request_id="transport-request",
                user_message_id="user-new",
                frontend_parent_message_id="old-final",
            ),
            event_types=("message",),
            response_bytes=10,
        )

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "fill_composer", noop)
    monkeypatch.setattr(service_module, "send_real", fake_send_real)

    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path,
            poll=0.001,
            stable_seconds=0,
            identity_timeout=1,
            timeout=1,
        )
    )
    first = await core.send(
        "prompt",
        conversation=conversation_id,
        request_id="recover-request",
    )

    uncertain = core.store.load("recover-request")
    assert first.state == TurnState.UNKNOWN
    assert first.failure is not None
    assert first.failure.category.value == "backend"
    assert uncertain.helper_page_target_id == "owned-helper-target"
    assert uncertain.helper_page_closed_at is None
    assert helper.closed is False
    assert unrelated.closed is False

    Backend.recovering = True
    recovered = await core.watch("recover-request")

    final = core.store.load("recover-request")
    assert recovered.success is True
    assert recovered.response == "RECOVERED_OK"
    assert final.state == TurnState.COMPLETE
    assert final.helper_page_closed_at is not None
    assert helper.closed is True
    assert unrelated.closed is False
    assert core.store.load_conversation(conversation_id).active_request_id is None


@pytest.mark.asyncio
async def test_recovery_respects_durable_keep_helper_policy(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    helper = _FakePage("kept-target")
    context = _FakeContext(helper)
    record = TurnRecord.new(request_id="kept", prompt="prompt").with_helper_page(
        "kept-target", keep=True
    )
    core.store.save(record)

    await core._close_owned_helper(context, "kept")  # type: ignore[arg-type]

    persisted = core.store.load("kept")
    assert helper.closed is False
    assert persisted.helper_page_closed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["watch", "cancel"])
async def test_terminal_request_retries_exact_helper_cleanup(
    tmp_path, monkeypatch, operation
) -> None:
    import playwright_gpt_core.service as service_module

    helper = _FakePage("terminal-helper")
    unrelated = _FakePage("terminal-unrelated")
    _FakeBrowserSession.context_value = _FakeContext(helper, [unrelated])
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    record = TurnRecord.new(request_id="terminal", prompt="prompt").with_helper_page(
        "terminal-helper", keep=False
    )
    record = core.store.save(record.transition(TurnState.CANCELLED))

    result = await getattr(core, operation)("terminal")

    persisted = core.store.load("terminal")
    assert result.state == TurnState.CANCELLED
    assert persisted.helper_page_closed_at is not None
    assert helper.closed is True
    assert unrelated.closed is False
