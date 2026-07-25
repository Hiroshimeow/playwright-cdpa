from __future__ import annotations

from typing import Any

from .errors import AmbiguousIdentityError, ConflictingIdentityError, IdentityMissingError
from .graph import content_text
from .models import TurnIdentity

_FIELDS = (
    "conversation_id",
    "turn_exchange_id",
    "request_id",
    "stream_topic_id",
    "user_message_id",
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
    candidates: list[tuple[str, dict[str, Any]]] = []
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
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        _validate_present_correlation(metadata, identity)
        if prompt is not None and content_text(message.get("content")) != prompt:
            continue
        candidates.append((message_id, node))
    if not candidates:
        raise IdentityMissingError("submitted user node not found on current branch")
    if len(candidates) != 1:
        raise AmbiguousIdentityError(
            f"multiple submitted user candidates: {[candidate[0] for candidate in candidates]}"
        )
    message_id, node = candidates[0]
    parent = node.get("parent")
    if identity.parent_message_id is not None and str(parent) != identity.parent_message_id:
        raise ConflictingIdentityError("submitted user parent conflicts with transport identity")
    if identity.pre_send_current_node is not None and str(parent) != identity.pre_send_current_node:
        raise ConflictingIdentityError("submitted user node does not descend from pre-Send anchor")
    return merge_identity(
        identity,
        TurnIdentity(
            user_message_id=message_id,
            parent_message_id=str(parent) if parent is not None else None,
        ),
        source="graph-user",
    )


def _validate_present_correlation(metadata: dict[str, Any], identity: TurnIdentity) -> None:
    for field in ("turn_exchange_id", "request_id"):
        expected = getattr(identity, field)
        present = metadata.get(field)
        if expected is not None and present is not None and str(present) != expected:
            raise ConflictingIdentityError(f"user node {field} conflicts with transport identity")


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
