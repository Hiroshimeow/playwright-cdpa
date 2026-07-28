from __future__ import annotations

from pathlib import Path

import pytest

from playwright_api.config import ClientConfig
from playwright_api.models import TurnRecord, TurnState
from playwright_api.service import ChatGPTClient
from playwright_api.targets import ChatTarget


def test_relative_default_home_fails_before_store_construction(tmp_path, monkeypatch) -> None:
    from playwright_api.errors import InvalidInputError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", "relative-home")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    with pytest.raises(InvalidInputError, match="home directory must be absolute"):
        ChatGPTClient(ClientConfig(state_dir=Path("local-state")))

    assert not (tmp_path / "local-state").exists()
    assert not (tmp_path / "relative-home").exists()


def test_service_release_uses_atomic_coordination_primitive(tmp_path) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="atomic-release-service",
        )
    )
    calls: list[tuple[str, str, bool]] = []

    class Coordination:
        def release_if_owned(
            self, conversation_id: str, request_id: str, *, terminal: bool
        ) -> None:
            calls.append((conversation_id, request_id, terminal))

        def load(self, _conversation_id: str):
            raise AssertionError("service must not load before atomic release")

        def release(self, *_args, **_kwargs):
            raise AssertionError("service must not use strict release for cleanup")

    core._coordination = Coordination()  # type: ignore[assignment]

    core._release_if_owned("conversation-1", "request-1", terminal=True)

    assert calls == [("conversation-1", "request-1", True)]


@pytest.mark.asyncio
async def test_terminal_release_is_idempotent_and_preserves_immediate_next_owner(
    tmp_path,
) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="terminal-release-race",
        )
    )
    core.coordination.claim("conversation-1", "completed-request")

    core._release_if_owned("conversation-1", "completed-request", terminal=True)
    core.coordination.claim("conversation-1", "next-request")
    core._release_if_owned("conversation-1", "completed-request", terminal=True)

    assert core.coordination.load("conversation-1").active_request_id == "next-request"


@pytest.mark.asyncio
async def test_cancel_before_send_is_proven_without_browser(tmp_path) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.cancel("req-1")
    assert result.state == TurnState.CANCELLED


@pytest.mark.asyncio
async def test_get_without_exact_identity_fails_closed_without_browser(tmp_path) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.get("req-1")
    assert result.failure is not None
    assert result.failure.category.value == "identity_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "get", "status", "cancel"])
@pytest.mark.parametrize("request_id", ["", "../escape", 123])
async def test_public_request_id_validation_precedes_state_coordination_and_browser(
    tmp_path, monkeypatch, operation: str, request_id
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import InvalidInputError

    class ForbiddenBrowserSession:
        def __init__(self, _config) -> None:
            raise AssertionError("invalid request ID must fail before browser construction")

    monkeypatch.setattr(service_module, "BrowserSession", ForbiddenBrowserSession)
    state_dir = tmp_path / "state"
    coordination_dir = tmp_path / "coordination"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=state_dir,
            coordination_dir=coordination_dir,
            deployment_id="request-id-validation",
        )
    )

    with pytest.raises(InvalidInputError, match="request_id"):
        if operation == "send":
            await core.send("prompt", target=ChatTarget.fresh(), request_id=request_id)
        elif operation == "get":
            await core.get(request_id)
        elif operation == "status":
            core.status(request_id)
        else:
            await core.cancel(request_id)

    assert not state_dir.exists()
    assert not coordination_dir.exists()
    assert core._coordination is None


@pytest.mark.asyncio
async def test_duplicate_public_request_id_fails_before_browser_access(tmp_path) -> None:
    from playwright_api.errors import OwnershipConflictError

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    core.store.create(TurnRecord.new(request_id="duplicate", prompt="first"))
    from playwright_api.cli import EXIT_INVARIANT, exit_code
    from playwright_api.models import Result

    with pytest.raises(OwnershipConflictError) as captured:
        await core.send("second", target=ChatTarget.fresh(), request_id="duplicate")

    failure = captured.value.as_failure()
    result = Result(1, "duplicate", TurnState.FAILED, failure=failure)
    assert failure.category.value == "ownership"
    assert result.disposition == "invariant_failure"
    assert exit_code(result, command="send") == EXIT_INVARIANT


@pytest.mark.asyncio
async def test_preparing_cancel_blocks_stale_sender_before_click_boundary(tmp_path) -> None:
    from playwright_api.errors import ConcurrentStateError
    from playwright_api.models import SendProvenance

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    record = core.store.create(
        TurnRecord.new(
            request_id="preparing",
            prompt="draft",
            target_kind="conversation",
            target_conversation_id="conversation-1",
        )
    )
    core.coordination.claim("conversation-1", "preparing")
    preparing = core.store.save(
        record.transition(TurnState.PREPARING), expected_revision=record.revision
    )

    result = await core.cancel("preparing")

    assert result.state == TurnState.CANCELLED
    assert core.coordination.load("conversation-1").active_request_id is None
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
    def __init__(
        self,
        target_id: str,
        *,
        goto_error: Exception | None = None,
        url: str = "about:blank",
    ) -> None:
        self.target_id = target_id
        self.goto_error = goto_error
        self.closed = False
        self.url = url
        self.context = None

    async def goto(self, url: str, *_args, **_kwargs) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed

    def locator(self, _selector: str):
        class Ready:
            @property
            def first(self):
                return self

            async def wait_for(self, *, state: str, timeout: int) -> None:
                assert state == "visible"
                assert timeout > 0

        return Ready()


class _FakeContext:
    def __init__(self, page: _FakePage, extra_pages: list[_FakePage] | None = None) -> None:
        self.page = page
        self.pages = [*(extra_pages or []), page]
        for item in self.pages:
            item.context = self
        self.request = None
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
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


class _AtomicPage(_FakePage):
    def __init__(self, target_id: str, *, url: str) -> None:
        super().__init__(target_id, url=url)
        self.send_clicks = 0

    async def evaluate(self, _script: str, expected: dict[str, object]):
        from urllib.parse import urlparse

        prompt = str(expected["prompt"])
        conversation_id = expected["conversation_id"]
        parsed = urlparse(self.url)
        exact_page = (
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and (
                parsed.path == "/"
                if conversation_id is None
                else parsed.path == f"/c/{conversation_id}"
            )
        )
        if not exact_page:
            return {"ok": False, "reason": "page_identity"}
        assert prompt == "prompt"
        self.send_clicks += 1
        return {"ok": True}


