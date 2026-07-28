from __future__ import annotations

from pathlib import Path

import pytest

from playwright_api import ProjectMemoryScope, ProjectRef
from playwright_api.errors import ConflictingIdentityError, InvalidInputError
from playwright_api.projects import ProjectRegistry, select_exact_project


def project(project_id: str, name: str = "Task Project") -> ProjectRef:
    return ProjectRef(
        project_id=project_id,
        canonical_url=f"https://chatgpt.com/g/{project_id}/project",
        name=name,
        memory_scope=ProjectMemoryScope.DEFAULT,
    )


def test_project_ref_requires_exact_g_p_identity() -> None:
    ref = project("g-p-project123")
    assert ref.target.project_id == "g-p-project123"
    with pytest.raises(InvalidInputError):
        project("project123")


def test_exact_project_matching_is_zero_one_or_fail_closed() -> None:
    assert select_exact_project([], name="Task Project") is None
    expected = project("g-p-project123")
    assert select_exact_project([expected], name="Task Project") == expected
    with pytest.raises(ConflictingIdentityError, match="multiple"):
        select_exact_project(
            [expected, project("g-p-project456")], name="Task Project"
        )


def test_project_registry_persists_uncertain_create_and_proven_identity(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path)
    registry.begin_creation(
        key="task-123",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )
    uncertain = registry.load("task-123")
    assert uncertain is not None
    assert uncertain.creation_unknown is True
    assert uncertain.project is None

    expected = project("g-p-project123")
    registry.resolve("task-123", expected)
    resolved = registry.load("task-123")
    assert resolved is not None
    assert resolved.creation_unknown is False
    assert resolved.project == expected


def test_project_registry_refuses_key_rebinding(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path)
    registry.begin_creation(
        key="task-123",
        name="Task Project",
        memory_scope=ProjectMemoryScope.DEFAULT,
    )
    with pytest.raises(ConflictingIdentityError, match="different"):
        registry.begin_creation(
            key="task-123",
            name="Other Name",
            memory_scope=ProjectMemoryScope.DEFAULT,
        )
