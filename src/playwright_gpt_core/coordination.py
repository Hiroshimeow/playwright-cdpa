from __future__ import annotations

import os
from pathlib import Path

from .locking import ConversationLock
from .models import ConversationRecord
from .storage import StateStore


class CoordinationStore:
    """Deployment-scoped conversation ownership, separate from local turn results."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        for child in (self.root / "conversations", self.root / "locks"):
            child.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(child, 0o700)
        self._store = StateStore(self.root)

    def lock(self, conversation_id: str, *, timeout: float = 0.0) -> ConversationLock:
        return ConversationLock(self.root, conversation_id, timeout=timeout)

    def load(self, conversation_id: str) -> ConversationRecord:
        return self._store.load_conversation(conversation_id)

    def claim(self, conversation_id: str, request_id: str) -> ConversationRecord:
        return self._store.claim_conversation(conversation_id, request_id)

    def release(
        self, conversation_id: str, request_id: str, *, terminal: bool
    ) -> ConversationRecord:
        return self._store.release_conversation(conversation_id, request_id, terminal=terminal)

    def release_if_owned(
        self, conversation_id: str, request_id: str, *, terminal: bool
    ) -> ConversationRecord:
        return self._store.release_conversation_if_owned(
            conversation_id, request_id, terminal=terminal
        )
