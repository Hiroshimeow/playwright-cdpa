from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .errors import ConflictingIdentityError, IdentityMissingError
from .models import TurnIdentity
from .redaction import redact

_TERMINAL_STATUSES = {
    "finished_successfully",
    "complete",
    "completed",
    "success",
}
_FAILURE_STATUSES = {"failed", "error", "cancelled", "canceled"}
_FINAL_CONTENT_TYPES = {"text", "multimodal_text"}


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
    encoded = json.dumps(
        redact(safe), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def graph_fingerprints(conversation: dict[str, Any]) -> dict[str, str]:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return {}
    return {
        str(node_id): fingerprint_node(node)
        for node_id, node in mapping.items()
        if isinstance(node, dict)
    }


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
        if not self.identity.monitorable:
            raise IdentityMissingError(
                "conversation_id, exact user_message_id, and turn correlation are required"
            )
        mapping = conversation.get("mapping")
        current = conversation.get("current_node")
        if not isinstance(mapping, dict) or not isinstance(current, str) or current not in mapping:
            raise IdentityMissingError("conversation graph has no valid current branch")
        chain = self._current_chain(mapping, current)
        try:
            user_index = chain.index(self.identity.user_message_id or "")
        except ValueError as exc:
            raise IdentityMissingError(
                "submitted user node is absent from the current branch"
            ) from exc
        user_node = mapping[self.identity.user_message_id or ""]
        if not isinstance(user_node, dict):
            raise IdentityMissingError("submitted user node is malformed")
        parent = user_node.get("parent")
        if self.identity.parent_message_id is not None and str(parent) != self.identity.parent_message_id:
            raise ConflictingIdentityError(
                "submitted user node parent does not match durable identity"
            )
        if (
            self.identity.pre_send_current_node is not None
            and str(parent) != self.identity.pre_send_current_node
        ):
            raise ConflictingIdentityError(
                "submitted user node does not descend from pre-Send anchor"
            )

        exact_nodes = chain[user_index:]
        self._validate_chain_correlation(mapping, exact_nodes)
        self._validate_tool_state(mapping, exact_nodes)

        selected: GraphCandidate | None = None
        for node_id in reversed(exact_nodes[1:]):
            raw_node = mapping.get(node_id)
            if not isinstance(raw_node, dict):
                continue
            message = raw_node.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            role = str(author.get("role") or "") if isinstance(author, dict) else ""
            recipient = str(message.get("recipient") or "all")
            content = message.get("content")
            content_type = (
                str(content.get("content_type") or "") if isinstance(content, dict) else ""
            )
            status = str(message.get("status") or "").casefold()
            text = content_text(content)
            if (
                role == "assistant"
                and recipient in {"", "all"}
                and content_type in _FINAL_CONTENT_TYPES
                and text
                and status in _TERMINAL_STATUSES
            ):
                selected = GraphCandidate(
                    message_id=str(message.get("id") or node_id),
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

    def observe_fingerprints(self, conversation: dict[str, Any]) -> dict[str, str]:
        return graph_fingerprints(conversation)

    def _validate_chain_correlation(
        self, mapping: dict[str, Any], exact_nodes: list[str]
    ) -> None:
        positive = False
        for node_id in exact_nodes:
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            metadata = (
                message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            )
            for field in ("turn_exchange_id", "request_id", "working_turn_id"):
                expected = getattr(self.identity, field)
                present = metadata.get(field)
                if expected is not None and present is not None:
                    if str(present) != expected:
                        raise ConflictingIdentityError(
                            f"exact chain {field} conflicts with canonical graph identity"
                        )
                    positive = True
        if not positive:
            raise IdentityMissingError("exact chain has no positive turn correlation")

    def _validate_tool_state(self, mapping: dict[str, Any], exact_nodes: list[str]) -> None:
        unresolved_tool = False
        for node_id in exact_nodes[1:]:
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            author = message.get("author")
            role = str(author.get("role") or "") if isinstance(author, dict) else ""
            recipient = str(message.get("recipient") or "all")
            status = str(message.get("status") or "").casefold()
            if role == "assistant" and recipient not in {"", "all"}:
                unresolved_tool = status not in _TERMINAL_STATUSES | _FAILURE_STATUSES
            elif role == "tool":
                unresolved_tool = status not in _TERMINAL_STATUSES | _FAILURE_STATUSES
            elif role == "assistant" and recipient in {"", "all"}:
                unresolved_tool = False
        if unresolved_tool:
            raise IdentityMissingError("exact tool chain is not terminal")

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
            parent = node.get("parent")
            node_id = str(parent) if parent is not None else None
        reverse.reverse()
        return reverse