def _persist_running_request(
    core: ChatGPTClient,
    *,
    request_id: str,
    conversation_id: str,
    helper_target_id: str | None = None,
) -> TurnRecord:
    from playwright_api.models import SendProvenance, TurnIdentity

    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
        pre_send_current_node="root",
    )
    record = TurnRecord.new(
        request_id=request_id,
        prompt="prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    if helper_target_id is not None:
        record = record.with_helper_page(helper_target_id, keep=False)
    record = core.store.create(record)
    core.coordination.claim(conversation_id, request_id)
    return record


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
            __import__("playwright.async_api", fromlist=["Error"]).Error("navigation failed"),
            "network",
            id="playwright-error",
        ),
    ],
)
async def test_pre_click_playwright_failure_releases_claim_and_closes_helper(
    tmp_path, monkeypatch, browser_error, expected_category
) -> None:
    import playwright_api.service as service_module

    page = _FakePage("target-timeout", goto_error=browser_error)
    _FakeBrowserSession.context_value = _FakeContext(page)
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    result = await core.send(
        "prompt",
        target=ChatTarget.conversation("conversation-timeout"),
        request_id="req-timeout",
    )

    persisted = core.store.load("req-timeout")
    assert result.state == TurnState.FAILED
    assert result.failure is not None
    assert result.failure.category.value == expected_category
    assert result.failure.external is True
    assert core.coordination.load("conversation-timeout").active_request_id is None
    assert persisted.helper_page_target_id is None
    assert persisted.helper_page_closed_at is None
    assert page.closed is True

    claimed = core.coordination.claim("conversation-timeout", "next-request")
    assert claimed.active_request_id == "next-request"


@pytest.mark.asyncio
async def test_attachment_preflight_failure_releases_claim_without_marking_mutation(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api import AttachmentInput
    from playwright_api.errors import FrontendNotReadyError
    from playwright_api.frontend import FrontendState
    from playwright_api.models import AttachmentStage, SendProvenance
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = "attachment-preflight"
    created = _FakePage("created-target")
    borrowed = _FakePage(
        "borrowed-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    _FakeBrowserSession.context_value = _FakeContext(created, [borrowed])

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def observe(page):
        return FrontendState(
            url=page.url,
            composer_present=True,
            composer_editable=True,
            composer_text="",
            attachment_count=0,
            send_visible=False,
            send_enabled=False,
            stop_visible=False,
            choice_prompt=False,
        )

    async def upload(
        _page,
        _attachments,
        *,
        timeout: float,
        on_upload_boundary,
    ) -> None:
        assert timeout > 0
        assert callable(on_upload_boundary)
        raise FrontendNotReadyError("could not identify one exact ChatGPT file input")

    async def forbidden_fill(*_args, **_kwargs) -> None:
        raise AssertionError("upload preflight failure must precede composer mutation")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "upload_attachments", upload)
    monkeypatch.setattr(service_module, "fill_composer", forbidden_fill)

    path = tmp_path / "evidence.txt"
    path.write_text("evidence", encoding="utf-8")
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="attachment-preflight",
            poll=0.001,
            timeout=0.05,
        )
    )

    result = await core.send(
        "prompt",
        request_id="attachment-preflight",
        target=ChatTarget.conversation(conversation_id),
        attachments=(AttachmentInput.from_path(path),),
    )
    persisted = core.store.load(result.request_id)

    assert result.state is TurnState.FAILED
    assert result.failure is not None
    assert result.failure.category.value == "frontend_not_ready"
    assert persisted.send_provenance is SendProvenance.NOT_ATTEMPTED
    assert persisted.attachment_stage is AttachmentStage.SNAPSHOTTED
    assert core.coordination.load(conversation_id).active_request_id is None
    assert borrowed.closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("page_kind", ["owned", "borrowed"])
@pytest.mark.parametrize("drift", ["text", "attachment"])
async def test_preclick_manual_state_preserves_exact_page(
    tmp_path, monkeypatch, page_kind: str, drift: str
) -> None:
    import playwright_api.service as service_module
    from playwright_api.frontend import FrontendState
    from playwright_api.models import SendProvenance
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = f"manual-{page_kind}-{drift}"
    created = _FakePage("created-target")
    borrowed = _FakePage(
        "borrowed-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    context = _FakeContext(created, [borrowed] if page_kind == "borrowed" else None)
    target = borrowed if page_kind == "borrowed" else created
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    observations = 0

    async def observe(page):
        nonlocal observations
        observations += 1
        if observations == 1:
            return FrontendState(
                url=page.url,
                composer_present=True,
                composer_editable=True,
                composer_text="",
                attachment_count=0,
                send_visible=False,
                send_enabled=False,
                stop_visible=True,
                choice_prompt=False,
            )
        if drift == "text":
            page.manual_text = "manual replacement"
            text = page.manual_text
            attachment_count = 0
        else:
            page.manual_attachment_count = 1
            text = "prompt"
            attachment_count = page.manual_attachment_count
        return FrontendState(
            url=page.url,
            composer_present=True,
            composer_editable=True,
            composer_text=text,
            attachment_count=attachment_count,
            send_visible=False,
            send_enabled=False,
            stop_visible=True,
            choice_prompt=False,
        )

    async def fill(page, prompt: str) -> None:
        assert prompt == "prompt"
        page.automated_text = prompt

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def forbidden_send(*_args, **_kwargs):
        raise AssertionError("manual drift must fail before real Send")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "fill_composer", fill)
    monkeypatch.setattr(service_module, "send_real", forbidden_send)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="manual-state-preservation",
            poll=0.001,
            timeout=0.05,
        )
    )
    result = await core.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
        request_id=f"manual-{page_kind}-{drift}",
    )
    persisted = core.store.load(result.request_id)

    assert result.state == TurnState.FAILED
    assert result.failure is not None
    assert result.failure.category.value == "frontend_not_ready"
    assert persisted.send_provenance == SendProvenance.NOT_ATTEMPTED
    assert target.closed is False
    assert core.coordination.load(conversation_id).active_request_id is None
    if page_kind == "owned":
        assert persisted.helper_page_target_id == target.target_id
        assert persisted.helper_page_keep is True
        assert persisted.helper_page_closed_at is None
    else:
        assert persisted.helper_page_target_id is None
    if drift == "text":
        assert target.manual_text == "manual replacement"
    else:
        assert target.manual_attachment_count == 1

    get_result = await core.get(result.request_id)
    cancel_result = await core.cancel(result.request_id)
    after_terminal_reads = core.store.load(result.request_id)

    assert get_result.state == TurnState.FAILED
    assert cancel_result.state == TurnState.FAILED
    assert target.closed is False
    assert after_terminal_reads.helper_page_closed_at is None
    if page_kind == "owned":
        assert after_terminal_reads.helper_page_keep is True
    if drift == "text":
        assert target.manual_text == "manual replacement"
    else:
        assert target.manual_attachment_count == 1


