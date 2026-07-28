from __future__ import annotations

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
