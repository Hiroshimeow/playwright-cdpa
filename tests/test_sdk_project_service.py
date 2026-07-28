from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from playwright_api import (
    ChatGPTClient,
    ChatTarget,
    ClientConfig,
    ProjectMemoryScope,
    ProjectRef,
    TurnIdentity,
)
from playwright_api.errors import (
    AmbiguousOutcomeError,
    ConflictingIdentityError,
    FrontendNotReadyError,
    OperationTimeoutError,
)
from playwright_api.models import SendProvenance, TurnRecord, TurnState
from playwright_api.monitor import MonitorSnapshot
from tests.fixtures.graph_factory import graph, message


def project(project_id: str, name: str = "Task Project") -> ProjectRef:
    return ProjectRef(
        project_id=project_id,
        canonical_url=f"https://chatgpt.com/g/{project_id}/project",
        name=name,
        memory_scope=ProjectMemoryScope.DEFAULT,
    )


def client(tmp_path: Path) -> ChatGPTClient:
    return ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="project-service",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["find", "create"])
async def test_project_helper_closes_page_before_browser_session_detach(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    import playwright_api.service as service_module

    sdk = client(tmp_path)
    expected = project("g-p-project123")
    events: list[str] = []

    class Page:
        detached = False

        async def goto(self, _url: str, *, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            events.append("goto")

        def is_closed(self) -> bool:
            return self.detached

        async def close(self) -> None:
            assert self.detached is False
            events.append("close")

    page = Page()

    class Context:
        async def new_page(self) -> Page:
            events.append("new_page")
            return page

    class Session:
        def __init__(self, _config) -> None:
            self.context = Context()

        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *_args) -> None:
            events.append("detach")
            page.detached = True

    async def verify(_page) -> None:
        events.append("verify")

    async def find_frontend(_page, **_kwargs) -> ProjectRef:
        events.append("frontend")
        return expected

    async def create_frontend(
        _page, *, name, memory_scope, on_create_boundary
    ) -> ProjectRef:
        assert name == "Task Project"
        assert memory_scope is ProjectMemoryScope.DEFAULT
        assert callable(on_create_boundary)
        events.append("frontend")
        return expected

    monkeypatch.setattr(service_module, "BrowserSession", Session)
    monkeypatch.setattr(service_module, "verify_authenticated", verify)
    monkeypatch.setattr(service_module, "find_project_frontend", find_frontend)
    monkeypatch.setattr(service_module, "create_project_frontend", create_frontend)

    if operation == "find":
        result = await sdk._find_project(name="Task Project")  # noqa: SLF001
    else:
        result = await sdk._create_project(  # noqa: SLF001
            "Task Project",
            ProjectMemoryScope.DEFAULT,
            lambda: None,
        )

    assert result == expected
    assert events.index("close") < events.index("detach")


@pytest.mark.asyncio
async def test_ensure_project_creates_once_after_durable_unknown_marker(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)
    expected = project("g-p-project123")
    observed_unknown: list[bool] = []

    async def find_project(**_kwargs):
        return None

    async def create_project(name, memory_scope, on_create_boundary):
        assert sdk.projects.load("task-123") is None
        on_create_boundary()
        record = sdk.projects.load("task-123")
        observed_unknown.append(record is not None and record.creation_unknown)
        assert name == "Task Project"
        assert memory_scope is ProjectMemoryScope.DEFAULT
        return expected

    monkeypatch.setattr(sdk, "_find_project", find_project)
    monkeypatch.setattr(sdk, "_create_project", create_project)

    first = await sdk.ensure_project("task-123", "Task Project")
    second = await sdk.ensure_project("task-123", "Task Project")

    assert first == expected
    assert second == expected
    assert observed_unknown == [True]


@pytest.mark.asyncio
async def test_ensure_project_serializes_create_across_application_state_roots(
    tmp_path: Path, monkeypatch
) -> None:
    shared = {
        "coordination_dir": tmp_path / "coordination",
        "deployment_id": "shared-project-profile",
        "poll": 0.001,
        "timeout": 1.0,
    }
    first = ChatGPTClient(ClientConfig(state_dir=tmp_path / "state-a", **shared))
    second = ChatGPTClient(ClientConfig(state_dir=tmp_path / "state-b", **shared))
    expected = project("g-p-0123456789abcdef0123456789abcdea")
    duplicate = project("g-p-0123456789abcdef0123456789abcdeb")
    created: ProjectRef | None = None
    create_calls = 0
    first_boundary = asyncio.Event()
    release_first = asyncio.Event()

    async def find_project(**_kwargs):
        return created

    async def create_first(name, memory_scope, on_create_boundary):
        nonlocal create_calls, created
        assert name == "Task Project"
        assert memory_scope is ProjectMemoryScope.DEFAULT
        on_create_boundary()
        create_calls += 1
        first_boundary.set()
        await release_first.wait()
        created = expected
        return expected

    async def create_second(name, memory_scope, on_create_boundary):
        nonlocal create_calls, created
        assert name == "Task Project"
        assert memory_scope is ProjectMemoryScope.DEFAULT
        on_create_boundary()
        create_calls += 1
        created = duplicate
        return duplicate

    monkeypatch.setattr(first, "_find_project", find_project)
    monkeypatch.setattr(second, "_find_project", find_project)
    monkeypatch.setattr(first, "_create_project", create_first)
    monkeypatch.setattr(second, "_create_project", create_second)

    first_task = asyncio.create_task(first.ensure_project("task-123", "Task Project"))
    await asyncio.wait_for(first_boundary.wait(), timeout=1)
    second_task = asyncio.create_task(second.ensure_project("task-123", "Task Project"))
    await asyncio.sleep(0.05)
    release_first.set()

    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert create_calls == 1
    assert first_result == second_result == expected
    assert first.config.coordination_root == second.config.coordination_root
    assert first.config.state_dir != second.config.state_dir
    claim_files = list(first.project_creations.records.glob("*.json"))
    assert len(claim_files) == 1
    claim_text = claim_files[0].read_text(encoding="utf-8")
    assert "project_id" not in claim_text
    assert "canonical_url" not in claim_text
    assert expected.project_id not in claim_text


@pytest.mark.asyncio
async def test_project_key_metadata_conflict_is_deployment_scoped(
    tmp_path: Path, monkeypatch
) -> None:
    shared = {
        "coordination_dir": tmp_path / "coordination",
        "deployment_id": "shared-project-profile",
        "poll": 0.001,
        "timeout": 1.0,
    }
    first = ChatGPTClient(ClientConfig(state_dir=tmp_path / "state-a", **shared))
    second = ChatGPTClient(ClientConfig(state_dir=tmp_path / "state-b", **shared))
    expected = project("g-p-0123456789abcdef0123456789abcdea")

    async def no_match(**_kwargs):
        return None

    async def create_project(_name, _memory_scope, on_create_boundary):
        on_create_boundary()
        return expected

    async def frontend_must_not_run(**_kwargs):
        raise AssertionError("metadata conflict must fail before frontend reconciliation")

    monkeypatch.setattr(first, "_find_project", no_match)
    monkeypatch.setattr(first, "_create_project", create_project)
    monkeypatch.setattr(second, "_find_project", frontend_must_not_run)

    assert await first.ensure_project("task-123", "Task Project") == expected
    with pytest.raises(ConflictingIdentityError, match="different metadata"):
        await second.ensure_project("task-123", "Other Project")


@pytest.mark.asyncio
async def test_pre_click_project_failure_does_not_create_unknown_marker(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)

    async def find_project(**_kwargs):
        return None

    async def create_project(_name, _memory_scope, _on_create_boundary):
        raise FrontendNotReadyError("create control was not actionable")

    monkeypatch.setattr(sdk, "_find_project", find_project)
    monkeypatch.setattr(sdk, "_create_project", create_project)

    with pytest.raises(FrontendNotReadyError, match="actionable"):
        await sdk.ensure_project("task-123", "Task Project")
    assert sdk.projects.load("task-123") is None
    assert sdk.project_creations.load("task-123") is None


@pytest.mark.asyncio
async def test_post_click_project_failure_persists_unknown_marker(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)

    async def find_project(**_kwargs):
        return None

    async def create_project(_name, _memory_scope, on_create_boundary):
        on_create_boundary()
        raise OperationTimeoutError("project creation outcome is unknown")

    monkeypatch.setattr(sdk, "_find_project", find_project)
    monkeypatch.setattr(sdk, "_create_project", create_project)

    with pytest.raises(OperationTimeoutError, match="unknown"):
        await sdk.ensure_project("task-123", "Task Project")
    record = sdk.projects.load("task-123")
    assert record is not None
    assert record.creation_unknown is True
    assert record.project is None
    assert sdk.project_creations.load("task-123") == (
        "Task Project",
        ProjectMemoryScope.DEFAULT,
    )


@pytest.mark.asyncio
async def test_uncertain_project_creation_reconciles_exact_match_without_second_create(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)
    expected = project("g-p-project123")
    sdk.projects.begin_creation(
        key="task-123",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )
    calls = 0

    async def find_project(**_kwargs):
        return expected

    async def create_project(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("uncertain creation must reconcile, not click Create again")

    monkeypatch.setattr(sdk, "_find_project", find_project)
    monkeypatch.setattr(sdk, "_create_project", create_project)

    assert await sdk.ensure_project("task-123", "Task Project") == expected
    assert calls == 0


@pytest.mark.asyncio
async def test_uncertain_project_creation_with_zero_match_stays_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)
    sdk.projects.begin_creation(
        key="task-123",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )

    async def find_project(**_kwargs):
        return None

    monkeypatch.setattr(sdk, "_find_project", find_project)

    with pytest.raises(AmbiguousOutcomeError, match="unknown"):
        await sdk.ensure_project("task-123", "Task Project")


@pytest.mark.asyncio
async def test_unknown_project_root_recovers_exact_conversation_from_slugged_page(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)
    project_id = "g-p-0123456789abcdef0123456789abcdef"
    conversation_id = "conversation-123"
    record = sdk.store.create(
        TurnRecord.new(
            request_id="project-root-request",
            prompt="project prompt",
            target=ChatTarget.project(project_id),
        )
    )
    record = sdk.store.save(
        record.transition(TurnState.PREPARING)
        .with_identity(TurnIdentity(user_message_id="user-1"))
        .with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED),
        expected_revision=record.revision,
    )
    record = sdk.store.save(
        record.with_send_provenance(SendProvenance.FRONTEND_ACCEPTED).transition(
            TurnState.SENT
        ),
        expected_revision=record.revision,
    )
    record = sdk.store.save(
        record.transition(
            TurnState.UNKNOWN,
            failure=AmbiguousOutcomeError("project route unresolved").as_failure(),
        ),
        expected_revision=record.revision,
    )

    class Page:
        url = (
            f"https://chatgpt.com/g/{project_id}-task-project/c/{conversation_id}"
        )

    class Context:
        pages = [Page()]

    class Backend:
        async def snapshot(self, observed_conversation_id: str) -> MonitorSnapshot:
            assert observed_conversation_id == conversation_id
            return MonitorSnapshot(
                stream_status="COMPLETE",
                graph=graph(
                    message("user-1", "user", None, text="project prompt"),
                    current="user-1",
                ),
            )

    recovered = await sdk._recover_project_conversation_identity(  # noqa: SLF001
        record,
        Context(),  # type: ignore[arg-type]
        Backend(),  # type: ignore[arg-type]
    )

    assert recovered.state is TurnState.RUNNING
    assert recovered.send_provenance is SendProvenance.DURABLE_HANDOFF
    assert recovered.identity is not None
    assert recovered.identity.conversation_id == conversation_id
    assert recovered.identity.user_message_id == "user-1"
    assert (
        sdk.coordination.load(
            ChatTarget.project_conversation(project_id, conversation_id).coordination_key
        ).active_request_id
        == record.request_id
    )


@pytest.mark.asyncio
async def test_find_project_fails_closed_on_duplicate_exact_name(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)

    async def find_project(**_kwargs):
        raise ConflictingIdentityError("multiple exact projects matched")

    monkeypatch.setattr(sdk, "_find_project", find_project)

    with pytest.raises(ConflictingIdentityError, match="multiple"):
        await sdk.find_project(name="Task Project")
