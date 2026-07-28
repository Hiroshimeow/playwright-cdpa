from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from playwright_api.cli import (
    EXIT_AMBIGUOUS,
    EXIT_CANCELLATION_UNPROVEN,
    EXIT_CANCELLED,
    EXIT_INVARIANT,
    EXIT_OWNERSHIP,
    EXIT_RECOVERABLE_EXTERNAL,
    EXIT_TERMINAL_EXTERNAL,
    build_parser,
    exit_code,
)
from playwright_api.config import ClientConfig
from playwright_api.errors import ConflictingIdentityError, Failure, FailureCategory
from playwright_api.models import Result, SendProvenance, TurnRecord, TurnState
from playwright_api.service import ChatGPTClient
from playwright_api.targets import ChatTarget


@pytest.mark.asyncio
async def test_existing_send_claims_before_creating_local_turn(tmp_path, monkeypatch) -> None:
    coordination = tmp_path / "coordination"
    owner = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "owner",
            coordination_dir=coordination,
            deployment_id="shared",
            timeout=1,
            poll=0.001,
        )
    )
    contender = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "contender",
            coordination_dir=coordination,
            deployment_id="shared",
            timeout=1,
            poll=0.001,
        )
    )
    owner.coordination.claim("conversation-1", "foreign")

    async def fake_send_record(
        self, record, prompt, target, *, claimed_coordination=None
    ):
        assert prompt == "next"
        assert target == ChatTarget.conversation("conversation-1")
        assert claimed_coordination == "conversation-1"
        assert self.coordination.load(claimed_coordination).active_request_id == record.request_id
        return self._result(record)

    monkeypatch.setattr(ChatGPTClient, "_send_record", fake_send_record)
    task = asyncio.create_task(
        contender.send(
            "next",
            target=ChatTarget.conversation("conversation-1"),
            request_id="next-request",
        )
    )
    await asyncio.sleep(0.02)
    assert not contender.store.turn_path("next-request").exists()

    owner.coordination.release("conversation-1", "foreign", terminal=True)
    result = await task

    assert result.request_id == "next-request"
    assert contender.store.turn_path("next-request").exists()
    assert contender.coordination.load("conversation-1").active_request_id == "next-request"


@pytest.mark.asyncio
async def test_competing_existing_sends_create_only_winning_local_turn(
    tmp_path, monkeypatch
) -> None:
    coordination = tmp_path / "coordination"
    blocker = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "blocker",
            coordination_dir=coordination,
            deployment_id="shared",
        )
    )
    contenders = [
        ChatGPTClient(
            ClientConfig(
                state_dir=tmp_path / name,
                coordination_dir=coordination,
                deployment_id="shared",
                timeout=0.05,
                poll=0.001,
            )
        )
        for name in ("one", "two")
    ]
    blocker.coordination.claim("conversation-1", "foreign")
    hold = asyncio.Event()
    winner_started = asyncio.Event()
    observed_foreign = [asyncio.Event(), asyncio.Event()]

    for index, core in enumerate(contenders):
        coordination_store = core.coordination
        original_load = coordination_store.load

        def load(conversation_id, *, _index=index, _load=original_load):
            record = _load(conversation_id)
            if record.active_request_id == "foreign":
                observed_foreign[_index].set()
            return record

        monkeypatch.setattr(coordination_store, "load", load)

    async def fake_send_record(
        self, record, prompt, target, *, claimed_coordination=None
    ):
        assert target == ChatTarget.conversation("conversation-1")
        assert claimed_coordination == "conversation-1"
        winner_started.set()
        await hold.wait()
        return self._result(record)

    monkeypatch.setattr(ChatGPTClient, "_send_record", fake_send_record)
    tasks = [
        asyncio.create_task(
            core.send(
                "next",
                target=ChatTarget.conversation("conversation-1"),
                request_id=f"request-{index}",
            )
        )
        for index, core in enumerate(contenders, start=1)
    ]
    await asyncio.gather(*(event.wait() for event in observed_foreign))
    blocker.coordination.release("conversation-1", "foreign", terminal=True)
    await winner_started.wait()
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    assert len(done) == 1
    assert len(pending) == 1
    hold.set()
    results = await asyncio.gather(*tasks)

    ownership_timeouts = [
        result for result in results if result.disposition == "ownership_timeout"
    ]
    winners = [result for result in results if result.disposition != "ownership_timeout"]
    assert len(ownership_timeouts) == 1
    assert len(winners) == 1
    existing = [
        core.store.turn_path(f"request-{index}").exists()
        for index, core in enumerate(contenders, start=1)
    ]
    assert existing.count(True) == 1