@pytest.mark.asyncio
async def test_cancel_race_still_durably_preserves_owned_manual_page(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import ConcurrentStateError
    from playwright_api.frontend import FrontendState
    from playwright_api.models import SendProvenance
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = "manual-cancel-race"
    request_id = "manual-cancel-race-request"
    page = _FakePage("created-target")
    context = _FakeContext(page)
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    observations = 0
    send_entries = 0
    cancel_result = None
    inject_retention_conflict = False
    retention_conflicts = 0
    core: ChatGPTClient

    async def observe(active_page):
        nonlocal observations, cancel_result, inject_retention_conflict
        observations += 1
        if observations == 1:
            return FrontendState(
                url=active_page.url,
                composer_present=True,
                composer_editable=True,
                composer_text="",
                attachment_count=0,
                send_visible=False,
                send_enabled=False,
                stop_visible=True,
                choice_prompt=False,
            )
        cancel_result = await core.cancel(request_id)
        inject_retention_conflict = True
        active_page.manual_text = "manual replacement"
        return FrontendState(
            url=active_page.url,
            composer_present=True,
            composer_editable=True,
            composer_text=active_page.manual_text,
            attachment_count=0,
            send_visible=False,
            send_enabled=False,
            stop_visible=True,
            choice_prompt=False,
        )

    async def fill(active_page, prompt: str) -> None:
        assert prompt == "prompt"
        active_page.automated_text = prompt

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def forbidden_send(*_args, **_kwargs):
        nonlocal send_entries
        send_entries += 1
        raise AssertionError("manual cancel race must fail before real Send")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "fill_composer", fill)
    monkeypatch.setattr(service_module, "send_real", forbidden_send)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="manual-cancel-race",
            poll=0.001,
            timeout=0.05,
        )
    )
    original_save = core.store.save

    def save_with_one_retention_conflict(record, *, expected_revision=None):
        nonlocal retention_conflicts
        if inject_retention_conflict and record.helper_page_keep and retention_conflicts == 0:
            retention_conflicts += 1
            raise ConcurrentStateError("injected retention revision conflict")
        return original_save(record, expected_revision=expected_revision)

    monkeypatch.setattr(core.store, "save", save_with_one_retention_conflict)
    send_result = await core.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
        request_id=request_id,
    )
    before_reads = core.store.load(request_id)

    assert cancel_result is not None
    assert cancel_result.state == TurnState.CANCELLED
    assert send_result.state == TurnState.CANCELLED
    assert before_reads.send_provenance == SendProvenance.NOT_ATTEMPTED
    assert before_reads.helper_page_target_id == page.target_id
    assert before_reads.helper_page_keep is True
    assert before_reads.helper_page_closed_at is None
    assert send_entries == 0
    assert retention_conflicts == 1
    assert page.closed is False
    assert page.manual_text == "manual replacement"
    assert core.coordination.load(conversation_id).active_request_id is None

    get_result = await core.get(request_id)
    cancel_again = await core.cancel(request_id)
    after_reads = core.store.load(request_id)

    assert get_result.state == TurnState.CANCELLED
    assert cancel_again.state == TurnState.CANCELLED
    assert after_reads.helper_page_keep is True
    assert after_reads.helper_page_closed_at is None
    assert page.closed is False
    assert page.manual_text == "manual replacement"


@pytest.mark.asyncio
async def test_preclick_cancel_is_one_atomic_save_under_manual_state_race(
    tmp_path, monkeypatch
) -> None:
    import asyncio
    import threading

    import playwright_api.service as service_module
    from playwright_api.frontend import FrontendState
    from playwright_api.models import SendProvenance
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = "atomic-preclick-cancel"
    request_id = "atomic-preclick-cancel-request"
    page = _FakePage("created-target")
    context = _FakeContext(page)
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    async def fill(active_page, prompt: str) -> None:
        assert prompt == "prompt"
        active_page.automated_text = prompt

    async def noop(*_args, **_kwargs) -> None:
        return None

    send_entries = 0

    async def forbidden_send(*_args, **_kwargs):
        nonlocal send_entries
        send_entries += 1
        raise AssertionError("pre-click cancel race must never enter real Send")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "fill_composer", fill)
    monkeypatch.setattr(service_module, "send_real", forbidden_send)

    config = ClientConfig(
        state_dir=tmp_path / "state",
        coordination_dir=tmp_path / "coordination",
        deployment_id="atomic-preclick-cancel",
        poll=0.001,
        timeout=1,
    )
    sender = ChatGPTClient(config)
    canceller = ChatGPTClient(config)
    cancel_saved = threading.Event()
    retention_saved = threading.Event()
    cancel_saves = []

    original_cancel_save = canceller.store.save

    def pause_after_cancel_save(record, *, expected_revision=None):
        saved = original_cancel_save(record, expected_revision=expected_revision)
        cancel_saves.append(saved)
        cancel_saved.set()
        assert retention_saved.wait(2)
        return saved

    monkeypatch.setattr(canceller.store, "save", pause_after_cancel_save)
    original_sender_save = sender.store.save

    def signal_retention_save(record, *, expected_revision=None):
        saved = original_sender_save(record, expected_revision=expected_revision)
        if saved.helper_page_keep:
            retention_saved.set()
        return saved

    monkeypatch.setattr(sender.store, "save", signal_retention_save)
    observations = 0
    cancel_task = None

    async def observe(active_page):
        nonlocal observations, cancel_task
        observations += 1
        if observations == 1:
            return FrontendState(
                url=active_page.url,
                composer_present=True,
                composer_editable=True,
                composer_text="",
                attachment_count=0,
                send_visible=False,
                send_enabled=False,
                stop_visible=True,
                choice_prompt=False,
            )
        cancel_task = asyncio.create_task(
            asyncio.to_thread(lambda: asyncio.run(canceller.cancel(request_id)))
        )
        assert await asyncio.to_thread(cancel_saved.wait, 2)
        active_page.manual_text = "manual replacement"
        return FrontendState(
            url=active_page.url,
            composer_present=True,
            composer_editable=True,
            composer_text=active_page.manual_text,
            attachment_count=0,
            send_visible=False,
            send_enabled=False,
            stop_visible=True,
            choice_prompt=False,
        )

    monkeypatch.setattr(service_module, "observe_frontend", observe)
    send_result = await sender.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
        request_id=request_id,
    )
    assert cancel_task is not None
    cancel_result = await asyncio.wait_for(cancel_task, timeout=2)
    persisted = sender.store.load(request_id)

    assert len(cancel_saves) == 1
    assert cancel_saves[0].state == TurnState.CANCELLED
    assert cancel_saves[0].cancellation_requested_at is not None
    assert cancel_result.state == TurnState.CANCELLED
    assert send_result.state == TurnState.CANCELLED
    assert persisted.state == TurnState.CANCELLED
    assert persisted.cancellation_requested_at is not None
    assert persisted.send_provenance == SendProvenance.NOT_ATTEMPTED
    assert persisted.helper_page_target_id == page.target_id
    assert persisted.helper_page_keep is True
    assert persisted.helper_page_closed_at is None
    assert send_entries == 0
    assert page.closed is False
    assert page.manual_text == "manual replacement"
    assert sender.coordination.load(conversation_id).active_request_id is None

    get_result = await sender.get(request_id)
    cancel_again = await sender.cancel(request_id)

    assert get_result.state == TurnState.CANCELLED
    assert cancel_again.state == TurnState.CANCELLED
    assert page.closed is False
    assert page.manual_text == "manual replacement"


