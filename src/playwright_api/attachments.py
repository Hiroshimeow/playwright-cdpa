from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
