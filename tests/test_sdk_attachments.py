from __future__ import annotations

from pathlib import Path

import pytest

from playwright_api import AttachmentInput
from playwright_api.attachments import snapshot_attachments, validate_attachments_unchanged
from playwright_api.errors import InvalidInputError


def test_attachment_snapshot_is_immutable_and_exact(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")

    attachment = AttachmentInput.from_path(path)

    assert attachment.path == path.resolve()
    assert attachment.size == 5
    assert attachment.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert attachment.media_type == "text/plain"
    assert attachment.to_public_dict() == {
        "name": "note.txt",
        "size": 5,
        "sha256": attachment.sha256,
        "media_type": "text/plain",
    }


def test_attachment_rejects_unsupported_and_duplicate_paths(tmp_path: Path) -> None:
    unsupported = tmp_path / "payload.bin"
    unsupported.write_bytes(b"x")
    with pytest.raises(InvalidInputError, match="unsupported"):
        AttachmentInput.from_path(unsupported)

    note = tmp_path / "note.txt"
    note.write_text("hello", encoding="utf-8")
    attachment = AttachmentInput.from_path(note)
    with pytest.raises(InvalidInputError, match="duplicate"):
        snapshot_attachments([attachment, attachment])


def test_attachment_change_is_detected_before_send(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("one", encoding="utf-8")
    attachment = AttachmentInput.from_path(path)
    path.write_text("two changed", encoding="utf-8")

    with pytest.raises(InvalidInputError, match="changed"):
        validate_attachments_unchanged([attachment])
