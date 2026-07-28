from __future__ import annotations

import pytest

from playwright_api import ChatTarget, TargetKind
from playwright_api.errors import InvalidInputError


@pytest.mark.parametrize(
    ("value", "kind", "project_id", "conversation_id", "url", "coordination_key"),
    [
        ("/", TargetKind.FRESH, None, None, "https://chatgpt.com/", "fresh"),
        (
            "/c/conversation-123",
            TargetKind.CONVERSATION,
            None,
            "conversation-123",
            "https://chatgpt.com/c/conversation-123",
            "conversation:conversation-123",
        ),
        (
            "/g/g-p-project123/project",
            TargetKind.PROJECT,
            "g-p-project123",
            None,
            "https://chatgpt.com/g/g-p-project123/project",
            "project:g-p-project123:root",
        ),
        (
            "/g/g-p-project123/c/conversation-123",
            TargetKind.PROJECT_CONVERSATION,
            "g-p-project123",
            "conversation-123",
            "https://chatgpt.com/g/g-p-project123/c/conversation-123",
            "project:g-p-project123:conversation:conversation-123",
        ),
    ],
)
def test_target_model_preserves_exact_identity(
    value, kind, project_id, conversation_id, url, coordination_key
) -> None:
    target = ChatTarget.parse(value)
    assert target.kind is kind
    assert target.project_id == project_id
    assert target.conversation_id == conversation_id
    assert target.canonical_url == url
    assert target.coordination_key == coordination_key


@pytest.mark.parametrize(
    "value",
    [
        "http://chatgpt.com/c/conversation-123",
        "https://user:pass@chatgpt.com/c/conversation-123",
        "https://chatgpt.com/c/conversation-123?x=1",
        "https://chatgpt.com/c/conversation-123#x",
        "https://chatgpt.com:444/c/conversation-123",
        "https://example.com/c/conversation-123",
        "/g/g-p-project123/c/",
        "/g/project123/project",
        "/c/short",
        "/projects",
    ],
)
def test_target_model_rejects_ambiguous_or_noncanonical_input(value: str) -> None:
    with pytest.raises(InvalidInputError):
        ChatTarget.parse(value)


def test_target_constructors_are_canonical() -> None:
    assert ChatTarget.fresh() == ChatTarget.parse("/")
    assert ChatTarget.conversation("conversation-123") == ChatTarget.parse(
        "/c/conversation-123"
    )
    assert ChatTarget.project("g-p-project123") == ChatTarget.parse(
        "/g/g-p-project123/project"
    )
    assert ChatTarget.project_conversation(
        "g-p-project123", "conversation-123"
    ) == ChatTarget.parse("/g/g-p-project123/c/conversation-123")