@pytest.mark.asyncio
async def test_atomic_send_rejects_same_target_navigation_after_python_preflight(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.frontend import click_send_atomic
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = "conversation-race"
    page = _AtomicPage(
        "same-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    context = _FakeContext(page)
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    observations = 0

    async def observe(_page):
        nonlocal observations
        from playwright_api.frontend import FrontendState

        observations += 1
        return FrontendState(
            url=page.url,
            composer_present=True,
            composer_editable=True,
            composer_text="" if observations == 1 else "prompt",
            attachment_count=0,
            send_visible=observations > 1,
            send_enabled=observations > 1,
            stop_visible=False,
            choice_prompt=False,
        )

    async def noop(*_args, **_kwargs) -> None:
        return None

    attempted_atomic_send = False

    async def fake_send_real(
        active_page,
        *,
        prompt,
        target,
        expected_attachment_names,
        send_timeout,
        on_accepted,
    ):
        nonlocal attempted_atomic_send
        attempted_atomic_send = True
        assert send_timeout > 0
        assert on_accepted is not None
        active_page.url = "https://chatgpt.com/c/foreign-conversation"
        await click_send_atomic(
            active_page,
            prompt,
            target=target,
            expected_attachment_names=expected_attachment_names,
        )
        raise AssertionError("wrong-conversation Send must not click")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "fill_composer", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "send_real", fake_send_real)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="atomic-send-test",
            poll=0.001,
            timeout=1,
        )
    )
    result = await core.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
        request_id="atomic-send-request",
    )

    assert attempted_atomic_send is True
    assert page.send_clicks == 0
    assert result.state == TurnState.UNKNOWN
    assert result.failure is not None
    assert result.failure.category.value == "frontend_not_ready"


@pytest.mark.asyncio
async def test_get_navigation_failure_requires_same_id_get(tmp_path, monkeypatch) -> None:
    from playwright.async_api import Error as PlaywrightError

    import playwright_api.service as service_module
    from playwright_api.cli import EXIT_RECOVERABLE_EXTERNAL, exit_code

    conversation_id = "get-navigation-failure"
    page = _FakePage(
        "created-target",
        goto_error=PlaywrightError("navigation failed"),
    )
    context = _FakeContext(page)
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="get-navigation-test",
        )
    )
    record = _persist_running_request(
        core,
        request_id="get-navigation-request",
        conversation_id=conversation_id,
    )

    result = await core.get(record.request_id)

    assert result.state == TurnState.UNKNOWN
    assert result.disposition == "get_required"
    assert result.failure is not None
    assert result.failure.category.value == "network"
    assert exit_code(result, command="get") == EXIT_RECOVERABLE_EXTERNAL
    assert page.closed is True


@pytest.mark.asyncio
async def test_get_target_identification_failure_closes_new_helper(
    tmp_path, monkeypatch
) -> None:
    from playwright.async_api import Error as PlaywrightError

    import playwright_api.service as service_module

    conversation_id = "get-target-failure"
    page = _FakePage("created-target")

    class Context:
        def __init__(self) -> None:
            self.pages = []
            self.request = None

        async def new_page(self):
            page.context = self
            return page

        async def new_cdp_session(self, _page):
            raise PlaywrightError("target lookup failed")

    _FakeBrowserSession.context_value = Context()
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="get-target-test",
        )
    )
    record = _persist_running_request(
        core,
        request_id="get-target-request",
        conversation_id=conversation_id,
    )

    result = await core.get(record.request_id)

    assert result.disposition == "get_required"
    assert result.failure is not None
    assert result.failure.category.value == "network"
    assert page.closed is True


