from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import TracebackType

from .errors import OwnershipConflictError


class FileLock:
    def __init__(self, path: str | Path, *, timeout: float = 0.0) -> None:
        self.path = Path(path)
        self.timeout = max(0.0, timeout)
        self._handle = None

    def acquire(self) -> None:
        import portalocker

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                self._handle = handle
                return
            except portalocker.exceptions.LockException as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise OwnershipConflictError("resource is owned by another process") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if self._handle is None:
            return
        import portalocker

        try:
            portalocker.unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class ConversationLock(FileLock):
    def __init__(self, root: str | Path, conversation_id: str, *, timeout: float = 0.0) -> None:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        super().__init__(Path(root) / "locks" / f"conversation-{digest}.lock", timeout=timeout)
