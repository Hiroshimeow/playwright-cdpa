from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from playwright_api import AttachmentInput, ChatTarget
from playwright_api.errors import FrontendNotReadyError
from playwright_api.frontend import attachment_state, click_send_atomic, upload_attachments


class FileInput:
    def __init__(self, boundary_events: list[str] | None = None) -> None:
        self.paths: list[str] | None = None
        self.boundary_events = boundary_events

    async def set_input_files(self, paths: list[str]) -> None:
        if self.boundary_events is not None:
            assert self.boundary_events == ["boundary"]
        self.paths = paths


class InputLocator:
    def __init__(self, input_control: FileInput, count: int = 1) -> None:
        self.first = input_control
        self._count = count

    async def count(self) -> int:
        return self._count


class UploadPage:
    def __init__(self, states: list[dict[str, object]], *, input_count: int = 1) -> None:
        self.input_control = FileInput()
        self.input_count = input_count
        self.states = states
        self.evaluate_calls = 0

    def locator(self, selector: str):
        assert selector == 'input[type="file"]#upload-files'
        return InputLocator(self.input_control, self.input_count)

    async def evaluate(self, script: str, *_args):
        assert 'role="group"' in script
        index = min(self.evaluate_calls, len(self.states) - 1)
        self.evaluate_calls += 1
        return self.states[index]

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
    page = UploadPage(
        [
            {"names": ["same.txt", "same.txt"], "pending": True},
            {"names": ["same.txt", "same.txt"], "pending": False},
        ]
    )

    await upload_attachments(page, attachments)  # type: ignore[arg-type]

    assert page.input_control.paths == [str(first_path.resolve()), str(second_path.resolve())]


@pytest.mark.asyncio
async def test_upload_marks_boundary_immediately_before_file_input_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.txt"
    path.write_text("evidence", encoding="utf-8")
    events: list[str] = []
    page = UploadPage([{"names": ["evidence.txt"], "pending": False}])
    page.input_control = FileInput(events)

    await upload_attachments(
        page,  # type: ignore[arg-type]
        (AttachmentInput.from_path(path),),
        on_upload_boundary=lambda: events.append("boundary"),
    )

    assert events == ["boundary"]
    assert page.input_control.paths == [str(path.resolve())]


@pytest.mark.asyncio
async def test_upload_preflight_failure_does_not_enter_mutation_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.txt"
    path.write_text("evidence", encoding="utf-8")
    events: list[str] = []
    page = UploadPage([], input_count=2)

    with pytest.raises(FrontendNotReadyError, match="one exact"):
        await upload_attachments(
            page,  # type: ignore[arg-type]
            (AttachmentInput.from_path(path),),
            on_upload_boundary=lambda: events.append("boundary"),
        )

    assert events == []
    assert page.input_control.paths is None


@pytest.mark.asyncio
async def test_upload_rejects_partial_or_duplicate_ui_identity(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    first.write_text("one", encoding="utf-8")
    second = tmp_path / "two.txt"
    second.write_text("two", encoding="utf-8")
    attachments = (AttachmentInput.from_path(first), AttachmentInput.from_path(second))
    page = UploadPage([{"names": ["one.txt"], "pending": False}])

    with pytest.raises(FrontendNotReadyError, match="attachment identity"):
        await upload_attachments(page, attachments, timeout=0.001)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_attachment_state_uses_exact_file_tiles_and_pending_status() -> None:
    page = UploadPage([{"names": ["evidence.txt"], "pending": True}])

    state = await attachment_state(page)  # type: ignore[arg-type]

    assert state.names == ("evidence.txt",)
    assert state.pending is True


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
        "target_path_pattern": r"^/g/g\-p\-project123/c/conversation\-123$",
        "attachment_names": {"same.txt": 2, "other.pdf": 1},
    }
    assert "attachments_changed" in page.script
    assert "attachments_pending" in page.script
    assert "new RegExp(expectedPathPattern).test(current.pathname)" in page.script
