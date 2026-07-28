from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from playwright_api import AttachmentInput, ChatTarget
from playwright_api.errors import FrontendNotReadyError
from playwright_api.frontend import click_send_atomic, upload_attachments


class FileInput:
    def __init__(self) -> None:
        self.paths: list[str] | None = None

    async def set_input_files(self, paths: list[str]) -> None:
        self.paths = paths


class InputLocator:
    def __init__(self, input_control: FileInput) -> None:
        self.first = input_control

    async def count(self) -> int:
        return 1


class UploadPage:
    def __init__(self, names: list[str]) -> None:
        self.input_control = FileInput()
        self.names = names

    def locator(self, selector: str):
        assert selector == 'input[type="file"]'
        return InputLocator(self.input_control)

    async def evaluate(self, script: str, *_args):
        assert "data-filename" in script
        return self.names

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


@pytest.mark.asyncio
async def test_upload_uses_real_file_input_and_verifies_exact_multiset(tmp_path: Path) -> None:
    first_path = tmp_path / "same.txt"
    first_path.write_text("first", encoding="utf-8")
    second_dir = tmp_path / "other"
    second_dir.mkdir()
    second_path = second_dir / "same.txt"
    second_path.write_text("second", encoding="utf-8")
    attachments = (
        AttachmentInput.from_path(first_path),
        AttachmentInput.from_path(second_path),
    )
    page = UploadPage(["same.txt", "same.txt"])

    await upload_attachments(page, attachments)  # type: ignore[arg-type]

    assert page.input_control.paths == [str(first_path.resolve()), str(second_path.resolve())]


@pytest.mark.asyncio
async def test_upload_rejects_partial_or_duplicate_ui_identity(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    first.write_text("one", encoding="utf-8")
    second = tmp_path / "two.txt"
    second.write_text("two", encoding="utf-8")
    attachments = (AttachmentInput.from_path(first), AttachmentInput.from_path(second))
    page = UploadPage(["one.txt"])

    with pytest.raises(FrontendNotReadyError, match="attachment identity"):
        await upload_attachments(page, attachments, timeout=0.001)  # type: ignore[arg-type]


class AtomicPage:
    def __init__(self) -> None:
        self.payload = None
        self.script = ""

    async def evaluate(self, script: str, payload):
        self.script = script
        self.payload = payload
        return {"ok": True}


@pytest.mark.asyncio
async def test_atomic_send_binds_target_and_attachment_multiset() -> None:
    page = AtomicPage()
    target = ChatTarget.project_conversation("g-p-project123", "conversation-123")

    await click_send_atomic(
        page,  # type: ignore[arg-type]
        "prompt",
        target=target,
        expected_attachment_names=Counter({"same.txt": 2, "other.pdf": 1}),
    )

    assert page.payload == {
        "prompt": "prompt",
        "target_path": "/g/g-p-project123/c/conversation-123",
        "attachment_names": {"same.txt": 2, "other.pdf": 1},
    }
    assert "attachments_changed" in page.script
