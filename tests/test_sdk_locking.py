from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from playwright_api.errors import OwnershipConflictError
from playwright_api.locking import FileLock


def test_package_import_does_not_require_fcntl() -> None:
    script = r'''
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("fcntl unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import playwright_api
print(playwright_api.ChatGPTClient.__name__)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ChatGPTClient"


def test_file_lock_times_out_while_competing_process_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "shared.lock"
    with FileLock(path, timeout=0):
        with pytest.raises(OwnershipConflictError):
            with FileLock(path, timeout=0.02):
                pass


def test_file_lock_releases_after_context_exit(tmp_path: Path) -> None:
    path = tmp_path / "shared.lock"
    with FileLock(path, timeout=0):
        pass
    with FileLock(path, timeout=0.02):
        pass