def test_status_is_local_only_and_does_not_create_coordination(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module

    class ForbiddenBrowserSession:
        def __init__(self, _config) -> None:
            raise AssertionError("status must not construct BrowserSession")

    monkeypatch.setattr(service_module, "BrowserSession", ForbiddenBrowserSession)
    coordination = tmp_path / "coordination"
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=coordination,
            deployment_id="shared",
        )
    )
    core.store.create(TurnRecord.new(request_id="status-request", prompt="draft"))
    assert not coordination.exists()

    result = core.status("status-request")

    assert result.request_id == "status-request"
    assert result.state == TurnState.NEW
    assert not coordination.exists()


@pytest.mark.parametrize(
    ("state", "response", "failure", "disposition", "code"),
    [
        (TurnState.RUNNING, None, None, "get_required", EXIT_AMBIGUOUS),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.BACKEND, "temporary", retryable=True, external=True),
            "get_required",
            EXIT_RECOVERABLE_EXTERNAL,
        ),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.NETWORK, "offline", retryable=True, external=True),
            "get_required",
            EXIT_RECOVERABLE_EXTERNAL,
        ),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.TIMEOUT, "wait timed out", retryable=True, external=True),
            "get_required",
            EXIT_AMBIGUOUS,
        ),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.IDENTITY_MISSING, "identity missing", retryable=True),
            "get_required",
            EXIT_INVARIANT,
        ),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.SCHEMA_DRIFT, "schema changed", external=True),
            "get_required",
            EXIT_INVARIANT,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(
                FailureCategory.TIMEOUT, "pre-click timeout", retryable=True, external=True
            ),
            "external_failure",
            EXIT_RECOVERABLE_EXTERNAL,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.BACKEND, "temporary", retryable=True, external=True),
            "external_failure",
            EXIT_RECOVERABLE_EXTERNAL,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.BACKEND, "terminal", external=True),
            "external_failure",
            EXIT_TERMINAL_EXTERNAL,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.SCHEMA_DRIFT, "schema changed", external=True),
            "invariant_failure",
            EXIT_INVARIANT,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.CORRUPT_STATE, "corrupt"),
            "invariant_failure",
            EXIT_INVARIANT,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.INVARIANT, "bad"),
            "invariant_failure",
            EXIT_INVARIANT,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.OWNERSHIP, "conflict", retryable=True),
            "invariant_failure",
            EXIT_INVARIANT,
        ),
        (
            TurnState.FAILED,
            None,
            Failure(FailureCategory.OWNERSHIP_TIMEOUT, "busy", retryable=True),
            "ownership_timeout",
            EXIT_OWNERSHIP,
        ),
        (
            TurnState.UNKNOWN,
            None,
            Failure(FailureCategory.CANCELLATION_UNPROVEN, "unknown", retryable=True),
            "cancellation_unproven",
            EXIT_CANCELLATION_UNPROVEN,
        ),
        (TurnState.CANCELLED, None, None, "cancelled", EXIT_CANCELLED),
        (TurnState.COMPLETE, "ok", None, "complete", 0),
        (TurnState.COMPLETE, None, None, "get_required", EXIT_AMBIGUOUS),
        (
            TurnState.COMPLETE,
            None,
            Failure(FailureCategory.BACKEND, "temporary", retryable=True, external=True),
            "external_failure",
            EXIT_RECOVERABLE_EXTERNAL,
        ),
        (
            TurnState.COMPLETE,
            None,
            Failure(FailureCategory.SCHEMA_DRIFT, "schema changed", external=True),
            "invariant_failure",
            EXIT_INVARIANT,
        ),
    ],
)
def test_result_and_exit_taxonomy_are_state_aware(
    state: TurnState,
    response: str | None,
    failure: Failure | None,
    disposition: str,
    code: int,
) -> None:
    result = Result(1, "request", state, response=response, failure=failure)

    assert result.disposition == disposition
    assert exit_code(result, command="send") == code


