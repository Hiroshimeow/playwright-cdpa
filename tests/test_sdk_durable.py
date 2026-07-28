from __future__ import annotations

from pathlib import Path

from playwright_api import AttachmentInput, ChatTarget
from playwright_api.models import AttachmentStage, TurnRecord


def test_turn_record_persists_target_and_attachment_identity_without_content(tmp_path: Path) -> None:
    path = tmp_path / "evidence.txt"
    path.write_text("private body", encoding="utf-8")
    attachment = AttachmentInput.from_path(path)
    target = ChatTarget.project_conversation("g-p-project123", "conversation-123")

    record = TurnRecord.new(
        request_id="request-123",
        prompt="analyze this",
        target=target,
        attachments=(attachment,),
    )
    payload = record.to_dict()

    assert payload["schema_version"] == 5
    assert payload["target_kind"] == "project_conversation"
    assert payload["target_project_id"] == "g-p-project123"
    assert payload["target_conversation_id"] == "conversation-123"
    assert payload["attachment_stage"] == "SNAPSHOTTED"
    assert payload["attachments"] == [
        {
            "path": str(path.resolve()),
            "name": "evidence.txt",
            "size": len("private body"),
            "sha256": attachment.sha256,
            "media_type": "text/plain",
        }
    ]
    assert "private body" not in str(payload)
    assert TurnRecord.from_dict(payload) == record
    assert record.target == target
    assert record.coordination_key == target.coordination_key


def test_schema_four_migrates_to_fresh_target_and_no_attachments() -> None:
    legacy = TurnRecord.new(
        request_id="legacy-request",
        prompt="legacy",
        target=ChatTarget.fresh(),
    ).to_dict()
    legacy["schema_version"] = 4
    legacy.pop("target_project_id")
    legacy.pop("attachments")
    legacy.pop("attachment_stage")

    migrated = TurnRecord.from_dict(legacy)

    assert migrated.schema_version == 5
    assert migrated.target == ChatTarget.fresh()
    assert migrated.attachments == ()
    assert migrated.attachment_stage is AttachmentStage.NOT_REQUIRED
