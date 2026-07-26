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
    "aws_secret_access_key",
    "awssecretaccesskey",
    "cookie",
    "cookies",
    "credential",
    "client_assertion",
    "clientassertion",
    "code_verifier",
    "codeverifier",
    "encryption_key",
    "encryptionkey",
    "credentials",
    "password",
    "passphrase",
    "passwd",
    "private_key",
    "privatekey",
    "secret",
    "secret_access_key",
    "secretaccesskey",
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
    "set_cookie",
    "setcookie",
    "signature",
    "signing_key",
    "signingkey",
    "proof",
    "proof_token",
    "prooftoken",
    "proof_material",
    "proofmaterial",
    "proxy_authorization",
    "proxyauthorization",
    "sentinel",
    "turnstile",
    "token",
}
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{3,}\b")
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\b(?P<label>proxy-authorization|authorization)"
    r"(?P<separator>\s*[:=]\s*)"
)
_AUTH_SCHEME = re.compile(
    r"(?i)\b(?P<prefix>authorization\s*[:=]\s*)?"
    r"(?P<scheme>bearer|basic)\s+(?P<value>[^\s,;]+)"
)
_ASSIGNMENT_CANDIDATE = re.compile(
    r"(?i)(?=(?<![A-Za-z0-9_-])"
    r"(?P<key>(?:(?P<quote>[\"'])(?P<quoted_label>[A-Za-z][A-Za-z0-9_.:/-]{0,80})"
    r"(?P=quote)|(?P<label>[A-Za-z][A-Za-z0-9_-]{0,80})))"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\"[\s\S]*$|'[\s\S]*$|[^\s,;&\"']+))"
)
_HTTP_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_COOKIE_VALUE = re.compile(rf"(?i)(?:^|;\s*){_HTTP_TOKEN}=[^;\s]+")
_COOKIE_HEADER = re.compile(rf"(?i)\b(?:set-cookie|cookie)\s*:\s*(?={_HTTP_TOKEN}=)")
_UNQUOTED_ASSIGNMENT_DELIMITER = re.compile(r"[,;&\r\n]")
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
_QUALIFIED_SECRET_SUFFIXES = ("_authorization", "_cookie", "_cookies")
_COMPACT_HEADER_SECRET_SUFFIXES = (
    "proxyauthorization",
    "authorization",
    "setcookie",
    "cookies",
    "cookie",
)
_COMPACT_HEADER_QUALIFIERS = {
    "downstream",
    "header",
    "headers",
    "http",
    "https",
    "network",
    "proxy",
    "request",
    "requests",
    "response",
    "responses",
    "upstream",
}
_COMPACT_HEADER_COMPONENTS = tuple(sorted(_COMPACT_HEADER_QUALIFIERS, key=len, reverse=True))
_COMPACT_HEADER_CONTEXTS = {"header", "headers"}
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


def _compound_compact_header_qualifier(qualifier: str) -> bool:
    without_header = [False] * (len(qualifier) + 1)
    with_header = [False] * (len(qualifier) + 1)
    without_header[0] = True

    for index in range(len(qualifier)):
        if not without_header[index] and not with_header[index]:
            continue
        for component in _COMPACT_HEADER_COMPONENTS:
            if not qualifier.startswith(component, index):
                continue
            end = index + len(component)
            is_header = component in _COMPACT_HEADER_CONTEXTS
            if without_header[index]:
                if is_header:
                    with_header[end] = True
                else:
                    without_header[end] = True
            if with_header[index]:
                with_header[end] = True

    return with_header[-1]


def _qualified_header_secret_key(normalized: str, compact: str) -> bool:
    if any(normalized.endswith(suffix) for suffix in _QUALIFIED_SECRET_SUFFIXES):
        return True
    for suffix in _COMPACT_HEADER_SECRET_SUFFIXES:
        if not compact.endswith(suffix):
            continue
        qualifier = compact[: -len(suffix)]
        if qualifier in _COMPACT_HEADER_QUALIFIERS:
            return True
        return _compound_compact_header_qualifier(qualifier)
    return False