@pytest.mark.asyncio
async def test_readme_style_caller_uses_same_id_get_for_unknown_external_failure() -> None:
    class Core:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, request_id: str) -> Result:
            self.calls.append(request_id)
            return Result(1, request_id, TurnState.COMPLETE, response="EXACT")

    core = Core()
    result = Result(
        1,
        "caller-owned-id",
        TurnState.UNKNOWN,
        failure=Failure(
            FailureCategory.BACKEND,
            "temporary backend outage",
            retryable=True,
            external=True,
        ),
    )

    if result.disposition == "get_required":
        result = await core.get(result.request_id)
    if not result.success:
        raise RuntimeError(result.failure)

    assert core.calls == ["caller-owned-id"]
    assert result.response == "EXACT"


def test_primary_cli_surface_is_send_get_status_cancel_only() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "send" in help_text
    assert "get" in help_text
    assert "status" in help_text
    assert "cancel" in help_text
    assert "watch" not in help_text
    assert "wait-idle" not in help_text
    assert "keep-helper-tab" not in help_text

    args = parser.parse_args(
        ["send", "hello", "--target", "/", "--request-id", "caller-id", "--json"]
    )
    assert args.request_id == "caller-id"
    assert args.target == "/"
    assert not hasattr(ChatGPTClient, "watch")
    assert not hasattr(ChatGPTClient, "recover")
    assert not hasattr(ChatGPTClient, "wait_idle_and_send")


class _CDP:
    def __init__(self, page) -> None:
        self.page = page

    async def send(self, method: str):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.page.target_id}}

    async def detach(self) -> None:
        return None


class _Page:
    def __init__(self, target_id: str, url: str) -> None:
        self.target_id = target_id
        self.url = url
        self.closed = False
        self.goto_urls: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_urls.append(url)
        self.url = url

    def is_closed(self) -> bool:
        return self.closed


class _Context:
    def __init__(self, pages: list[_Page], created: _Page | None = None) -> None:
        self.pages = pages
        self.created = created

    async def new_page(self) -> _Page:
        assert self.created is not None
        self.pages.append(self.created)
        return self.created

    async def new_cdp_session(self, page: _Page) -> _CDP:
        return _CDP(page)


@pytest.mark.asyncio
async def test_exact_page_resolver_borrows_one_exact_page() -> None:
    from playwright_api.connection import resolve_conversation_page

    exact = _Page("exact-target", "https://chatgpt.com/c/conversation-1")
    unrelated = _Page("other-target", "https://chatgpt.com/c/conversation-2")
    lease = await resolve_conversation_page(_Context([unrelated, exact]), "conversation-1")

    assert lease.page is exact
    assert lease.target_id == "exact-target"
    assert lease.owned is False


@pytest.mark.asyncio
async def test_exact_page_resolver_rejects_duplicate_exact_pages() -> None:
    from playwright_api.connection import resolve_conversation_page

    context = _Context(
        [
            _Page("one", "https://chatgpt.com/c/conversation-1"),
            _Page("two", "https://chatgpt.com/c/conversation-1"),
        ]
    )
    with pytest.raises(ConflictingIdentityError, match="multiple"):
        await resolve_conversation_page(context, "conversation-1")


@pytest.mark.asyncio
async def test_exact_page_resolver_opens_only_exact_url_when_missing() -> None:
    from playwright_api.connection import resolve_conversation_page

    created = _Page("created-target", "about:blank")
    context = _Context(
        [_Page("other", "https://chatgpt.com/c/conversation-2")], created=created
    )
    lease = await resolve_conversation_page(context, "conversation-1")

    assert lease.page is created
    assert lease.owned is True
    assert created.goto_urls == ["https://chatgpt.com/c/conversation-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported_url",
    [
        "http://chatgpt.com/c/conversation-1",
        "https://chatgpt.com:444/c/conversation-1",
        "https://user:pass@chatgpt.com/c/conversation-1",
        "https://chatgpt.com/c/conversation-1?foreign=1#x",
        "https://chatgpt.example/c/conversation-1",
        "https://chatgpt.com:bad/c/conversation-1",
    ],
)
async def test_exact_page_resolver_ignores_noncanonical_candidate(
    unsupported_url: str,
) -> None:
    from playwright_api.connection import resolve_conversation_page

    unsupported = _Page("unsupported-target", unsupported_url)
    created = _Page("created-target", "about:blank")
    context = _Context([unsupported], created=created)

    lease = await resolve_conversation_page(context, "conversation-1")

    assert lease.page is created
    assert lease.owned is True
    assert created.goto_urls == ["https://chatgpt.com/c/conversation-1"]
    assert unsupported.closed is False


