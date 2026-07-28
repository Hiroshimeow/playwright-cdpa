from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import AmbiguousOutcomeError, BackendError, SchemaDriftError
from .frontend import click_send_atomic
from .models import TurnIdentity
from .schema import decode_identifier, decode_optional_identifier
from .targets import ChatTarget

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


def _transport_string(
    value: Any,
    field: str,
    *,
    optional: bool = True,
    max_length: int = 512,
) -> str | None:
    try:
        if optional:
            return decode_optional_identifier(value, field, max_length=max_length)
        return decode_identifier(value, field, max_length=max_length)
    except ValueError as exc:
        raise SchemaDriftError(str(exc)) from exc


def is_real_conversation_response(response: Any) -> bool:
    try:
        parsed = urlparse(str(response.url))
        port = parsed.port
        return (
            response.request.method == "POST"
            and parsed.scheme == "https"
            and (parsed.hostname or "").casefold() in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path == "/backend-api/f/conversation"
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
    except (AttributeError, TypeError, ValueError):
        return False


def reduce_request_payload(request: Any) -> FrontendAcceptance:
    try:
        value = json.loads(request.post_data or "{}")
    except (json.JSONDecodeError, TypeError):
        value = {}
    payload = value if isinstance(value, dict) else {}
    raw_metadata = payload.get("metadata")
    if raw_metadata is None:
        metadata: dict[str, Any] = {}
    elif not isinstance(raw_metadata, dict):
        raise SchemaDriftError("frontend request metadata must be an object")
    else:
        metadata = raw_metadata
    request_id = _transport_string(metadata.get("request_id"), "request_id")
    user_message_id = None
    messages = payload.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise SchemaDriftError("frontend request messages must be an array")
    if isinstance(messages, list):
        user_candidates: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                raise SchemaDriftError("frontend request message must be an object")
            author = item.get("author")
            if not isinstance(author, dict):
                raise SchemaDriftError("frontend request message author must be an object")
            role = _transport_string(
                author.get("role"),
                "frontend request author role",
                optional=False,
                max_length=80,
            )
            candidate = _transport_string(item.get("id"), "message id")
            if role == "user" and candidate:
                user_candidates.append(candidate)
        if len(user_candidates) == 1:
            user_message_id = user_candidates[0]
        elif len(user_candidates) > 1:
            raise SchemaDriftError("frontend request has multiple user message IDs")
    return FrontendAcceptance(
        http_status=0,
        request_id=request_id,
        user_message_id=user_message_id,
        frontend_parent_message_id=_transport_string(
            payload.get("parent_message_id"), "parent_message_id"
        ),
        model=_transport_string(payload.get("model"), "model", max_length=160),
        thinking_effort=_transport_string(
            payload.get("thinking_effort"), "thinking_effort", max_length=160
        ),
    )


def reduce_handoff(text: str, acceptance: FrontendAcceptance) -> FrontendHandoff:
    accepted_request_id = _transport_string(acceptance.request_id, "request_id")
    accepted_user_id = _transport_string(acceptance.user_message_id, "message id")
    accepted_parent_id = _transport_string(
        acceptance.frontend_parent_message_id, "parent_message_id"
    )
    events = parse_sse_events(text)
    conversation_id: str | None = None
    turn_exchange_id: str | None = None
    stream_topic_id: str | None = None
    event_types: list[str] = []
    for event in events:
        raw_type = event.get("type")
        if raw_type is None:
            event_type = "event"
        else:
            event_type = (
                _transport_string(raw_type, "event type", optional=False, max_length=80)
                or "event"
            )
        event_types.append(event_type)
        conversation_id = _merge_optional(
            "conversation_id",
            conversation_id,
            _transport_string(event.get("conversation_id"), "conversation_id"),
        )
        turn_exchange_id = _merge_optional(
            "turn_exchange_id",
            turn_exchange_id,
            _transport_string(event.get("turn_exchange_id"), "turn_exchange_id"),
        )
        options = event.get("options")
        if options is not None and not isinstance(options, list):
            raise SchemaDriftError("frontend response options must be an array")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    raise SchemaDriftError("frontend response option must be an object")
                stream_topic_id = _merge_optional(
                    "stream_topic_id",
                    stream_topic_id,
                    _transport_string(option.get("topic_id"), "stream_topic_id"),
                )
    if not conversation_id:
        raise SchemaDriftError("frontend response contained no conversation_id")
    identity = TurnIdentity(
        conversation_id=conversation_id,
        transport_turn_exchange_id=turn_exchange_id,
        transport_request_id=accepted_request_id,
        stream_topic_id=stream_topic_id,
        user_message_id=accepted_user_id,
        frontend_parent_message_id=accepted_parent_id,
        sources={
            key: source
            for key, source, value in (
                ("conversation_id", "frontend-response", conversation_id),
                ("transport_turn_exchange_id", "frontend-response", turn_exchange_id),
                ("stream_topic_id", "frontend-response", stream_topic_id),
                ("transport_request_id", "frontend-request", accepted_request_id),
                ("user_message_id", "frontend-request", accepted_user_id),
                ("frontend_parent_message_id", "frontend-request", accepted_parent_id),
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
    prompt: str,
    target: ChatTarget,
    expected_attachment_names: dict[str, int] | None,
    send_timeout: float,
    on_accepted: AcceptedCallback,
) -> FrontendHandoff:
    try:
        async with page.expect_response(
            is_real_conversation_response, timeout=send_timeout * 1000
        ) as response_info:
            await click_send_atomic(
                page,
                prompt,
                target=target,
                expected_attachment_names=expected_attachment_names,
            )
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