@pytest.mark.asyncio
async def test_cancel_rejects_duplicate_exact_pages_without_click(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-duplicate-pages"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        snapshots = iter(
            [
                MonitorSnapshot("RUNNING", exact_graph),
                MonitorSnapshot("CANCELLED", exact_graph),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return next(type(self).snapshots)

    class Stop:
        clicks = 0

        async def click(self) -> None:
            type(self).clicks += 1

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(_page):
        return Stop()

    created = _FakePage("created-target")
    exact_one = _FakePage("exact-one", url=f"https://chatgpt.com/c/{conversation_id}")
    exact_two = _FakePage("exact-two", url=f"https://chatgpt.com/c/{conversation_id}")
    context = _FakeContext(created, [exact_one, exact_two])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-duplicate-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-duplicate-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.UNKNOWN
    assert result.failure is not None
    assert result.failure.category.value == "identity_conflict"
    assert context.new_page_calls == 0
    assert Stop.clicks == 0
    assert exact_one.closed is False
    assert exact_two.closed is False


@pytest.mark.asyncio
async def test_cancel_borrows_unique_exact_page_without_closing(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-borrowed-page"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        snapshots = iter(
            [
                MonitorSnapshot("RUNNING", exact_graph),
                MonitorSnapshot("CANCELLED", exact_graph),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return next(type(self).snapshots)

    clicked_pages = []

    class Stop:
        async def click(self) -> None:
            return None

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(page):
        clicked_pages.append(page)
        return Stop()

    created = _FakePage("created-target")
    borrowed = _FakePage("borrowed-target", url=f"https://chatgpt.com/c/{conversation_id}")
    context = _FakeContext(created, [borrowed])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-borrowed-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-borrowed-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.CANCELLED
    assert clicked_pages == [borrowed]
    assert context.new_page_calls == 0
    assert borrowed.closed is False
    assert created.closed is False


@pytest.mark.asyncio
async def test_cancel_closes_only_created_exact_page(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-created-page"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        snapshots = iter(
            [
                MonitorSnapshot("RUNNING", exact_graph),
                MonitorSnapshot("CANCELLED", exact_graph),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return next(type(self).snapshots)

    clicked_pages = []

    class Stop:
        async def click(self) -> None:
            return None

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(page):
        clicked_pages.append(page)
        return Stop()

    created = _FakePage("created-target")
    context = _FakeContext(created)
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-created-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-created-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.CANCELLED
    assert clicked_pages == [created]
    assert context.new_page_calls == 1
    assert created.goto_error is None
    assert created.url == f"https://chatgpt.com/c/{conversation_id}"
    assert created.closed is True


@pytest.mark.asyncio
async def test_frontend_accepted_transient_backend_failure_then_watch_closes_exact_helper(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import BackendUnavailableError
    from playwright_api.models import TurnIdentity
    from playwright_api.monitor import MonitorSnapshot
    from playwright_api.transport import FrontendAcceptance, FrontendHandoff
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
                if type(self).send_calls <= 3:
                    return MonitorSnapshot("RUNNING", baseline)
                raise BackendUnavailableError("backend GET failed with HTTP 429")
            return MonitorSnapshot("COMPLETE", completed)

    async def noop(*_args, **_kwargs) -> None:
        return None

    frontend_observations = 0

    async def observe(page):
        nonlocal frontend_observations
        from playwright_api.frontend import FrontendState

        frontend_observations += 1
        return FrontendState(
            url=page.url,
            composer_present=True,
            composer_editable=True,
            composer_text="" if frontend_observations == 1 else "prompt",
            attachment_count=0,
            send_visible=frontend_observations > 1,
            send_enabled=frontend_observations > 1,
            stop_visible=True,
            choice_prompt=False,
        )

    async def fake_send_real(
        _page,
        *,
        prompt,
        target,
        expected_attachment_names,
        send_timeout,
        on_accepted,
    ):
        assert prompt == "prompt"
        assert target == ChatTarget.conversation(conversation_id)
        assert expected_attachment_names == {}
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
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "send_real", fake_send_real)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
            poll=0.001,
            stable_seconds=0,
            identity_timeout=1,
            timeout=1,
        )
    )
    first = await core.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
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
    recovered = await core.get("recover-request")

    final = core.store.load("recover-request")
    assert recovered.success is True
    assert recovered.response == "RECOVERED_OK"
    assert final.state == TurnState.COMPLETE
    assert final.helper_page_closed_at is not None
    assert helper.closed is True
    assert unrelated.closed is False
    assert core.coordination.load(conversation_id).active_request_id is None


@pytest.mark.asyncio
async def test_recovery_respects_durable_keep_helper_policy(tmp_path) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
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
@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_terminal_request_retries_exact_helper_cleanup(
    tmp_path, monkeypatch, operation
) -> None:
    import playwright_api.service as service_module

    helper = _FakePage("terminal-helper")
    unrelated = _FakePage("terminal-unrelated")
    _FakeBrowserSession.context_value = _FakeContext(helper, [unrelated])
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
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


class _ForbiddenBrowserSession:
    entered = False

    def __init__(self, _config) -> None:
        type(self).entered = True
        raise AssertionError("browser session must not be constructed for corrupt state")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "changes"),
    [
        ("get", {"helper_page_keep": []}),
        ("get", {"helper_page_closed_at": 123}),
        ("cancel", {"helper_page_target_id": 123}),
        (
            "cancel",
            {
                "helper_page_target_id": None,
                "helper_page_closed_at": "2026-07-25T08:00:00+00:00",
            },
        ),
    ],
)
async def test_malformed_helper_state_fails_before_browser_or_ownership_mutation(
    tmp_path, monkeypatch, operation, changes
) -> None:
    import json

    import playwright_api.service as service_module
    from playwright_api.errors import CorruptStateError

    conversation_id = "corrupt-helper-conversation"
    request_id = "corrupt-helper-request"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path,
            coordination_dir=tmp_path / "coordination",
            deployment_id="service-test",
        )
    )
    value = (
        TurnRecord.new(
            request_id=request_id,
            prompt="prompt",
            target_kind="conversation",
            target_conversation_id=conversation_id,
        )
        .transition(TurnState.CANCELLED)
        .to_dict()
    )
    value.update(
        {
            "helper_page_target_id": "owned-target",
            "helper_page_keep": False,
            "helper_page_closed_at": None,
        }
    )
    value.update(changes)
    path = core.store.turn_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    claim = core.coordination.claim(conversation_id, request_id)
    original_revision = claim.revision
    _ForbiddenBrowserSession.entered = False
    monkeypatch.setattr(service_module, "BrowserSession", _ForbiddenBrowserSession)

    with pytest.raises(CorruptStateError):
        await getattr(core, operation)(request_id)

    assert _ForbiddenBrowserSession.entered is False
    assert path.read_text(encoding="utf-8") == raw
    unchanged = core.coordination.load(conversation_id)
    assert unchanged.active_request_id == request_id
    assert unchanged.revision == original_revision


