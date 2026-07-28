from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .errors import InvalidInputError

_SUPPORTED_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass(frozen=True, slots=True)
class AttachmentInput:
    path: Path
    size: int
    sha256: str
    media_type: str

    @classmethod
    def from_path(cls, value: str | Path) -> AttachmentInput:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise InvalidInputError("attachment must be an existing regular file")
        media_type = _SUPPORTED_MEDIA_TYPES.get(path.suffix.casefold())
        if media_type is None:
            raise InvalidInputError("unsupported attachment media type")
        try:
            size, digest = _fingerprint(path)
        except OSError as exc:
            raise InvalidInputError("attachment is not readable") from exc
        return cls(path=path, size=size, sha256=digest, media_type=media_type)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.path.name,
            "size": self.size,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    def to_durable_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            **self.to_public_dict(),
        }

    @classmethod
    def from_durable_dict(cls, value: object) -> AttachmentInput:
        expected = {"path", "name", "size", "sha256", "media_type"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("attachment record has an invalid field set")
        raw_path = value["path"]
        raw_name = value["name"]
        raw_size = value["size"]
        raw_sha256 = value["sha256"]
        raw_media_type = value["media_type"]
        if type(raw_path) is not str or not raw_path:
            raise ValueError("attachment path must be a non-empty string")
        path = Path(raw_path)
        if not path.is_absolute() or path.name != raw_name:
            raise ValueError("attachment path and name must be canonical")
        if type(raw_size) is not int or raw_size < 0:
            raise ValueError("attachment size must be a non-negative integer")
        if (
            type(raw_sha256) is not str
            or len(raw_sha256) != 64
            or any(character not in "0123456789abcdef" for character in raw_sha256)
        ):
            raise ValueError("attachment sha256 must be lowercase hexadecimal")
        if type(raw_media_type) is not str or raw_media_type not in _SUPPORTED_MEDIA_TYPES.values():
            raise ValueError("attachment media type is unsupported")
        return cls(
            path=path,
            size=raw_size,
            sha256=raw_sha256,
            media_type=raw_media_type,
        )


def snapshot_attachments(values: Iterable[AttachmentInput]) -> tuple[AttachmentInput, ...]:
    attachments = tuple(values)
    seen: set[Path] = set()
    for attachment in attachments:
        if not isinstance(attachment, AttachmentInput):
            raise InvalidInputError("attachments must be AttachmentInput values")
        if attachment.path in seen:
            raise InvalidInputError("duplicate attachment path")
        seen.add(attachment.path)
    validate_attachments_unchanged(attachments)
    return attachments


def validate_attachments_unchanged(values: Iterable[AttachmentInput]) -> None:
    for attachment in values:
        try:
            size, digest = _fingerprint(attachment.path)
        except OSError as exc:
            raise InvalidInputError("attachment is missing or unreadable") from exc
        if size != attachment.size or digest != attachment.sha256:
            raise InvalidInputError("attachment changed after snapshot")


def _fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
