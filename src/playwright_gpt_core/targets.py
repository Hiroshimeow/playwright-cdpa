from __future__ import annotations

import re
from urllib.parse import urlparse

from .errors import InvalidInputError

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


def normalize_conversation(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise InvalidInputError("conversation target is empty")
    if "://" not in raw:
        conversation_id = raw.strip("/")
    else:
        parsed = urlparse(raw)
        if (parsed.hostname or "").casefold() not in {"chatgpt.com", "www.chatgpt.com"}:
            raise InvalidInputError("conversation URL must use chatgpt.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "c":
            raise InvalidInputError("conversation URL must be https://chatgpt.com/c/<id>")
        conversation_id = parts[1]
    if not _CONVERSATION_ID.fullmatch(conversation_id):
        raise InvalidInputError("invalid conversation ID")
    return conversation_id


def conversation_url(conversation_id: str) -> str:
    return f"https://chatgpt.com/c/{conversation_id}"
