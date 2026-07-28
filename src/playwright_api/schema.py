from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_E = TypeVar("_E", bound=Enum)


def decode_identifier(
    value: Any,
    field: str,
    *,
    optional: bool = False,
    max_length: int = 512,
) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        expected = "a string or null" if optional else "a string"
        raise ValueError(f"{field} must be exactly {expected}")
    if not value or value.isspace():
        raise ValueError(f"{field} must not be empty or whitespace-only")
    if len(value) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{field} contains control characters")
    return value


def decode_optional_identifier(value: Any, field: str, *, max_length: int = 512) -> str | None:
    return decode_identifier(value, field, optional=True, max_length=max_length)


def decode_required_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be exactly boolean")
    return value


def decode_required_int(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be exactly an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def decode_bounded_text(
    value: Any,
    field: str,
    *,
    optional: bool = False,
    max_length: int = 8192,
) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        expected = "a string or null" if optional else "a string"
        raise ValueError(f"{field} must be exactly {expected}")
    if len(value) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{field} contains NUL")
    return value


def decode_timestamp(value: Any, field: str, *, optional: bool = False) -> str | None:
    text = decode_bounded_text(value, field, optional=optional, max_length=64)
    if text is None:
        return None
    if not text:
        raise ValueError(f"{field} must not be empty")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return text


def decode_sha256(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _HEX_SHA256.fullmatch(value) is None:
        expected = (
            "a lowercase SHA-256 string or null" if optional else "a lowercase SHA-256 string"
        )
        raise ValueError(f"{field} must be exactly {expected}")
    return value


def decode_enum(value: Any, field: str, enum_type: type[_E]) -> _E:
    if type(value) is not str:
        raise ValueError(f"{field} must be exactly a string enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{field} is unsupported") from exc
