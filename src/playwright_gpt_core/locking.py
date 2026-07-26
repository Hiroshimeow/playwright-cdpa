from __future__ import annotations

import fcntl
import hashlib
import os
import time
from pathlib import Path
from types import TracebackType

from .errors import OwnershipConflictError


class ConversationLock:
    def __init__(self, root: str | Path, conversation_id: str, *, timeout: float = 0.0) -> None:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        self.path = Path(root) / "locks" / f"conversation-{digest}.lock"
        self.timeout = timeout
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise OwnershipConflictError("conversation is owned by another sender")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ConversationLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
