from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from playwright_gpt_core.errors import OwnershipConflictError
from playwright_gpt_core.locking import ConversationLock


def _hold_lock(root: str, ready: mp.Queue[bool]) -> None:
    with ConversationLock(root, "conversation-1", timeout=1):
        ready.put(True)
        time.sleep(0.8)


def test_competing_conversation_owner_fails(tmp_path) -> None:
    ready: mp.Queue[bool] = mp.Queue()
    process = mp.Process(target=_hold_lock, args=(str(tmp_path), ready))
    process.start()
    assert ready.get(timeout=2) is True
    with pytest.raises(OwnershipConflictError):
        with ConversationLock(tmp_path, "conversation-1", timeout=0.1):
            pass
    process.join(timeout=3)
    assert process.exitcode == 0


def test_process_exit_releases_lock(tmp_path) -> None:
    ready: mp.Queue[bool] = mp.Queue()
    process = mp.Process(target=_hold_lock, args=(str(tmp_path), ready))
    process.start()
    assert ready.get(timeout=2) is True
    process.terminate()
    process.join(timeout=2)
    with ConversationLock(tmp_path, "conversation-1", timeout=1):
        pass
