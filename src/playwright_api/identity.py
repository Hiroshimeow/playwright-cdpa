from __future__ import annotations

from typing import Any

from .errors import (
    AmbiguousIdentityError,
    AmbiguousOutcomeError,
    ConflictingIdentityError,
    IdentityMissingError,
    SchemaDriftError,
)
from .graph import baseline_graph_fingerprints, content_text
from .models import TurnIdentity
from .schema import decode_identifier, decode_optional_identifier

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


def _graph_string(value: Any, field: str, *, optional: bool = False) -> str | None:
    try:
        if optional:
            return decode_optional_identifier(value, field)
        return decode_identifier(value, field)
    except ValueError as exc:
        raise SchemaDriftError(str(exc)) from exc


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
    baseline_node_fingerprints: dict[str, str],
    prompt: str | None = None,
) -> TurnIdentity:
    baseline_node_ids = set(baseline_node_fingerprints)
    if baseline_node_ids:
        if (
            identity.pre_send_current_node is not None
            and identity.pre_send_current_node not in baseline_node_ids
        ):
            raise AmbiguousOutcomeError(
                "persisted pre-Send graph baseline lacks the durable anchor fingerprint"
            )
        try:
            current_baseline = baseline_graph_fingerprints(graph, baseline_node_ids)
        except IdentityMissingError as exc:
            raise AmbiguousOutcomeError(
                "pre-Send graph baseline changed; structural recovery is unsafe"
            ) from exc
        if current_baseline != baseline_node_fingerprints:
            raise AmbiguousOutcomeError(
                "pre-Send graph baseline changed; structural recovery is unsafe"
            )

    mapping = graph.get("mapping")
    if not isinstance(mapping, dict):
        raise SchemaDriftError("graph mapping must be an object")
    for raw_node_id, raw_node in mapping.items():
        _graph_string(raw_node_id, "graph node id")
        if not isinstance(raw_node, dict):
            raise SchemaDriftError("graph node must be an object")
    current = _graph_string(graph.get("current_node"), "current_node")
    assert current is not None
    if current not in mapping:
        raise IdentityMissingError("graph has no valid current branch")
    chain = _current_chain(mapping, current)
    current_candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for node_id in chain:
        if node_id in baseline_node_ids:
            continue
        candidate = _user_candidate(node_id, mapping[node_id], identity, prompt=prompt)
        if candidate is not None:
            current_candidates.append(candidate)

    structural_recovery = bool(
        baseline_node_ids
        and identity.pre_send_current_node is not None
        and not identity.has_transport_correlation
        and identity.user_message_id is None
        and prompt is None
    )
    if structural_recovery:
        anchor = identity.pre_send_current_node
        direct_candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for node_id, node in mapping.items():
            if node_id in baseline_node_ids:
                continue
            candidate = _user_candidate(node_id, node, identity, prompt=None)
            if candidate is None:
                continue
            parent = _graph_string(node.get("parent"), "graph parent", optional=True)
            if parent == anchor:
                direct_candidates.append(candidate)
        if not direct_candidates:
            raise IdentityMissingError(
                "no unique post-baseline user node descends from the pre-Send anchor"
            )
        if len(direct_candidates) != 1:
            raise AmbiguousIdentityError(
                "multiple post-baseline user nodes descend from the pre-Send anchor"
            )
        if len(current_candidates) != 1:
            raise AmbiguousIdentityError(
                "structural recovery requires exactly one post-baseline user "
                "on the current branch"
            )
        if direct_candidates[0][0] != current_candidates[0][0]:
            raise AmbiguousIdentityError(
                "the unique post-baseline anchor child is not the unique current-branch user"
            )
        candidates = direct_candidates
    else:
        candidates = current_candidates
        if not candidates:
            raise IdentityMissingError("submitted user node not found on current branch")
        if len(candidates) != 1:
            raise AmbiguousIdentityError(
                "multiple submitted user candidates: "
                f"{[candidate[0] for candidate in candidates]}"
            )
    message_id, node, metadata = candidates[0]
    parent = _graph_string(node.get("parent"), "graph parent", optional=True)
    if identity.pre_send_current_node is not None and parent != identity.pre_send_current_node:
        raise ConflictingIdentityError(
            "submitted user node does not descend from pre-Send graph anchor"
        )
    graph_identity = TurnIdentity(
        user_message_id=message_id,
        parent_message_id=parent,
        turn_exchange_id=_metadata_string(metadata, "turn_exchange_id"),
        request_id=_metadata_string(metadata, "request_id"),
        working_turn_id=_metadata_string(metadata, "working_turn_id"),
    )
    if not graph_identity.has_graph_correlation:
        raise IdentityMissingError("submitted user node has no graph turn correlation")
    return merge_identity(identity, graph_identity, source="graph-user")


def _user_candidate(
    node_id: str,
    node: dict[str, Any],
    identity: TurnIdentity,
    *,
    prompt: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    message = node.get("message")
    if not isinstance(message, dict):
        return None
    author = message.get("author")
    if not isinstance(author, dict):
        raise SchemaDriftError("message author must be an object")
    role = _graph_string(author.get("role"), "message author role")
    if role != "user":
        return None
    message_id = _graph_string(message.get("id"), "message id")
    assert message_id is not None
    if message_id != node_id:
        raise SchemaDriftError("message id does not match graph node id")
    if identity.user_message_id and message_id != identity.user_message_id:
        return None
    if prompt is not None and content_text(message.get("content")) != prompt:
        return None
    raw_metadata = message.get("metadata")
    if raw_metadata is None:
        metadata: dict[str, Any] = {}
    elif not isinstance(raw_metadata, dict):
        raise SchemaDriftError("message metadata must be an object")
    else:
        metadata = raw_metadata
    return message_id, node, metadata


def _metadata_string(metadata: dict[str, Any], field: str) -> str | None:
    if field not in metadata or metadata[field] is None:
        return None
    return _graph_string(metadata[field], field)


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
        node_id = _graph_string(node.get("parent"), "graph parent", optional=True)
    reverse.reverse()
    return reverse
