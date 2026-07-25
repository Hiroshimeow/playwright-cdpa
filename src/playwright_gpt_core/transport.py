from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .errors import AmbiguousOutcomeError, BackendError, SchemaDriftError
from .frontend import find_send_button
from .models import TurnIdentity

AcceptedCallback = Callable[["FrontendAcceptance"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FrontendAcceptance:
    http_status: int
    request_id: str | None
    user_message_id: str | None
    frontend_parent_message_id: str | None
    model: str | None
    thinking_effort: str | None


@dataclass(frozen=True, slots=True)
class FrontendHandoff:
    acceptance: FrontendAcceptance
    identity: TurnIdentity
    event_types: tuple[str, ...]
    response_bytes: int


def is_real_conversation_response(response: Any) -> bool:
    try:
        parsed = urlparse(str(response.url))
        return (
            response.request.method == "POST"
            and (parsed.hostname or "").casefold() in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.path == "/backend-api/f/conversation"
        )
    except Exception:
        return False


def reduce_request_payload(request: Any) -> FrontendAcceptance:
    try:
        value = json.loads(request.post_data or "{}")
    except Exception:
        value = {}
    payload = value if isinstance(value, dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    request_id = _string(metadata.get("request_id"))
    user_message_id = None
    messages = payload.get("messages")
    if isinstance(messages, list):
        user_candidates: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            role = str(author.get("role") or "") if isinstance(author, dict) else ""
            candidate = _string(item.get("id"))
            if role == "user" and candidate:
                user_candidates.append(candidate)
        if len(user_candidates) == 1:
            user_message_id = user_candidates[0]
    return FrontendAcceptance(
        http_status=0,
        request_id=request_id,
        user_message_id=user_message_id,
        frontend_parent_message_id=_string(payload.get("parent_message_id")),
        model=_string(payload.get("model")),
        thinking_effort=_string(payload.get("thinking_effort")),
    )


def reduce_handoff(text: str, acceptance: FrontendAcceptance) -> FrontendHandoff:
    events = parse_sse_events(text)
    conversation_id: str | None = None
    turn_exchange_id: str | None = None
    stream_topic_id: str | None = None
    event_types: list[str] = []
    for event in events:
        event_types.append(str(event.get("type") or "event")[:80])
        conversation_id = _merge_optional(
            "conversation_id", conversation_id, _string(event.get("conversation_id"))
        )
        turn_exchange_id = _merge_optional(
            "turn_exchange_id", turn_exchange_id, _string(event.get("turn_exchange_id"))
        )
        options = event.get("options")
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    stream_topic_id = _merge_optional(
                        "stream_topic_id",
                        stream_topic_id,
                        _string(option.get("topic_id")),
                    )
    if not conversation_id:
        raise SchemaDriftError("frontend response contained no conversation_id")
    identity = TurnIdentity(
        conversation_id=conversation_id,
        transport_turn_exchange_id=turn_exchange_id,
        transport_request_id=acceptance.request_id,
        stream_topic_id=stream_topic_id,
        user_message_id=acceptance.user_message_id,
        frontend_parent_message_id=acceptance.frontend_parent_message_id,
        sources={
            key: source
            for key, source, value in (
                ("conversation_id", "frontend-response", conversation_id),
                ("transport_turn_exchange_id", "frontend-response", turn_exchange_id),
                ("stream_topic_id", "frontend-response", stream_topic_id),
                ("transport_request_id", "frontend-request", acceptance.request_id),
                ("user_message_id", "frontend-request", acceptance.user_message_id),
                ("frontend_parent_message_id", "frontend-request", acceptance.frontend_parent_message_id),
            )
            if value is not None
        },
    )
    if not identity.has_transport_correlation:
        raise SchemaDriftError("frontend handoff has no turn-scoped correlation field")
    return FrontendHandoff(
        acceptance=acceptance,
        identity=identity,
        event_types=tuple(event_types[:100]),
        response_bytes=len(text.encode("utf-8")),
    )


async def send_real(
    page: Page,
    *,
    send_timeout: float,
    on_accepted: AcceptedCallback,
) -> FrontendHandoff:
    button = await find_send_button(page)
    try:
        async with page.expect_response(
            is_real_conversation_response, timeout=send_timeout * 1000
        ) as response_info:
            await button.click()
        response = await response_info.value
    except PlaywrightTimeoutError as exc:
        raise AmbiguousOutcomeError(
            "Send click crossed the irreversible boundary but no frontend response was observed"
        ) from exc
    acceptance = reduce_request_payload(response.request)
    acceptance = FrontendAcceptance(
        http_status=int(response.status),
        request_id=acceptance.request_id,
        user_message_id=acceptance.user_message_id,
        frontend_parent_message_id=acceptance.frontend_parent_message_id,
        model=acceptance.model,
        thinking_effort=acceptance.thinking_effort,
    )
    await on_accepted(acceptance)
    text = await response.text()
    if not response.ok:
        raise BackendError(f"real frontend Send failed with HTTP {response.status}")
    return reduce_handoff(text, acceptance)


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            value: Any = json.loads(raw)
            if isinstance(value, str):
                value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _merge_optional(name: str, old: str | None, new: str | None) -> str | None:
    if old is not None and new is not None and old != new:
        raise SchemaDriftError(f"frontend response contains conflicting {name}")
    return old if old is not None else new


def _string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