class _WaitPage:
    def __init__(self) -> None:
        self.context = object()


class _WaitBackend:
    async def snapshot(self, _conversation_id: str):
        from playwright_api.monitor import MonitorSnapshot

        return MonitorSnapshot("RUNNING", {"mapping": {}, "current_node": None})


def _frontend_state(
    *,
    ready: bool,
    attachments: int = 0,
    url: str = "https://chatgpt.com/c/conversation-1",
    composer_text: str = "queued prompt",
):
    from playwright_api.frontend import FrontendState

    return FrontendState(
        url=url,
        composer_present=True,
        composer_editable=True,
        composer_text=composer_text,
        attachment_count=attachments,
        send_visible=ready,
        send_enabled=ready,
        stop_visible=True,
        choice_prompt=False,
    )


def _waiting_core(tmp_path) -> tuple[ChatGPTClient, TurnRecord]:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="steering-test",
            timeout=0.1,
            poll=0.001,
        )
    )
    record = core.store.create(
        TurnRecord.new(
            request_id="steer-request",
            prompt="queued prompt",
            target_kind="conversation",
            target_conversation_id="conversation-1",
        )
    )
    core.coordination.claim("conversation-1", record.request_id)
    return core, record


def _fresh_waiting_core(tmp_path) -> tuple[ChatGPTClient, TurnRecord]:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="fresh-page-test",
            timeout=0.1,
            poll=0.001,
        )
    )
    record = core.store.create(
        TurnRecord.new(
            request_id="fresh-request",
            prompt="queued prompt",
            target_kind="fresh",
        )
    )
    return core, record


@pytest.mark.asyncio
async def test_active_frontend_with_send_ready_is_one_steering_boundary(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module

    core, record = _waiting_core(tmp_path)
    observations = 0

    async def target_id(_context, _page):
        return "exact-target"

    async def observe(_page):
        nonlocal observations
        observations += 1
        return _frontend_state(ready=True)

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    state = await core._wait_for_send_ready(
        _WaitPage(),  # type: ignore[arg-type]
        _WaitBackend(),  # type: ignore[arg-type]
        record.request_id,
        "queued prompt",
        ChatTarget.conversation("conversation-1"),
        "exact-target",
        record.revision,
        {},
        Counter(),
    )

    assert state.send_ready is True
    assert state.stop_visible is True
    assert observations == 1
    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_active_frontend_without_send_waits_and_preserves_exact_prompt(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module

    core, record = _waiting_core(tmp_path)
    states = iter([_frontend_state(ready=False), _frontend_state(ready=True)])

    async def target_id(_context, _page):
        return "exact-target"

    async def observe(_page):
        return next(states)

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    state = await core._wait_for_send_ready(
        _WaitPage(),  # type: ignore[arg-type]
        _WaitBackend(),  # type: ignore[arg-type]
        record.request_id,
        "queued prompt",
        ChatTarget.conversation("conversation-1"),
        "exact-target",
        record.revision,
        {},
        Counter(),
    )

    assert state.send_ready is True
    assert state.composer_text == "queued prompt"
    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_preclick_owner_drift_is_invariant_not_owner_wait_timeout(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.cli import EXIT_INVARIANT, exit_code
    from playwright_api.errors import OwnershipConflictError

    core, record = _waiting_core(tmp_path)
    core.coordination.release("conversation-1", record.request_id, terminal=False)
    core.coordination.claim("conversation-1", "foreign-request")

    async def target_id(_context, _page):
        return "exact-target"

    async def observe(_page):
        return _frontend_state(ready=True)

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)

    with pytest.raises(OwnershipConflictError) as captured:
        await core._wait_for_send_ready(
            _WaitPage(),  # type: ignore[arg-type]
            _WaitBackend(),  # type: ignore[arg-type]
            record.request_id,
            "queued prompt",
            ChatTarget.conversation("conversation-1"),
            "exact-target",
            record.revision,
            {},
            Counter(),
        )

    failure = captured.value.as_failure()
    result = Result(1, record.request_id, TurnState.FAILED, failure=failure)
    assert result.disposition == "invariant_failure"
    assert exit_code(result, command="send") == EXIT_INVARIANT
    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED
    assert core.coordination.load("conversation-1").active_request_id == "foreign-request"


@pytest.mark.asyncio
async def test_fresh_send_wait_rejects_same_target_navigation_to_conversation(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import FrontendNotReadyError

    core, record = _fresh_waiting_core(tmp_path)

    async def target_id(_context, _page):
        return "same-target"

    async def observe(_page):
        return _frontend_state(
            ready=True,
            url="https://chatgpt.com/c/foreign-conversation",
        )

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)

    with pytest.raises(FrontendNotReadyError, match="exact Send target"):
        await core._wait_for_send_ready(
            _WaitPage(),  # type: ignore[arg-type]
            _WaitBackend(),  # type: ignore[arg-type]
            record.request_id,
            "queued prompt",
            ChatTarget.fresh(),
            "same-target",
            record.revision,
            {},
            Counter(),
        )

    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_fresh_send_final_preclick_rejects_same_target_navigation_to_conversation(
    tmp_path, monkeypatch
) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import FrontendNotReadyError

    core, record = _fresh_waiting_core(tmp_path)

    async def target_id(_context, _page):
        return "same-target"

    async def observe(_page):
        return _frontend_state(
            ready=True,
            url="https://chatgpt.com/c/foreign-conversation",
        )

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)

    with pytest.raises(FrontendNotReadyError, match="exact Send target"):
        await core._validate_ready_state(
            _WaitPage(),  # type: ignore[arg-type]
            "queued prompt",
            ChatTarget.fresh(),
            "same-target",
            Counter(),
        )

    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://chatgpt.com/share/foreign-conversation",
    ],
)
def test_fresh_send_initial_state_rejects_unsupported_page(tmp_path, url) -> None:
    from playwright_api.errors import FrontendNotReadyError

    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="fresh-page-test",
        )
    )
    state = _frontend_state(ready=True, url=url, composer_text="")

    with pytest.raises(FrontendNotReadyError, match="exact Send target"):
        core._validate_initial_send_state(state, ChatTarget.fresh())


