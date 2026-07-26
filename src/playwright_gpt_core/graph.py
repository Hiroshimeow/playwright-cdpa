from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .errors import (
    ConflictingIdentityError,
    IdentityMissingError,
    SchemaDriftError,
)
from .models import TurnIdentity
from .schema import decode_identifier, decode_optional_identifier

_TERMINAL_STATUSES = {
    "finished_successfully",
    "complete",
    "completed",
    "success",
}
_FAILURE_STATUSES = {"failed", "error", "cancelled", "canceled"}
_FINAL_CONTENT_TYPES = {"text", "multimodal_text"}
_CORRELATION_FIELDS = ("turn_exchange_id", "request_id", "working_turn_id")


def _graph_identifier(
    value: Any,
    field: str,
    *,
    optional: bool = False,
    max_length: int = 512,
) -> str | None:
    try:
        if optional:
            return decode_optional_identifier(value, field, max_length=max_length)
        return decode_identifier(value, field, max_length=max_length)
    except ValueError as exc:
        raise SchemaDriftError(str(exc)) from exc


def _message_role(message: dict[str, Any]) -> str:
    author = message.get("author")
    if not isinstance(author, dict):
        raise SchemaDriftError("message author must be an object")
    role = _graph_identifier(author.get("role"), "message author role", max_length=80)
    assert role is not None
    return role


def _assistant_recipient(message: dict[str, Any], role: str) -> str | None:
    if role != "assistant":
        return None
    recipient = _graph_identifier(
        message.get("recipient"), "assistant recipient", max_length=160
    )
    assert recipient is not None
    return recipient


def _message_id(message: dict[str, Any]) -> str:
    value = _graph_identifier(message.get("id"), "message id")
    assert value is not None
    return value


def _parent_id(node: dict[str, Any]) -> str | None:
    return _graph_identifier(node.get("parent"), "graph parent", optional=True)


def _metadata_identifier(metadata: dict[str, Any], field: str) -> str | None:
    if field not in metadata or metadata[field] is None:
        return None
    return _graph_identifier(metadata[field], field)


def content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    output: list[str] = []
    for part in parts:
        if isinstance(part, str):
            output.append(part)
        elif isinstance(part, dict):
            for key in ("text", "content", "value"):
                item = part.get(key)
                if isinstance(item, str):
                    output.append(item)
                    break
    return "\n".join(output).strip()


