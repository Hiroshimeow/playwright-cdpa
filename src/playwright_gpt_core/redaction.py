from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

_REDACTED = "<redacted>"
_MAX_DIAGNOSTIC = 8192
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_LABEL = re.compile(r"[^a-z0-9]+")
_KNOWN_SECRET_LABELS = {
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "privatekey",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "resume_token",
    "resumetoken",
    "resume_conversation_token",
    "resumeconversationtoken",
    "session_token",
    "sessiontoken",
    "signing_key",
    "signingkey",
    "proof",
    "proof_token",
    "prooftoken",
    "proof_material",
    "proofmaterial",
    "sentinel",
    "turnstile",
    "token",
}
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{3,}\b")
_AUTH_SCHEME = re.compile(
    r"(?i)\b(?P<prefix>authorization\s*[:=]\s*)?"
    r"(?P<scheme>bearer|basic)\s+(?P<value>[^\s,;]+)"
)
_ASSIGNMENT_CANDIDATE = re.compile(
    r"(?i)(?=(?<![A-Za-z0-9_-])"
    r"(?P<label>[A-Za-z][A-Za-z0-9_-]{0,80})"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;&]+))"
)
_COOKIE_VALUE = re.compile(r"(?i)(?:^|;\s*)(?:__secure-|__host-)?[A-Za-z0-9_.-]+=\S+")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PATH_SECRET_CONTEXTS = {
    "reset",
    "password_reset",
    "oauth",
    "magic_link",
    "signed_url",
    "webhook",
}
_PATH_SEPARATORS = "-_.:"
_PATH_PUBLIC_CONTEXTS = {
    "docs",
    "documentation",
    "example",
    "examples",
    "format",
    "guide",
    "help",
    "manual",
    "reference",
    "resource",
    "schema",
    "spec",
    "tutorial",
}


def _normalize_secret_label(value: str) -> tuple[str, str]:
    separated = _CAMEL_BOUNDARY.sub("_", value)
    normalized = _NON_LABEL.sub("_", separated.casefold()).strip("_")
    return normalized, normalized.replace("_", "")


def _secret_key(key: str) -> bool:
    normalized, compact = _normalize_secret_label(key)
    if normalized in _KNOWN_SECRET_LABELS or compact in _KNOWN_SECRET_LABELS:
        return True
    if normalized.endswith("_token") or compact.endswith("token"):
        return True
    if normalized.endswith(
        (
            "_secret",
            "_password",
            "_passwd",
            "_api_key",
            "_credential",
            "_credentials",
            "_private_key",
            "_signing_key",
        )
    ):
        return True
    if compact.endswith(
        (
            "secret",
            "password",
            "passwd",
            "apikey",
            "credential",
            "credentials",
            "privatekey",
            "signingkey",
        )
    ):
        return True
    return False


def _path_secret_marker(segment: str) -> bool:
    normalized, _compact = _normalize_secret_label(segment)
    return normalized in _PATH_SECRET_CONTEXTS or _secret_key(segment)


def _path_secret_marker_with_payload(segment: str) -> bool:
    boundaries: set[int] = set()
    for index, character in enumerate(segment):
        if index == 0:
            continue
        previous = segment[index - 1]
        if character in _PATH_SEPARATORS:
            boundaries.add(index)
        elif character.isupper() and (previous.islower() or previous.isdigit()):
            boundaries.add(index)

    for index in sorted(boundaries):
        marker = segment[:index].rstrip(_PATH_SEPARATORS)
        payload = segment[index:].lstrip(_PATH_SEPARATORS)
        payload_label, _payload_compact = _normalize_secret_label(payload)
        if payload_label in _PATH_PUBLIC_CONTEXTS:
            continue
        if marker and payload and _path_secret_marker(marker):
            return True
    return False


def _sanitize_url(raw_url: str) -> str:
    trailing = ""
    while raw_url and raw_url[-1] in ".,);]":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        if not hostname:
            return f"<redacted-url>{trailing}"
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
        netloc = f"{host}:{port}" if port is not None else host

        safe_segments: list[str] = []
        secret_context = False
        for raw_segment in parsed.path.split("/"):
            decoded = unquote(raw_segment)
            if secret_context:
                safe_segments.append(_REDACTED)
                continue
            if _path_secret_marker_with_payload(decoded):
                safe_segments.append(_REDACTED)
                secret_context = True
                continue
            if _path_secret_marker(decoded):
                safe_segments.append(quote(decoded, safe="-._~"))
                secret_context = True
                continue
            sanitized = _sanitize_plain_text(decoded)
            safe_segments.append(quote(sanitized, safe="-._~<>") if sanitized else "")
        safe_path = "/".join(safe_segments)

        safe_query: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            safe_query.append(
                (key, _REDACTED if _secret_key(key) else _sanitize_plain_text(value))
            )
        query = urlencode(safe_query, doseq=True, safe="<>")
        fragment = _REDACTED if parsed.fragment else ""
        return urlunsplit((parsed.scheme, netloc, safe_path, query, fragment)) + trailing
    except (TypeError, ValueError):
        return f"<redacted-url>{trailing}"


def _sanitize_assignments(value: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    last_end = -1
    for match in _ASSIGNMENT_CANDIDATE.finditer(value):
        label = match.group("label")
        if not _secret_key(label):
            continue
        start = match.start("label")
        end = match.end("value")
        if start < last_end:
            continue
        replacements.append(
            (
                start,
                end,
                f"{label}{match.group('separator')}{_REDACTED}",
            )
        )
        last_end = end

    for start, end, replacement in reversed(replacements):
        value = value[:start] + replacement + value[end:]
    return value


def _sanitize_plain_text(value: str) -> str:
    value = _AUTH_SCHEME.sub(
        lambda match: f"{match.group('prefix') or ''}{match.group('scheme')} {_REDACTED}",
        value,
    )
    value = _sanitize_assignments(value)
    value = _JWT.sub(_REDACTED, value)
    return value


def sanitize_diagnostic(value: Any, *, max_length: int = _MAX_DIAGNOSTIC) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text[: max(max_length * 4, max_length)]
    text = _URL.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _sanitize_plain_text(text)
    if _COOKIE_VALUE.search(text) and (
        ";" in text or text.casefold().lstrip().startswith("cookie")
    ):
        text = _REDACTED
    if len(text) > max_length:
        text = text[:max_length] + f"...<truncated:{len(text) - max_length}>"
    return text


def _redact_string(value: str) -> str:
    return sanitize_diagnostic(value)


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return "<max-depth>"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = sanitize_diagnostic(raw_key, max_length=256)
            output[key] = (
                _REDACTED if _secret_key(key) else redact(raw_value, _depth=_depth + 1)
            )
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
