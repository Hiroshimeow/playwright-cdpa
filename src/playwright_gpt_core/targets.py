from __future__ import annotations

import re
from urllib.parse import urlparse

from .errors import InvalidInputError

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


def normalize_conversation(value: str) -> str:
    if type(value) is not str:
        raise InvalidInputError("conversation target must be exactly a string")
    raw = value.strip()
    if not raw:
        raise InvalidInputError("conversation target is empty")
    if "://" not in raw:
        conversation_id = raw.strip("/")
    else:
        parsed = urlparse(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise InvalidInputError("conversation URL has an invalid port") from exc
        supported_origin = (
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        )
        if not supported_origin:
            raise InvalidInputError("conversation URL must use exact HTTPS ChatGPT origin")
        if parsed.params or parsed.query or parsed.fragment:
            raise InvalidInputError(
                "conversation URL must not contain params, query, or fragment"
            )
        match = re.fullmatch(r"/c/([A-Za-z0-9_-]{8,160})", parsed.path)
        if match is None:
            raise InvalidInputError("conversation URL must be https://chatgpt.com/c/<id>")
        conversation_id = match.group(1)
    if not _CONVERSATION_ID.fullmatch(conversation_id):
        raise InvalidInputError("invalid conversation ID")
    return conversation_id


def conversation_url(conversation_id: str) -> str:
    return f"https://chatgpt.com/c/{conversation_id}"
