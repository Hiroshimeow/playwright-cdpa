from __future__ import annotations

import pytest

from playwright_gpt_core.errors import InvalidInputError
from playwright_gpt_core.targets import normalize_conversation


@pytest.mark.parametrize(
    "value",
    [
        "http://chatgpt.com/c/conversation-1",
        "https://chatgpt.com:444/c/conversation-1",
        "https://user:pass@chatgpt.com/c/conversation-1",
        "https://chatgpt.com/c/conversation-1?foreign=1",
        "https://chatgpt.com/c/conversation-1#foreign",
        "https://chatgpt.com/c/conversation-1/",
        "https://chatgpt.example/c/conversation-1",
        "https://chatgpt.com:bad/c/conversation-1",
    ],
)
def test_normalize_conversation_rejects_noncanonical_url(value: str) -> None:
    with pytest.raises(InvalidInputError):
        normalize_conversation(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://chatgpt.com/c/conversation-1",
        "https://www.chatgpt.com/c/conversation-1",
        "https://chatgpt.com:443/c/conversation-1",
    ],
)
def test_normalize_conversation_accepts_canonical_https_url(value: str) -> None:
    assert normalize_conversation(value) == "conversation-1"
