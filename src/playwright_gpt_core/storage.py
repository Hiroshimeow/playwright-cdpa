from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

from .errors import ConcurrentStateError, CorruptStateError, OwnershipConflictError
from .models import ConversationRecord, TurnRecord
from .redaction import redact

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.turns_dir = self.root / "turns"
        self.conversations_dir = self.root / "conversations"
        self.locks_dir = self.root / "locks"

    def turn_path(self, request_id: str) -> Path:
        if not _SAFE_ID.fullmatch(request_id):
            raise ValueError("request_id contains unsafe path characters")
        return self.turns_dir / f"{request_id}.json"

    def conversation_path(self, conversation_id: str) -> Path:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return self.conversations_dir / f"{digest}.json"

    def load(self, request_id: str) -> TurnRecord:
        return self._load(self.turn_path(request_id), TurnRecord.from_dict)

    def save(self, record: TurnRecord, *, expected_revision: int | None = None) -> TurnRecord:
        with self.record_lock("turn", record.request_id):
            path = self.turn_path(record.request_id)
            current_revision = self.load(record.request_id).revision if path.exists() else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise ConcurrentStateError(
                    f"turn revision changed: expected {expected_revision}, found {current_revision}"
                )
            saved = replace(record, revision=current_revision + 1)
            self._atomic_json(path, saved.to_dict())
            return saved

    def load_conversation(self, conversation_id: str) -> ConversationRecord:
        path = self.conversation_path(conversation_id)
        if not path.exists():
            return ConversationRecord.new(conversation_id)
        record = self._load(path, ConversationRecord.from_dict)
        if record.conversation_id != conversation_id:
            raise CorruptStateError("conversation digest record identity mismatch")
        return record

    def save_conversation(
        self, record: ConversationRecord, *, expected_revision: int | None = None
    ) -> ConversationRecord:
        with self.record_lock("conversation-record", record.conversation_id):
            path = self.conversation_path(record.conversation_id)
            current_revision = self.load_conversation(record.conversation_id).revision if path.exists() else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise ConcurrentStateError(
                    "conversation revision changed: "
                    f"expected {expected_revision}, found {current_revision}"
                )
            saved = replace(record, revision=current_revision + 1)
            self._atomic_json(path, saved.to_dict())
            return saved

    def claim_conversation(self, conversation_id: str, request_id: str) -> ConversationRecord:
        with self.record_lock("conversation-record", conversation_id):
            path = self.conversation_path(conversation_id)
            current = self.load_conversation(conversation_id)
            if current.active_request_id not in {None, request_id}:
                raise OwnershipConflictError(
                    f"conversation has active request {current.active_request_id}"
                )
            claimed = replace(
                current.claim(request_id),
                revision=current.revision + 1,
            )
            self._atomic_json(path, claimed.to_dict())
            return claimed

    def release_conversation(
        self, conversation_id: str, request_id: str, *, terminal: bool
    ) -> ConversationRecord:
        with self.record_lock("conversation-record", conversation_id):
            path = self.conversation_path(conversation_id)
            current = self.load_conversation(conversation_id)
            released = replace(
                current.release(request_id, terminal=terminal),
                revision=current.revision + 1,
            )
            self._atomic_json(path, released.to_dict())
            return released

    def find_by_conversation(self, conversation_id: str) -> list[TurnRecord]:
        if not self.turns_dir.exists():
            return []
        records: list[TurnRecord] = []
        for path in sorted(self.turns_dir.glob("*.json")):
            record = self.load(path.stem)
            if record.identity and record.identity.conversation_id == conversation_id:
                records.append(record)
        records.sort(key=lambda item: item.created_at)
        return records

    @contextmanager
    def record_lock(self, kind: str, identity: str) -> Iterator[None]:
        digest = hashlib.sha256(f"{kind}:{identity}".encode("utf-8")).hexdigest()
        path = self.locks_dir / f"{digest}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self, path: Path, parser: Any) -> Any:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("record root must be an object")
            return parser(value)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise CorruptStateError(f"corrupt state preserved at {path}: {exc}") from exc

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cleaned = redact(value)
        encoded = (
            json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
