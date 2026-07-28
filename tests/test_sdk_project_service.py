from __future__ import annotations

from pathlib import Path

import pytest

from playwright_api import ChatGPTClient, ClientConfig, ProjectMemoryScope, ProjectRef
from playwright_api.errors import AmbiguousOutcomeError, ConflictingIdentityError


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

    async def list_projects():
        return []

    async def create_project(name, memory_scope):
        record = sdk.projects.load("task-123")
        observed_unknown.append(record is not None and record.creation_unknown)
        assert name == "Task Project"
        assert memory_scope is ProjectMemoryScope.DEFAULT
        return expected

    monkeypatch.setattr(sdk, "_list_projects", list_projects)
    monkeypatch.setattr(sdk, "_create_project", create_project)

    first = await sdk.ensure_project("task-123", "Task Project")
    second = await sdk.ensure_project("task-123", "Task Project")

    assert first == expected
    assert second == expected
    assert observed_unknown == [True]


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

    async def list_projects():
        return [expected]

    async def create_project(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("uncertain creation must reconcile, not click Create again")

    monkeypatch.setattr(sdk, "_list_projects", list_projects)
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

    async def list_projects():
        return []

    monkeypatch.setattr(sdk, "_list_projects", list_projects)

    with pytest.raises(AmbiguousOutcomeError, match="unknown"):
        await sdk.ensure_project("task-123", "Task Project")


@pytest.mark.asyncio
async def test_find_project_fails_closed_on_duplicate_exact_name(
    tmp_path: Path, monkeypatch
) -> None:
    sdk = client(tmp_path)

    async def list_projects():
        return [project("g-p-project123"), project("g-p-project456")]

    monkeypatch.setattr(sdk, "_list_projects", list_projects)

    with pytest.raises(ConflictingIdentityError, match="multiple"):
        await sdk.find_project(name="Task Project")
