from __future__ import annotations

import json

import pytest

from playwright_api.errors import SchemaDriftError
from playwright_api.transport import (
    FrontendAcceptance,
    is_real_conversation_response,
    parse_sse_events,
    reduce_handoff,
    reduce_request_payload,
)


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.post_data = json.dumps(payload)


class FakeResponseRequest:
    def __init__(self, method: str) -> None:
        self.method = method


class FakeResponse:
    def __init__(self, url: str, method: str = "POST") -> None:
        self.url = url
        self.request = FakeResponseRequest(method)


@pytest.mark.parametrize(
    ("url", "method", "expected"),
    [
        ("https://chatgpt.com/backend-api/f/conversation", "POST", True),
        ("https://www.chatgpt.com:443/backend-api/f/conversation", "POST", True),
        ("http://chatgpt.com/backend-api/f/conversation", "POST", False),
        ("https://user:pass@chatgpt.com/backend-api/f/conversation", "POST", False),
        ("https://chatgpt.com:444/backend-api/f/conversation", "POST", False),
        ("https://chatgpt.com/backend-api/f/conversation;foreign", "POST", False),
        ("https://chatgpt.com/backend-api/f/conversation?foreign=1", "POST", False),
        ("https://chatgpt.com/backend-api/f/conversation#foreign", "POST", False),
        ("https://chatgpt.com/backend-api/f/conversation", "GET", False),
        ("https://chatgpt.com:bad/backend-api/f/conversation", "POST", False),
    ],
)
def test_real_conversation_response_requires_exact_https_endpoint(
    url: str, method: str, expected: bool
) -> None:
    assert is_real_conversation_response(FakeResponse(url, method)) is expected


def test_request_reduction_extracts_only_identity_fields() -> None:
    reduced = reduce_request_payload(
        FakeRequest(
            {
                "model": "gpt-test",
                "thinking_effort": "high",
                "parent_message_id": "parent-1",
                "metadata": {"request_id": "req-1", "secret_token": "do-not-copy"},
                "messages": [
                    {
                        "id": "user-1",
                        "author": {"role": "user"},
                        "content": {"parts": ["secret prompt"]},
                    }
                ],
            }
        )
    )
    assert reduced.request_id == "req-1"
    assert reduced.user_message_id == "user-1"
    assert reduced.frontend_parent_message_id == "parent-1"
    assert "secret" not in repr(reduced)


def test_handoff_reduction_requires_conversation_and_turn_correlation() -> None:
    acceptance = FrontendAcceptance(200, "req-1", "user-1", "parent-1", None, None)
    text = "\n".join(
        [
            'data: {"type":"message","conversation_id":"conv-1","turn_exchange_id":"turn-1"}',
            'data: {"type":"stream","options":[{"topic_id":"topic-1"}]}',
            "data: [DONE]",
        ]
    )
    handoff = reduce_handoff(text, acceptance)
    assert handoff.identity.conversation_id == "conv-1"
    assert handoff.identity.user_message_id == "user-1"
    assert handoff.event_types == ("message", "stream")


def test_conflicting_sse_identity_fails_closed() -> None:
    acceptance = FrontendAcceptance(200, "req-1", "user-1", None, None, None)
    text = "\n".join(
        [
            'data: {"conversation_id":"conv-a"}',
            'data: {"conversation_id":"conv-b"}',
        ]
    )
    with pytest.raises(SchemaDriftError):
        reduce_handoff(text, acceptance)


def test_sse_parser_ignores_malformed_and_done() -> None:
    assert parse_sse_events('data: nope\ndata: {"type":"ok"}\ndata: [DONE]') == [{"type": "ok"}]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", {"bad": True}),
        ("request_id", 123),
    ],
)
def test_request_reduction_rejects_malformed_identity(field, value) -> None:
    with pytest.raises(SchemaDriftError, match=field):
        reduce_request_payload(
            FakeRequest(
                {
                    "metadata": {field: value},
                    "messages": [{"id": "user-1", "author": {"role": "user"}}],
                }
            )
        )


def test_request_reduction_rejects_object_message_id() -> None:
    with pytest.raises(SchemaDriftError, match="message id"):
        reduce_request_payload(
            FakeRequest(
                {
                    "metadata": {"request_id": "request-1"},
                    "messages": [{"id": {"bad": True}, "author": {"role": "user"}}],
                }
            )
        )


def test_handoff_reduction_rejects_object_conversation_id() -> None:
    acceptance = FrontendAcceptance(200, "req-1", "user-1", None, None, None)
    with pytest.raises(SchemaDriftError, match="conversation_id"):
        reduce_handoff(
            'data: {"conversation_id":{"bad":true},"turn_exchange_id":"turn-1"}',
            acceptance,
        )