@pytest.mark.asyncio
async def test_cancel_rejects_foreign_shared_owner_before_browser(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.models import SendProvenance, TurnIdentity

    conversation_id = "foreign-owner-conversation"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "local-state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="shared-browser",
        )
    )
    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
    )
    record = TurnRecord.new(
        request_id="local-stale-request",
        prompt="prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    core.store.create(record)
    core.coordination.claim(conversation_id, "foreign-request")
    _ForbiddenBrowserSession.entered = False
    monkeypatch.setattr(service_module, "BrowserSession", _ForbiddenBrowserSession)

    result = await core.cancel(record.request_id)

    from playwright_api.cli import EXIT_CANCELLATION_UNPROVEN, exit_code

    assert result.state == TurnState.UNKNOWN
    assert result.disposition == "cancellation_unproven"
    assert result.failure is not None
    assert result.failure.category.value == "cancellation_unproven"
    assert exit_code(result, command="cancel") == EXIT_CANCELLATION_UNPROVEN
    assert core.store.load(record.request_id).cancellation_requested_at is not None
    assert _ForbiddenBrowserSession.entered is False
    assert core.coordination.load(conversation_id).active_request_id == "foreign-request"


@pytest.mark.asyncio
async def test_cancel_terminal_observation_requires_graph_convergence(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.models import SendProvenance, TurnIdentity
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-convergence-conversation"
    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
        pre_send_current_node="root",
    )

    def completed(text: str):
        return graph(
            message("root", "system", None, turn=None, request=None),
            message("user-1", "user", "root"),
            message("assistant-1", "assistant", "user-1", text=text),
            current="assistant-1",
        )

    class Backend:
        calls = 0
        snapshots = iter(
            [
                MonitorSnapshot("COMPLETE", completed("PARTIAL")),
                MonitorSnapshot("COMPLETE", completed("FINAL")),
                MonitorSnapshot("COMPLETE", completed("FINAL")),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            type(self).calls += 1
            return next(type(self).snapshots)

    _FakeBrowserSession.context_value = _FakeContext(_FakePage("unused-page"))
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-test",
            poll=0.001,
            stable_samples=2,
            stable_seconds=0,
            timeout=1,
        )
    )
    record = TurnRecord.new(
        request_id="cancel-convergence-request",
        prompt="long prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    core.store.create(record)
    core.coordination.claim(conversation_id, record.request_id)

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.COMPLETE
    assert result.response == "FINAL"
    assert Backend.calls == 3
    assert core.coordination.load(conversation_id).active_request_id is None


@pytest.mark.asyncio
async def test_cancel_convergence_resets_across_running_snapshot(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module
    from playwright_api.models import SendProvenance, TurnIdentity
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-reset-running"
    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
        pre_send_current_node="root",
    )

    final_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="FINAL"),
        current="assistant-1",
    )

    class Backend:
        calls = 0
        snapshots = iter(
            [
                MonitorSnapshot("COMPLETE", final_graph),
                MonitorSnapshot("RUNNING", final_graph),
                MonitorSnapshot("COMPLETE", final_graph),
                MonitorSnapshot("COMPLETE", final_graph),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            type(self).calls += 1
            return next(type(self).snapshots)

    class Stop:
        clicked = False

        async def click(self) -> None:
            type(self).clicked = True

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(_page):
        return Stop()

    _FakeBrowserSession.context_value = _FakeContext(_FakePage("cancel-reset-page"))
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-reset-test",
            poll=0.001,
            stable_samples=2,
            stable_seconds=0,
            timeout=1,
        )
    )
    record = TurnRecord.new(
        request_id="cancel-reset-request",
        prompt="long prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    core.store.create(record)
    core.coordination.claim(conversation_id, record.request_id)

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.COMPLETE
    assert result.response == "FINAL"
    assert Backend.calls == 4
    assert Stop.clicked is True


@pytest.mark.asyncio
async def test_cancel_convergence_resets_when_exact_candidate_disappears(tmp_path) -> None:
    from playwright_api.models import SendProvenance, TurnIdentity
    from playwright_api.monitor import CandidateConvergence
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-reset-missing"
    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
        pre_send_current_node="root",
    )
    exact = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="FINAL"),
        current="assistant-1",
    )
    missing = graph(
        message("root", "system", None, turn=None, request=None),
        current="root",
    )
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-reset-test",
            stable_samples=2,
            stable_seconds=0,
        )
    )
    record = TurnRecord.new(
        request_id="cancel-reset-missing-request",
        prompt="prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    core.store.create(record)
    core.coordination.claim(conversation_id, record.request_id)
    convergence = CandidateConvergence(2, 0)

    first = await core._terminal_after_cancel_observation(
        record.request_id, identity, "COMPLETE", exact, convergence
    )
    second = await core._terminal_after_cancel_observation(
        record.request_id, identity, "COMPLETE", missing, convergence
    )
    third = await core._terminal_after_cancel_observation(
        record.request_id, identity, "COMPLETE", exact, convergence
    )
    fourth = await core._terminal_after_cancel_observation(
        record.request_id, identity, "COMPLETE", exact, convergence
    )

    assert first is None
    assert second is None
    assert third is None
    assert fourth is not None
    assert fourth.state == TurnState.COMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_turn_file_payload_identity_mismatch_fails_before_browser_or_claim_mutation(
    tmp_path, monkeypatch, operation
) -> None:
    import json

    import playwright_api.service as service_module
    from playwright_api.errors import CorruptStateError

    conversation_id = "poisoned-request-conversation"
    requested_id = "requested-id"
    payload_id = "different-id"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="poisoned-request-test",
        )
    )
    value = (
        TurnRecord.new(
            request_id=payload_id,
            prompt="prompt",
            target_kind="conversation",
            target_conversation_id=conversation_id,
        )
        .transition(TurnState.CANCELLED)
        .to_dict()
    )
    path = core.store.turn_path(requested_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    claim = core.coordination.claim(conversation_id, requested_id)
    _ForbiddenBrowserSession.entered = False
    monkeypatch.setattr(service_module, "BrowserSession", _ForbiddenBrowserSession)

    with pytest.raises(CorruptStateError, match="identity mismatch"):
        await getattr(core, operation)(requested_id)

    assert _ForbiddenBrowserSession.entered is False
    assert path.read_text(encoding="utf-8") == raw
    assert not core.store.turn_path(payload_id).exists()
    unchanged = core.coordination.load(conversation_id)
    assert unchanged.active_request_id == requested_id
    assert unchanged.revision == claim.revision


@pytest.mark.asyncio
async def test_cancel_raw_secret_mutation_requires_fresh_consecutive_samples(tmp_path) -> None:
    from playwright_api.models import SendProvenance, TurnIdentity
    from playwright_api.monitor import CandidateConvergence
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-raw-secret-mutation"
    identity = TurnIdentity(
        conversation_id=conversation_id,
        turn_exchange_id="turn-1",
        request_id="req-1",
        user_message_id="user-1",
        parent_message_id="root",
        pre_send_current_node="root",
    )

    def completed(text: str):
        return graph(
            message("root", "system", None, turn=None, request=None),
            message("user-1", "user", "root"),
            message("assistant-1", "assistant", "user-1", text=text),
            current="assistant-1",
        )

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-raw-secret-test",
            stable_samples=2,
            stable_seconds=0,
        )
    )
    record = TurnRecord.new(
        request_id="cancel-raw-secret-request",
        prompt="prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    )
    record = (
        record.transition(TurnState.PREPARING)
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
        .with_identity(identity)
        .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
        .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
        .transition(TurnState.RUNNING)
    )
    core.store.create(record)
    core.coordination.claim(conversation_id, record.request_id)
    convergence = CandidateConvergence(2, 0)

    first = await core._terminal_after_cancel_observation(
        record.request_id,
        identity,
        "COMPLETE",
        completed("proof_token=FIRST-SECRET"),
        convergence,
    )
    second = await core._terminal_after_cancel_observation(
        record.request_id,
        identity,
        "COMPLETE",
        completed("proof_token=SECOND-SECRET"),
        convergence,
    )
    third = await core._terminal_after_cancel_observation(
        record.request_id,
        identity,
        "COMPLETE",
        completed("proof_token=SECOND-SECRET"),
        convergence,
    )

    assert first is None
    assert second is None
    assert third is not None
    assert third.response == "proof_token=SECOND-SECRET"
    assert core.coordination.load(conversation_id).active_request_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "cancel"])