def fingerprint_node(node: dict[str, Any]) -> str:
    message = node.get("message") if isinstance(node.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    safe = {
        "id": message.get("id") or node.get("id"),
        "parent": node.get("parent"),
        "children": node.get("children"),
        "author": message.get("author"),
        "recipient": message.get("recipient"),
        "status": message.get("status"),
        "content": message.get("content"),
        "metadata": {
            key: metadata.get(key)
            for key in (
                "turn_exchange_id",
                "request_id",
                "working_turn_id",
                "model_slug",
                "resolved_model_slug",
                "reasoning_status",
                "is_complete",
                "tool_name",
                "invoked_plugin",
                "invoked_resource",
            )
            if key in metadata
        },
    }
    # Convergence hashes exact allowlisted in-memory graph material. Redacting
    # before hashing would collapse distinct mutable responses to one digest.
    # Only the resulting digest may leave this boundary.
    encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(conversation: dict[str, Any]) -> dict[str, Any]:
    raw = conversation.get("mapping")
    if not isinstance(raw, dict):
        raise SchemaDriftError("conversation mapping must be an object")
    for node_id, node in raw.items():
        _graph_identifier(node_id, "graph node id")
        if not isinstance(node, dict):
            raise SchemaDriftError("conversation mapping node must be an object")
    return raw


def graph_fingerprints(conversation: dict[str, Any]) -> dict[str, str]:
    mapping = _mapping(conversation)
    return {node_id: fingerprint_node(node) for node_id, node in mapping.items()}


@dataclass(frozen=True, slots=True)
class GraphCandidate:
    message_id: str
    text: str
    fingerprint: str
    chain_fingerprint: str
    status: str


class GraphResolver:
    def __init__(self, identity: TurnIdentity) -> None:
        self.identity = identity

    def resolve(self, conversation: dict[str, Any]) -> GraphCandidate:
        mapping, exact_nodes = self.validate_exact_branch(conversation)
        self._validate_tool_state(mapping, exact_nodes)

        selected: GraphCandidate | None = None
        for node_id in reversed(exact_nodes[1:]):
            raw_node = mapping[node_id]
            message = raw_node.get("message")
            if not isinstance(message, dict):
                continue
            role = _message_role(message)
            recipient = _assistant_recipient(message, role)
            content = message.get("content")
            content_type = ""
            if isinstance(content, dict) and content.get("content_type") is not None:
                decoded = _graph_identifier(
                    content.get("content_type"), "content_type", max_length=160
                )
                content_type = decoded or ""
            status = ""
            if message.get("status") is not None:
                decoded_status = _graph_identifier(
                    message.get("status"), "message status", max_length=160
                )
                status = (decoded_status or "").casefold()
            text = content_text(content)
            if (
                role == "assistant"
                and recipient == "all"
                and content_type in _FINAL_CONTENT_TYPES
                and text
                and status in _TERMINAL_STATUSES
            ):
                selected = GraphCandidate(
                    message_id=_message_id(message),
                    text=text,
                    fingerprint=fingerprint_node(raw_node),
                    chain_fingerprint=self._chain_fingerprint(mapping, exact_nodes),
                    status=status,
                )
                break
        if selected is None:
            raise IdentityMissingError(
                "no exact terminal assistant candidate on the current branch"
            )
        return selected

    def validate_exact_branch(
        self, conversation: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        if not self.identity.monitorable:
            raise IdentityMissingError(
                "conversation_id, exact user_message_id, and graph correlation are required"
            )
        mapping = _mapping(conversation)
        current = _graph_identifier(conversation.get("current_node"), "current_node")
        assert current is not None
        if current not in mapping:
            raise IdentityMissingError("conversation graph current_node is absent")
        chain = self._current_chain(mapping, current)
        try:
            user_index = chain.index(self.identity.user_message_id or "")
        except ValueError as exc:
            raise IdentityMissingError(
                "submitted user node is absent from the current branch"
            ) from exc
        user_node = mapping[self.identity.user_message_id or ""]
        parent = _parent_id(user_node)
        if (
            self.identity.parent_message_id is not None
            and parent != self.identity.parent_message_id
        ):
            raise ConflictingIdentityError(
                "submitted user node parent does not match durable identity"
            )
        if (
            self.identity.pre_send_current_node is not None
            and parent != self.identity.pre_send_current_node
        ):
            raise ConflictingIdentityError(
                "submitted user node does not descend from pre-Send anchor"
            )
        exact_nodes = chain[user_index:]
        self._validate_chain_correlation(mapping, exact_nodes)
        return mapping, exact_nodes

    def observe_fingerprints(self, conversation: dict[str, Any]) -> dict[str, str]:
        return graph_fingerprints(conversation)

    def _validate_chain_correlation(
        self, mapping: dict[str, Any], exact_nodes: list[str]
    ) -> None:
        segment: dict[str, str | None] = {
            field: getattr(self.identity, field) for field in _CORRELATION_FIELDS
        }
        positive = False

        for index, node_id in enumerate(exact_nodes):
            node = mapping[node_id]
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            message_id = _message_id(message)
            if message_id != node_id:
                raise SchemaDriftError("message id does not match graph node id")
            role = _message_role(message)
            _assistant_recipient(message, role)
            raw_metadata = message.get("metadata")
            if raw_metadata is None:
                metadata: dict[str, Any] = {}
            elif not isinstance(raw_metadata, dict):
                raise SchemaDriftError("message metadata must be an object")
            else:
                metadata = raw_metadata

            if index > 0 and role == "user":
                parent_id = _parent_id(node)
                parent = mapping.get(parent_id) if parent_id is not None else None
                parent_message = parent.get("message") if isinstance(parent, dict) else None
                if not isinstance(parent_message, dict):
                    raise SchemaDriftError("internal user parent message is malformed")
                parent_role = _message_role(parent_message)
                if parent_role != "tool":
                    raise ConflictingIdentityError(
                        "a later user node is not a tool-parented internal continuation"
                    )
                next_segment = {
                    field: _metadata_identifier(metadata, field)
                    for field in _CORRELATION_FIELDS
                }
                if not any(next_segment.values()):
                    raise IdentityMissingError(
                        "tool-parented internal continuation has no graph correlation"
                    )
                segment = next_segment
                positive = True
                continue

            node_positive = False
            for field in _CORRELATION_FIELDS:
                expected = segment.get(field)
                present = _metadata_identifier(metadata, field)
                if expected is None or present is None:
                    continue
                if present != expected:
                    raise ConflictingIdentityError(
                        f"exact graph segment {field} conflicts with its segment identity"
                    )
                node_positive = True
                positive = True
            if index == 0 and not node_positive:
                raise IdentityMissingError(
                    "submitted user node has no positive canonical graph correlation"
                )

        if not positive:
            raise IdentityMissingError("exact branch has no positive graph correlation")

    def _validate_tool_state(self, mapping: dict[str, Any], exact_nodes: list[str]) -> None:
        pending_tool_calls = 0
        for node_id in exact_nodes[1:]:
            node = mapping[node_id]
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            role = _message_role(message)
            recipient = _assistant_recipient(message, role)
            status = ""
            if message.get("status") is not None:
                decoded = _graph_identifier(
                    message.get("status"), "message status", max_length=160
                )
                status = (decoded or "").casefold()
            if role == "assistant" and recipient != "all":
                if status not in _FAILURE_STATUSES:
                    pending_tool_calls += 1
            elif role == "tool":
                if status not in _TERMINAL_STATUSES | _FAILURE_STATUSES:
                    raise IdentityMissingError("exact tool result is not terminal")
                if pending_tool_calls > 0:
                    pending_tool_calls -= 1
        if pending_tool_calls:
            raise IdentityMissingError(
                f"exact tool chain has {pending_tool_calls} unresolved call(s)"
            )

    @staticmethod
    def _chain_fingerprint(mapping: dict[str, Any], exact_nodes: list[str]) -> str:
        encoded = json.dumps(
            [(node_id, fingerprint_node(mapping[node_id])) for node_id in exact_nodes],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _current_chain(mapping: dict[str, Any], current: str) -> list[str]:
        reverse: list[str] = []
        seen: set[str] = set()
        node_id: str | None = current
        while node_id is not None:
            if node_id in seen:
                raise IdentityMissingError("cycle in conversation graph")
            seen.add(node_id)
            reverse.append(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                raise IdentityMissingError("current branch references missing node")
            node_id = _parent_id(node)
        reverse.reverse()
        return reverse
