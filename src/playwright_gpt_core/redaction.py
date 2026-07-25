from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_REDACTED = "<redacted>"
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|cookies|password|passwd|secret|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|resume(?:[_-]conversation)?[_-]?token|"
    r"sentinel|turnstile|proof(?:[_-]?token|[_-]?material)?|session[_-]?token|token)(?:$|[_-])",
    re.IGNORECASE,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{3,}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{3,}")
_COOKIE_VALUE = re.compile(r"(?i)(?:^|;\s*)(?:__secure-|__host-)?[A-Za-z0-9_.-]+=\S+")


def _secret_key(key: str) -> bool:
    lowered = key.casefold()
    return bool(_SECRET_KEY.search(lowered)) or lowered.endswith("_token") or lowered == "token"


def _redact_string(value: str) -> str:
    if _BEARER.search(value) or _JWT.search(value):
        return _REDACTED
    if len(value) > 8192:
        value = value[:8192] + f"...<truncated:{len(value) - 8192}>"
    if _COOKIE_VALUE.search(value) and (";" in value or value.casefold().startswith("cookie")):
        return _REDACTED
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return "<max-depth>"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            output[key] = _REDACTED if _secret_key(key) else redact(raw_value, _depth=_depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, _depth=_depth + 1) for item in list(value)[:500]]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value))


def safe_json_dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        redact(value),
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
    )
