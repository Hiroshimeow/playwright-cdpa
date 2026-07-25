from __future__ import annotations

from typing import Any


def message(
    message_id: str,
    role: str,
    parent: str | None,
    *,
    text: str = "",
    turn: str | None = "turn-1",
    request: str | None = "req-1",
    status: str = "finished_successfully",
    recipient: str = "all",
    content_type: str = "text",
) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if turn is not None:
        metadata["turn_exchange_id"] = turn
    if request is not None:
        metadata["request_id"] = request
    return message_id, {
        "id": message_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": message_id,
            "author": {"role": role},
            "recipient": recipient,
            "status": status,
            "content": {"content_type": content_type, "parts": [text]},
            "metadata": metadata,
        },
    }


def graph(*nodes: tuple[str, dict[str, Any]], current: str) -> dict[str, Any]:
    mapping = {key: value for key, value in nodes}
    for key, node in mapping.items():
        parent = node.get("parent")
        if parent in mapping:
            mapping[parent]["children"].append(key)
    return {"mapping": mapping, "current_node": current}