def _secret_key(key: str) -> bool:
    normalized, compact = _normalize_secret_label(key)
    if normalized in _KNOWN_SECRET_LABELS or compact in _KNOWN_SECRET_LABELS:
        return True
    if _qualified_header_secret_key(normalized, compact):
        return True
    if normalized.endswith("_token") or compact.endswith("token"):
        return True
    if normalized.endswith(
        (
            "_secret",
            "_secret_access_key",
            "_encryption_key",
            "_passphrase",
            "_password",
            "_passwd",
            "_api_key",
            "_credential",
            "_credentials",
            "_private_key",
            "_signing_key",
            "_signature",
            "_code_verifier",
            "_client_assertion",
        )
    ):
        return True
    return compact.endswith(
        (
            "secret",
            "secretaccesskey",
            "encryptionkey",
            "passphrase",
            "password",
            "passwd",
            "apikey",
            "credential",
            "credentials",
            "privatekey",
            "signingkey",
            "signature",
            "codeverifier",
            "clientassertion",
        )
    )


def _path_secret_marker(segment: str) -> bool:
    normalized, _compact = _normalize_secret_label(segment)
    return normalized in _PATH_SECRET_CONTEXTS or _secret_key(segment)


def _path_secret_marker_with_payload(segment: str) -> bool:
    boundaries: set[int] = set()
    for index, character in enumerate(segment):
        if index == 0:
            continue
        previous = segment[index - 1]
        if character in _PATH_SEPARATORS or (
            character.isupper() and (previous.islower() or previous.isdigit())
        ):
            boundaries.add(index)

    for index in sorted(boundaries, reverse=True):
        marker = segment[:index].rstrip(_PATH_SEPARATORS)
        payload = segment[index:].lstrip(_PATH_SEPARATORS)
        payload_label, _payload_compact = _normalize_secret_label(payload)
        if payload_label in _PATH_PUBLIC_CONTEXTS and _path_secret_marker(marker):
            return False

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


def _assignment_value_end(value: str, match: re.Match[str]) -> int:
    matched_value = match.group("value")
    if matched_value and all(part == _REDACTED for part in matched_value.split("#")):
        return match.end("value")
    if matched_value.startswith(('"', "'")):
        return match.end("value")

    search_start = match.end("value")
    end = len(value)
    delimiter = _UNQUOTED_ASSIGNMENT_DELIMITER.search(value, search_start)
    if delimiter is not None:
        end = delimiter.start()
    next_assignment = _ASSIGNMENT_CANDIDATE.search(value, search_start)
    if next_assignment is not None:
        end = min(end, next_assignment.start("key"))
    while end > match.start("value") and value[end - 1].isspace():
        end -= 1
    return end


def _assignment_label(match: re.Match[str]) -> str:
    return match.group("quoted_label") or match.group("label")


def _sanitize_assignments(value: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    last_end = -1
    for match in _ASSIGNMENT_CANDIDATE.finditer(value):
        label = _assignment_label(match)
        if not _secret_key(label):
            continue
        start = match.start("key")
        end = _assignment_value_end(value, match)
        if start < last_end:
            continue
        raw_value = match.group("value")
        if raw_value.startswith(('"', "'")) and raw_value[-1:] == raw_value[:1]:
            replacement_value = f"{raw_value[0]}{_REDACTED}{raw_value[0]}"
        else:
            replacement_value = _REDACTED
        replacements.append(
            (
                start,
                end,
                f"{match.group('key')}{match.group('separator')}{replacement_value}",
            )
        )
        last_end = end

    for start, end, replacement in reversed(replacements):
        value = value[:start] + replacement + value[end:]
    return value


def _authorization_value_end(value: str, start: int) -> int:
    newline = re.search(r"[\r\n]", value[start:])
    return len(value) if newline is None else start + newline.start()


def _sanitize_authorization_headers(value: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    for match in _AUTHORIZATION_HEADER.finditer(value):
        start = match.start("label")
        end = _authorization_value_end(value, match.end())
        replacements.append(
            (
                start,
                end,
                f"{match.group('label')}{match.group('separator')}{_REDACTED}",
            )
        )
    for start, end, replacement in reversed(replacements):
        value = value[:start] + replacement + value[end:]
    return value


def _sanitize_plain_text(value: str) -> str:
    value = _sanitize_authorization_headers(value)
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
    if _COOKIE_HEADER.search(text):
        return _REDACTED
    text = _URL.sub(lambda match: _sanitize_url(match.group(0)), text)
    text = _sanitize_plain_text(text)
    cookie_matches = list(_COOKIE_VALUE.finditer(text))
    if len(cookie_matches) >= 2:
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
