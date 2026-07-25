from __future__ import annotations

from typing import Any

from .errors import AmbiguousIdentityError, ConflictingIdentityError, IdentityMissingError
from .graph import content_text
from .models import TurnIdentity

_FIELDS = (
    "conversation_id",
    "transport_turn_exchange_id",
    "transport_request_id",
    "stream_topic_id",
    "turn_exchange_id",
    "request_id",
    "working_turn_id",
    "user_message_id",
    "frontend_parent_message_id",
    "parent_message_id",
    "pre_send_current_node",
)


def merge_identity(base: TurnIdentity, incoming: TurnIdentity, *, source: str) -> TurnIdentity:
    values: dict[str, object] = {}
    sources = dict(base.sources)
    for field in _FIELDS:
        old = getattr(base, field)
        new = getattr(incoming, field)
        if old is not None and new is not None and old != new:
            raise ConflictingIdentityError(f"conflicting {field}: {old!r} != {new!r}")
        value = old if old is not None else new
        values[field] = value
        if new is not None:
            sources.setdefault(field, source)
    values["sources"] = sources
    return TurnIdentity(**values)


def discover_user_identity(
    graph: dict[str, Any],
    identity: TurnIdentity,
    *,
    baseline_node_ids: set[str],
    prompt: str | None = None,
) -> TurnIdentity:
    mapping = graph.get("mapping")
    current = graph.get("current_node")
    if not isinstance(mapping, dict) or not isinstance(current, str) or current not in mapping:
        raise IdentityMissingError("graph has no valid current branch")
    chain = _current_chain(mapping, current)
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for node_id in chain:
        if node_id in baseline_node_ids:
            continue
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        role = str(author.get("role") or "") if isinstance(author, dict) else ""
        if role != "user":
            continue
        message_id = str(message.get("id") or node_id)
        if identity.user_message_id and message_id != identity.user_message_id:
            continue
        if prompt is not None and content_text(message.get("content")) != prompt:
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        candidates.append((message_id, node, metadata))
    if not candidates:
        raise IdentityMissingError("submitted user node not found on current branch")
    if len(candidates) != 1:
        raise AmbiguousIdentityError(
            f"multiple submitted user candidates: {[candidate[0] for candidate in candidates]}"
        )
    message_id, node, metadata = candidates[0]
    parent = node.get("parent")
    if identity.pre_send_current_node is not None and str(parent) != identity.pre_send_current_node:
        raise ConflictingIdentityError(
            "submitted user node does not descend from pre-Send graph anchor"
        )
    graph_identity = TurnIdentity(
        user_message_id=message_id,
        parent_message_id=str(parent) if parent is not None else None,
        turn_exchange_id=_optional_string(metadata.get("turn_exchange_id")),
        request_id=_optional_string(metadata.get("request_id")),
        working_turn_id=_optional_string(metadata.get("working_turn_id")),
    )
    if not graph_identity.has_graph_correlation:
        raise IdentityMissingError("submitted user node has no graph turn correlation")
    return merge_identity(identity, graph_identity, source="graph-user")


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _current_chain(mapping: dict[str, Any], current: str) -> list[str]:
    reverse: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current
    while node_id is not None:
        if node_id in seen:
            raise IdentityMissingError("cycle in conversation graph")
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            raise IdentityMissingError("current branch references missing node")
        reverse.append(node_id)
        parent = node.get("parent")
        node_id = str(parent) if parent is not None else None
    reverse.reverse()
    return reverse