@pytest.mark.asyncio
async def test_get_observer_waits_through_transient_sent_state(tmp_path) -> None:
    core = ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="get-observer-test",
            identity_timeout=0.2,
            poll=0.001,
        )
    )
    from playwright_api.models import TurnIdentity

    record = TurnRecord.new(request_id="observer-request", prompt="draft")
    record = record.transition(TurnState.PREPARING).with_identity(
        TurnIdentity(
            conversation_id="conversation-1",
            user_message_id="user-message-1",
            frontend_parent_message_id="parent-message-1",
        )
    )
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = record.with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
    record = core.store.create(record.transition(TurnState.SENT))

    async def advance() -> None:
        await asyncio.sleep(0.01)
        current = core.store.load(record.request_id)
        core.store.save(
            current.with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
            .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
            .transition(TurnState.RUNNING),
            expected_revision=current.revision,
        )

    task = asyncio.create_task(advance())
    attached = await core._await_attachable_record(record)
    await task

    assert attached.state == TurnState.RUNNING


@pytest.mark.asyncio
async def test_attachment_drift_fails_before_click_boundary(tmp_path, monkeypatch) -> None:
    import playwright_api.service as service_module
    from playwright_api.errors import FrontendNotReadyError

    core, record = _waiting_core(tmp_path)

    async def target_id(_context, _page):
        return "exact-target"

    async def observe(_page):
        return _frontend_state(ready=True, attachments=1)

    monkeypatch.setattr(service_module, "page_target_id", target_id)
    monkeypatch.setattr(service_module, "observe_frontend", observe)
    with pytest.raises(FrontendNotReadyError, match="attachments"):
        await core._wait_for_send_ready(
            _WaitPage(),  # type: ignore[arg-type]
            _WaitBackend(),  # type: ignore[arg-type]
            record.request_id,
            "queued prompt",
            ChatTarget.conversation("conversation-1"),
            "exact-target",
            record.revision,
            {},
            Counter(),
        )

    assert core.store.load(record.request_id).send_provenance == SendProvenance.NOT_ATTEMPTED