async def test_unknown_nested_identity_field_fails_before_browser_or_claim_mutation(
    tmp_path, monkeypatch, operation: str
) -> None:
    import json

    import playwright_api.service as service_module
    from playwright_api.errors import CorruptStateError
    from playwright_api.models import TurnIdentity

    conversation_id = "unknown-identity-conversation"
    request_id = "unknown-identity-request"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="unknown-identity-test",
        )
    )
    value = TurnRecord.new(
        request_id=request_id,
        prompt="prompt",
        target_kind="conversation",
        target_conversation_id=conversation_id,
    ).to_dict()
    value["identity"] = TurnIdentity(conversation_id=conversation_id).to_dict()
    value["identity"]["unexpected_runtime_pointer"] = "foreign-id"
    path = core.store.turn_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    claim = core.coordination.claim(conversation_id, request_id)
    _ForbiddenBrowserSession.entered = False
    monkeypatch.setattr(service_module, "BrowserSession", _ForbiddenBrowserSession)

    with pytest.raises(CorruptStateError):
        await getattr(core, operation)(request_id)

    assert _ForbiddenBrowserSession.entered is False
    assert path.read_text(encoding="utf-8") == raw
    unchanged = core.coordination.load(conversation_id)
    assert unchanged.active_request_id == request_id
    assert unchanged.revision == claim.revision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_url", "replacement_kind"),
    [
        ("https://chatgpt.com/?foreign=1#replacement", "query-fragment"),
        ("https://chatgpt.com/;foreign", "params"),
    ],
)
@pytest.mark.parametrize(
    ("replacement_phase", "expected_fill_count"),
    [("before_fill", 0), ("while_waiting", 1), ("final_preclick", 1)],
)
async def test_fresh_send_rejects_url_suffix_before_click_boundary(
    tmp_path,
    monkeypatch,
    replacement_url: str,
    replacement_kind: str,
    replacement_phase: str,
    expected_fill_count: int,
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import FrontendNotReadyError
    from playwright_api.frontend import FrontendState
    from playwright_api.models import SendProvenance

    page = _FakePage("fresh-target")
    context = _FakeContext(page)
    _FakeBrowserSession.context_value = context
    calls = 0
    fill_count = 0
    send_count = 0

    class Backend:
        def __init__(self, _context) -> None:
            pass

    def state(url: str, text: str, ready: bool) -> FrontendState:
        return FrontendState(
            url=url,
            composer_present=True,
            composer_editable=True,
            composer_text=text,
            attachment_count=0,
            send_visible=ready,
            send_enabled=ready,
            stop_visible=False,
            choice_prompt=False,
        )

    async def observe(_page):
        nonlocal calls
        calls += 1
        canonical = "https://chatgpt.com/"
        replaced = replacement_url
        if replacement_phase == "before_fill":
            return state(replaced, "" if calls == 1 else "prompt", calls > 1)
        if replacement_phase == "while_waiting":
            return state(
                canonical if calls == 1 else replaced, "" if calls == 1 else "prompt", calls > 1
            )
        return state(
            replaced if calls >= 3 else canonical, "" if calls == 1 else "prompt", calls > 1
        )

    async def fill(_page, prompt: str) -> None:
        nonlocal fill_count
        assert prompt == "prompt"
        fill_count += 1

    async def fail_send(*_args, **_kwargs):
        nonlocal send_count
        send_count += 1
        raise FrontendNotReadyError("fixture stopped before a real click")

    async def noop(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "fill_composer", fill)
    monkeypatch.setattr(service_module, "send_real", fail_send)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="fresh-url-suffix-test",
            poll=0.001,
            timeout=1,
        )
    )
    result = await core.send(
        "prompt",
        target=ChatTarget.fresh(),
        request_id=f"fresh-{replacement_kind}-{replacement_phase}",
    )
    persisted = core.store.load(result.request_id)

    assert result.state == TurnState.FAILED
    assert result.failure is not None
    assert result.failure.category.value == "frontend_not_ready"
    assert persisted.send_provenance == SendProvenance.NOT_ATTEMPTED
    assert fill_count == expected_fill_count
    assert send_count == 0
    assert page.closed is True


@pytest.mark.asyncio
async def test_send_ignores_noncanonical_conversation_page_before_composer_mutation(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import FrontendNotReadyError
    from playwright_api.frontend import FrontendState
    from playwright_api.monitor import MonitorSnapshot

    conversation_id = "send-canonical-page"
    unsupported = _FakePage(
        "unsupported-target",
        url=f"http://chatgpt.com/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [unsupported])
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})

    observations = 0
    filled_pages = []
    send_pages = []

    async def observe(page):
        nonlocal observations
        observations += 1
        return FrontendState(
            url=page.url,
            composer_present=True,
            composer_editable=True,
            composer_text="" if observations == 1 else "prompt",
            attachment_count=0,
            send_visible=observations > 1,
            send_enabled=observations > 1,
            stop_visible=False,
            choice_prompt=False,
        )

    async def fill(page, prompt: str) -> None:
        assert prompt == "prompt"
        filled_pages.append(page)

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def fail_send(
        page,
        *,
        prompt,
        target,
        expected_attachment_names,
        send_timeout,
        on_accepted,
    ):
        assert prompt == "prompt"
        assert target == ChatTarget.conversation(conversation_id)
        assert expected_attachment_names == {}
        assert send_timeout > 0
        assert on_accepted is not None
        send_pages.append(page)
        raise FrontendNotReadyError("fixture stopped before real click")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    monkeypatch.setattr(service_module, "fill_composer", fill)
    monkeypatch.setattr(service_module, "send_real", fail_send)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="send-canonical-test",
            poll=0.001,
            timeout=1,
        )
    )
    result = await core.send(
        "prompt",
        target=ChatTarget.conversation(conversation_id),
        request_id="send-canonical-request",
    )

    assert result.state == TurnState.UNKNOWN
    assert filled_pages == [created]
    assert send_pages == [created]
    assert context.new_page_calls == 1
    assert created.url == f"https://chatgpt.com/c/{conversation_id}"
    assert unsupported.closed is False


