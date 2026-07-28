from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

_REDACTED = "<redacted>"
_MAX_DIAGNOSTIC = 8192
_MAX_MAPPING_KEY = 256
_MAX_QUOTED_KEY_RAW = _MAX_MAPPING_KEY * 6
_MAX_STRUCTURED_DIAGNOSTIC_DEPTH = 12
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
_UNQUOTED_ASSIGNMENT_CANDIDATE = re.compile(
    r"(?i)(?=(?<![A-Za-z0-9_-])"
    r"(?P<key>(?P<label>[A-Za-z][A-Za-z0-9_-]{0,80}))"
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
_SEPARATED_HEADER_SECRET_SUFFIXES = (
    ("proxy", "authorization"),
    ("set", "cookie"),
    ("authorization",),
    ("cookies",),
    ("cookie",),
)
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


def _separated_header_qualifier(normalized: str) -> bool:
    components = normalized.split("_")
    for suffix in _SEPARATED_HEADER_SECRET_SUFFIXES:
        if tuple(components[-len(suffix) :]) != suffix:
            continue
        qualifier = components[: -len(suffix)]
        return bool(qualifier) and all(
            component in _COMPACT_HEADER_QUALIFIERS for component in qualifier
        )
    return False


def _qualified_header_secret_key(normalized: str, compact: str) -> bool:
    if _separated_header_qualifier(normalized):
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


def _plain_text_has_secret_boundary(value: str) -> bool:
    if _COOKIE_HEADER.search(value):
        return True
    if _AUTHORIZATION_HEADER.search(value) or _AUTH_SCHEME.search(value) or _JWT.search(value):
        return True
    return any(
        _secret_key(match.group("label"))
        for match in _UNQUOTED_ASSIGNMENT_CANDIDATE.finditer(value)
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
    next_assignment = _UNQUOTED_ASSIGNMENT_CANDIDATE.search(value, search_start)
    if next_assignment is not None:
        end = min(end, next_assignment.start("key"))
    while end > match.start("value") and value[end - 1].isspace():
        end -= 1
    return end


def _assignment_label(match: re.Match[str]) -> str:
    return match.group("label")


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _quoted_key_close(value: str, start: int) -> int | None:
    quote_character = value[start]
    cursor = start + 1
    while cursor < len(value):
        character = value[cursor]
        if character in "\r\n":
            return None
        if character == quote_character and not _is_escaped(value, cursor):
            return cursor
        cursor += 1
    return None


def _decode_single_quoted_key(raw: str) -> str | None:
    output: list[str] = []
    cursor = 0
    escapes = {
        '"': '"',
        "'": "'",
        "/": "/",
        "\\": "\\",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while cursor < len(raw):
        character = raw[cursor]
        if character != "\\":
            output.append(character)
            cursor += 1
            continue
        if cursor + 1 >= len(raw):
            return None
        escaped = raw[cursor + 1]
        if escaped == "u":
            digits = raw[cursor + 2 : cursor + 6]
            if len(digits) != 4 or not all(
                character in "0123456789abcdefABCDEF" for character in digits
            ):
                return None
            output.append(chr(int(digits, 16)))
            cursor += 6
            continue
        decoded = escapes.get(escaped)
        if decoded is None:
            return None
        output.append(decoded)
        cursor += 2
    return "".join(output)


def _decode_quoted_key(raw: str, quote_character: str) -> str | None:
    try:
        decoded = (
            json.loads(f'"{raw}"') if quote_character == '"' else _decode_single_quoted_key(raw)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, str):
        return None
    if not decoded or len(decoded) > _MAX_MAPPING_KEY:
        return None
    if not all(character.isprintable() for character in decoded):
        return None
    return decoded


def _quoted_assignment_value(value: str, start: int) -> tuple[int, str]:
    if start >= len(value):
        return len(value), _REDACTED
    quote_character = value[start]
    if quote_character in {'"', "'"}:
        close = _quoted_key_close(value, start)
        if close is None:
            return len(value), _REDACTED
        return close + 1, f"{quote_character}{_REDACTED}{quote_character}"

    end = len(value)
    delimiter = _UNQUOTED_ASSIGNMENT_DELIMITER.search(value, start)
    if delimiter is not None:
        end = delimiter.start()
    next_assignment = _UNQUOTED_ASSIGNMENT_CANDIDATE.search(value, start + 1)
    if next_assignment is not None:
        end = min(end, next_assignment.start("key"))
    while end > start and value[end - 1].isspace():
        end -= 1
    return end, _REDACTED


def _malformed_secret_quoted_key(raw: str, quote_character: str) -> bool:
    separator_positions = [
        position for position, character in enumerate(raw) if character in ":="
    ]
    candidate_positions = separator_positions or [len(raw)]

    for position in reversed(candidate_positions):
        prefix = raw[:position].strip(" \t\"'")
        if not prefix:
            continue
        if len(prefix) > _MAX_QUOTED_KEY_RAW:
            return True
        decoded_key = _decode_quoted_key(prefix, quote_character)
        if decoded_key is None:
            return True
        if _secret_key(decoded_key) or _plain_text_has_secret_boundary(decoded_key):
            return True
    return False


def _sanitize_quoted_assignments(value: str) -> tuple[str, bool]:
    replacements: list[tuple[int, int, str]] = []
    cursor = 0
    while cursor < len(value):
        if value[cursor] not in {'"', "'"} or _is_escaped(value, cursor):
            cursor += 1
            continue
        if (
            cursor > 0
            and value[cursor - 1]
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        ):
            cursor += 1
            continue

        close = _quoted_key_close(value, cursor)
        if close is None:
            before_quote = value[:cursor].rstrip()
            if before_quote.endswith((":", "=")):
                cursor += 1
                continue
            line_end = len(value)
            newline = re.search(r"[\r\n]", value[cursor + 1 :])
            if newline is not None:
                line_end = cursor + 1 + newline.start()
            if _malformed_secret_quoted_key(value[cursor + 1 : line_end], value[cursor]):
                return _REDACTED, True
            cursor += 1
            continue

        separator_start = close + 1
        while separator_start < len(value) and value[separator_start].isspace():
            separator_start += 1
        if separator_start >= len(value) or value[separator_start] not in ":=":
            before_quote = value[:cursor].rstrip()
            if before_quote.endswith((":", "=")):
                cursor = close + 1
                continue
            raw_key = value[cursor + 1 : close]
            if _malformed_secret_quoted_key(raw_key, value[cursor]):
                return _REDACTED, True
            cursor = close + 1
            continue

        separator_end = separator_start + 1
        while separator_end < len(value) and value[separator_end].isspace():
            separator_end += 1
        raw_key = value[cursor + 1 : close]
        decoded_key = _decode_quoted_key(raw_key, value[cursor])
        if decoded_key is None:
            return _REDACTED, True
        if not _secret_key(decoded_key):
            cursor = separator_end
            continue

        value_end, replacement_value = _quoted_assignment_value(value, separator_end)
        replacements.append(
            (
                cursor,
                value_end,
                value[cursor:separator_end] + replacement_value,
            )
        )
        cursor = max(value_end, separator_end + 1)

    for start, end, replacement in reversed(replacements):
        value = value[:start] + replacement + value[end:]
    return value, False


def _sanitize_assignments(value: str) -> str:
    value, failed_closed = _sanitize_quoted_assignments(value)
    if failed_closed:
        return value

    replacements: list[tuple[int, int, str]] = []
    last_end = -1
    for match in _UNQUOTED_ASSIGNMENT_CANDIDATE.finditer(value):
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


def _line_break_after(value: str, index: int) -> int:
    return index + 2 if value.startswith("\r\n", index) else index + 1


def _authorization_value_end(value: str, start: int) -> int:
    cursor = start
    while True:
        newline = re.search(r"[\r\n]", value[cursor:])
        if newline is None:
            return len(value)
        line_end = cursor + newline.start()
        next_line = _line_break_after(value, line_end)
        if next_line >= len(value) or value[next_line] not in " \t":
            return line_end
        cursor = next_line


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


def _sanitize_unstructured_diagnostic(text: str, *, max_length: int) -> str:
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


def _skip_json_whitespace(text: str, cursor: int) -> int:
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    return cursor


def _json_string_close(text: str, start: int) -> int:
    close = _quoted_key_close(text, start)
    if close is None:
        raise ValueError("invalid JSON string boundary")
    return close


def _json_value_end(text: str, start: int, *, depth: int) -> int:
    if depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
        raise ValueError("structured diagnostic depth exceeded")
    cursor = _skip_json_whitespace(text, start)
    if cursor >= len(text):
        raise ValueError("missing JSON value")
    character = text[cursor]
    if character == '"':
        return _json_string_close(text, cursor) + 1
    if character == "{":
        cursor = _skip_json_whitespace(text, cursor + 1)
        if cursor < len(text) and text[cursor] == "}":
            return cursor + 1
        while cursor < len(text):
            if text[cursor] != '"':
                raise ValueError("invalid JSON object key")
            cursor = _json_string_close(text, cursor) + 1
            cursor = _skip_json_whitespace(text, cursor)
            if cursor >= len(text) or text[cursor] != ":":
                raise ValueError("missing JSON object separator")
            cursor = _json_value_end(text, cursor + 1, depth=depth + 1)
            cursor = _skip_json_whitespace(text, cursor)
            if cursor < len(text) and text[cursor] == "}":
                return cursor + 1
            if cursor >= len(text) or text[cursor] != ",":
                raise ValueError("invalid JSON object delimiter")
            cursor = _skip_json_whitespace(text, cursor + 1)
        raise ValueError("unterminated JSON object")
    if character == "[":
        cursor = _skip_json_whitespace(text, cursor + 1)
        if cursor < len(text) and text[cursor] == "]":
            return cursor + 1
        while cursor < len(text):
            cursor = _json_value_end(text, cursor, depth=depth + 1)
            cursor = _skip_json_whitespace(text, cursor)
            if cursor < len(text) and text[cursor] == "]":
                return cursor + 1
            if cursor >= len(text) or text[cursor] != ",":
                raise ValueError("invalid JSON array delimiter")
            cursor = _skip_json_whitespace(text, cursor + 1)
        raise ValueError("unterminated JSON array")

    while cursor < len(text) and text[cursor] not in ",]} \t\r\n":
        cursor += 1
    if cursor == start:
        raise ValueError("invalid JSON scalar")
    return cursor


def _decode_json_escape_layer(value: str) -> str | None:
    output: list[str] = []
    cursor = 0
    changed = False
    escapes = {
        '"': '"',
        "/": "/",
        "\\": "\\",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while cursor < len(value):
        character = value[cursor]
        if character != "\\" or cursor + 1 >= len(value):
            output.append(character)
            cursor += 1
            continue
        escaped = value[cursor + 1]
        if escaped == "u":
            digits = value[cursor + 2 : cursor + 6]
            if len(digits) == 4 and all(
                character in "0123456789abcdefABCDEF" for character in digits
            ):
                output.append(chr(int(digits, 16)))
                cursor += 6
                changed = True
                continue
            output.extend(("\\", escaped))
            cursor += 2
            continue
        decoded = escapes.get(escaped)
        if decoded is None:
            output.extend(("\\", escaped))
            cursor += 2
            continue
        output.append(decoded)
        cursor += 2
        changed = True
    return "".join(output) if changed else None


def _canonical_json_key_is_secret(value: str, *, depth: int) -> bool:
    current = value
    current_depth = depth
    while True:
        if current_depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
            raise ValueError("structured diagnostic key depth exceeded")
        if not current or len(current) > _MAX_MAPPING_KEY:
            raise ValueError("invalid bounded JSON object key")
        if not all(character.isprintable() for character in current):
            raise ValueError("control-bearing JSON object key")
        if _plain_text_has_secret_boundary(current):
            raise ValueError("secret-bearing JSON object key token")
        if _secret_key(current):
            return True

        output: list[str] = []
        cursor = 0
        changed = False
        escapes = {
            '"': '"',
            "/": "/",
            "\\": "\\",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        while cursor < len(current):
            character = current[cursor]
            if character != "\\":
                output.append(character)
                cursor += 1
                continue
            if cursor + 1 >= len(current):
                raise ValueError("unterminated JSON object-key escape")
            escaped = current[cursor + 1]
            if escaped == "u":
                digits = current[cursor + 2 : cursor + 6]
                if len(digits) != 4 or not all(
                    character in "0123456789abcdefABCDEF" for character in digits
                ):
                    raise ValueError("invalid JSON object-key Unicode escape")
                output.append(chr(int(digits, 16)))
                cursor += 6
                changed = True
                continue
            decoded = escapes.get(escaped)
            if decoded is None:
                raise ValueError("invalid JSON object-key escape")
            output.append(decoded)
            cursor += 2
            changed = True

        if not changed:
            return False
        decoded_key = "".join(output)
        if decoded_key == current:
            raise ValueError("non-convergent JSON object-key canonicalization")
        current = decoded_key
        current_depth += 1


def _sanitize_nested_diagnostic_value(
    value: str,
    *,
    max_length: int,
    depth: int,
) -> str:
    if depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
        return _REDACTED
    if len(value) > max(max_length * 4, max_length):
        return _REDACTED

    structured = _sanitize_json_string_values(
        value,
        max_length=max_length,
        depth=depth,
    )
    if structured is not None:
        return structured

    sanitized = _sanitize_unstructured_diagnostic(value, max_length=max_length)
    if sanitized != value:
        return sanitized

    decoded = _decode_json_escape_layer(value)
    if decoded is None or decoded == value:
        return value
    decoded_sanitized = _sanitize_nested_diagnostic_value(
        decoded,
        max_length=max_length,
        depth=depth + 1,
    )
    return decoded_sanitized if decoded_sanitized != decoded else value


def _collect_json_replacements(
    text: str,
    start: int,
    *,
    max_length: int,
    depth: int,
    replacements: list[tuple[int, int, str]],
) -> int:
    if depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
        raise ValueError("structured diagnostic depth exceeded")
    cursor = _skip_json_whitespace(text, start)
    if cursor >= len(text):
        raise ValueError("missing JSON value")
    character = text[cursor]

    if character == '"':
        close = _json_string_close(text, cursor)
        decoded_value = json.loads(text[cursor : close + 1])
        if not isinstance(decoded_value, str):
            raise ValueError("invalid JSON string value")
        sanitized = _sanitize_nested_diagnostic_value(
            decoded_value,
            max_length=max_length,
            depth=depth + 1,
        )
        if sanitized != decoded_value:
            encoded = json.dumps(sanitized, ensure_ascii=False)[1:-1]
            replacements.append((cursor + 1, close, encoded))
        return close + 1

    if character == "{":
        cursor = _skip_json_whitespace(text, cursor + 1)
        if cursor < len(text) and text[cursor] == "}":
            return cursor + 1
        while cursor < len(text):
            if text[cursor] != '"':
                raise ValueError("invalid JSON object key")
            key_close = _json_string_close(text, cursor)
            raw_key = text[cursor + 1 : key_close]
            decoded_key = _decode_quoted_key(raw_key, '"')
            if decoded_key is None:
                raise ValueError("invalid bounded JSON object key")
            secret_key = _canonical_json_key_is_secret(decoded_key, depth=depth)
            separator = _skip_json_whitespace(text, key_close + 1)
            if separator >= len(text) or text[separator] != ":":
                raise ValueError("missing JSON object separator")
            value_start = _skip_json_whitespace(text, separator + 1)
            if secret_key:
                value_end = _json_value_end(text, value_start, depth=depth + 1)
                replacements.append(
                    (value_start, value_end, json.dumps(_REDACTED, ensure_ascii=False))
                )
            else:
                value_end = _collect_json_replacements(
                    text,
                    value_start,
                    max_length=max_length,
                    depth=depth + 1,
                    replacements=replacements,
                )
            cursor = _skip_json_whitespace(text, value_end)
            if cursor < len(text) and text[cursor] == "}":
                return cursor + 1
            if cursor >= len(text) or text[cursor] != ",":
                raise ValueError("invalid JSON object delimiter")
            cursor = _skip_json_whitespace(text, cursor + 1)
        raise ValueError("unterminated JSON object")

    if character == "[":
        cursor = _skip_json_whitespace(text, cursor + 1)
        if cursor < len(text) and text[cursor] == "]":
            return cursor + 1
        while cursor < len(text):
            cursor = _collect_json_replacements(
                text,
                cursor,
                max_length=max_length,
                depth=depth + 1,
                replacements=replacements,
            )
            cursor = _skip_json_whitespace(text, cursor)
            if cursor < len(text) and text[cursor] == "]":
                return cursor + 1
            if cursor >= len(text) or text[cursor] != ",":
                raise ValueError("invalid JSON array delimiter")
            cursor = _skip_json_whitespace(text, cursor + 1)
        raise ValueError("unterminated JSON array")

    return _json_value_end(text, cursor, depth=depth)


def _sanitize_json_string_values(
    text: str,
    *,
    max_length: int,
    depth: int = 0,
) -> str | None:
    if not text.lstrip().startswith(('"', "{", "[")):
        return None
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None
    if not isinstance(decoded, (str, dict, list)):
        return None
    if depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
        return json.dumps(_REDACTED) if isinstance(decoded, str) else _REDACTED

    if isinstance(decoded, str):
        sanitized = _sanitize_nested_diagnostic_value(
            decoded,
            max_length=max_length,
            depth=depth + 1,
        )
        if sanitized == decoded:
            return text
        rendered = json.dumps(sanitized, ensure_ascii=False)
        return rendered if len(rendered) <= max_length else json.dumps(_REDACTED)

    replacements: list[tuple[int, int, str]] = []
    try:
        end = _collect_json_replacements(
            text,
            0,
            max_length=max_length,
            depth=depth,
            replacements=replacements,
        )
    except (RecursionError, TypeError, ValueError):
        return _REDACTED
    if _skip_json_whitespace(text, end) != len(text):
        return _REDACTED

    for start, stop, replacement in reversed(replacements):
        text = text[:start] + replacement + text[stop:]
    if len(text) > max_length:
        return _REDACTED
    return text


def sanitize_diagnostic(value: Any, *, max_length: int = _MAX_DIAGNOSTIC) -> str:
    text = value if isinstance(value, str) else str(value)
    input_budget = max(max_length * 4, max_length)
    stripped = text.lstrip()
    if len(text) > input_budget and stripped.startswith(('"', "{", "[")):
        return json.dumps(_REDACTED) if stripped.startswith('"') else _REDACTED
    text = text[:input_budget]
    structured = _sanitize_json_string_values(text, max_length=max_length)
    if structured is not None:
        return structured
    return _sanitize_unstructured_diagnostic(text, max_length=max_length)


def _redact_string(value: str) -> str:
    return sanitize_diagnostic(value)


def redact(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return "<max-depth>"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            raw_key_text = raw_key if isinstance(raw_key, str) else str(raw_key)
            key = sanitize_diagnostic(raw_key_text, max_length=_MAX_MAPPING_KEY)
            unsafe_key = False
            try:
                canonical_secret = _canonical_json_key_is_secret(
                    raw_key_text,
                    depth=_depth,
                )
            except ValueError:
                canonical_secret = True
                unsafe_key = True
            if unsafe_key:
                key = _REDACTED
            if key in output:
                return {_REDACTED: _REDACTED}
            value_is_secret = (
                len(raw_key_text) > _MAX_MAPPING_KEY or _secret_key(key) or canonical_secret
            )
            output[key] = _REDACTED if value_is_secret else redact(raw_value, _depth=_depth + 1)
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