@pytest.mark.asyncio
async def test_get_ignores_noncanonical_conversation_page(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    import playwright_api.service as service_module

    conversation_id = "get-canonical-page"
    unsupported = _FakePage(
        "unsupported-target",
        url=f"https://chatgpt.com:444/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [unsupported])
    _FakeBrowserSession.context_value = context

    class Backend:
        def __init__(self, _context) -> None:
            pass

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def monitor(*_args, **_kwargs):
        return SimpleNamespace(text="FINAL")

    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "monitor_live", monitor)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="get-canonical-test",
        )
    )
    record = _persist_running_request(
        core,
        request_id="get-canonical-request",
        conversation_id=conversation_id,
    )

    result = await core.get(record.request_id)

    assert result.state == TurnState.COMPLETE
    assert result.response == "FINAL"
    assert context.new_page_calls == 1
    assert created.url == f"https://chatgpt.com/c/{conversation_id}"
    assert created.closed is True
    assert unsupported.closed is False


@pytest.mark.asyncio
async def test_cancel_ignores_noncanonical_conversation_page(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-canonical-page"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        snapshots = iter(
            [
                MonitorSnapshot("RUNNING", exact_graph),
                MonitorSnapshot("CANCELLED", exact_graph),
            ]
        )

        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return next(type(self).snapshots)

    clicked_pages = []

    class Stop:
        async def click(self) -> None:
            return None

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(page):
        clicked_pages.append(page)
        return Stop()

    unsupported = _FakePage(
        "unsupported-target",
        url=f"https://user:pass@chatgpt.com/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [unsupported])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-canonical-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-canonical-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.CANCELLED
    assert clicked_pages == [created]
    assert context.new_page_calls == 1
    assert created.url == f"https://chatgpt.com/c/{conversation_id}"
    assert created.closed is True
    assert unsupported.closed is False


@pytest.mark.asyncio
async def test_cancel_stop_lookup_failure_requires_same_id_get(tmp_path, monkeypatch) -> None:
    from playwright.async_api import Error as PlaywrightError

    import playwright_api.service as service_module
    from playwright_api.cli import EXIT_RECOVERABLE_EXTERNAL, exit_code
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-lookup-failure"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", exact_graph)

    class LookupFailurePage(_FakePage):
        def locator(self, _selector: str):
            raise PlaywrightError("stop lookup failed")

    async def noop(*_args, **_kwargs) -> None:
        return None

    borrowed = LookupFailurePage(
        "borrowed-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [borrowed])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-lookup-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-lookup-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.UNKNOWN
    assert result.disposition == "get_required"
    assert result.failure is not None
    assert result.failure.category.value == "network"
    assert exit_code(result, command="cancel") == EXIT_RECOVERABLE_EXTERNAL
    assert borrowed.closed is False
    assert context.new_page_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("click_error_kind", ["timeout", "network"])
async def test_cancel_stop_click_failure_is_cancellation_unproven(
    tmp_path, monkeypatch, click_error_kind: str
) -> None:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    import playwright_api.service as service_module
    from playwright_api.cli import EXIT_CANCELLATION_UNPROVEN, exit_code
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = f"cancel-click-{click_error_kind}"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", exact_graph)

    class Stop:
        async def click(self) -> None:
            if click_error_kind == "timeout":
                raise PlaywrightTimeoutError("stop click timed out")
            raise PlaywrightError("stop click failed")

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(_page):
        return Stop()

    borrowed = _FakePage(
        "borrowed-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [borrowed])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-click-test",
            poll=0.001,
            timeout=1,
        )
    )
    record = _persist_running_request(
        core,
        request_id=f"cancel-click-{click_error_kind}-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert result.state == TurnState.UNKNOWN
    assert result.disposition == "cancellation_unproven"
    assert result.failure is not None
    assert result.failure.category.value == "cancellation_unproven"
    assert exit_code(result, command="cancel") == EXIT_CANCELLATION_UNPROVEN
    assert borrowed.closed is False
    assert context.new_page_calls == 0


@pytest.mark.asyncio
async def test_cancel_post_click_timeout_is_cancellation_unproven(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.cli import EXIT_CANCELLATION_UNPROVEN, exit_code
    from playwright_api.monitor import MonitorSnapshot
    from tests.fixtures.graph_factory import graph, message

    conversation_id = "cancel-post-click-timeout"
    exact_graph = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text="partial"),
        current="assistant-1",
    )

    class Backend:
        def __init__(self, _context) -> None:
            pass

        async def snapshot(self, _conversation_id: str):
            return MonitorSnapshot("RUNNING", exact_graph)

    class Stop:
        clicks = 0

        async def click(self) -> None:
            type(self).clicks += 1

    async def noop(*_args, **_kwargs) -> None:
        return None

    async def find_stop(_page):
        return Stop()

    borrowed = _FakePage(
        "borrowed-target",
        url=f"https://chatgpt.com/c/{conversation_id}",
    )
    created = _FakePage("created-target")
    context = _FakeContext(created, [borrowed])
    _FakeBrowserSession.context_value = context
    monkeypatch.setattr(service_module, "BrowserSession", _FakeBrowserSession)
    monkeypatch.setattr(service_module, "AuthenticatedBackend", Backend)
    monkeypatch.setattr(service_module, "verify_authenticated", noop)
    monkeypatch.setattr(service_module, "find_stop_button", find_stop)

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="cancel-post-click-test",
            poll=0.001,
            timeout=0.01,
        )
    )
    record = _persist_running_request(
        core,
        request_id="cancel-post-click-request",
        conversation_id=conversation_id,
    )

    result = await core.cancel(record.request_id)

    assert Stop.clicks == 1
    assert result.state == TurnState.UNKNOWN
    assert result.disposition == "cancellation_unproven"
    assert result.failure is not None
    assert result.failure.category.value == "cancellation_unproven"
    assert exit_code(result, command="cancel") == EXIT_CANCELLATION_UNPROVEN
    assert borrowed.closed is False
