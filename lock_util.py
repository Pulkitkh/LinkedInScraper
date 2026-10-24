"""Cross-thread and cross-process mutex for MS Word COM (docx2pdf) so PDF conversions never overlap."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import settings

_thread_lock = threading.Lock()


class LockTimeout(TimeoutError):
    pass


@contextmanager
def file_lock(path: Path, timeout: float = 120, stale_after: float = 600, poll: float = 0.25):
    """Exclusive lock using an O_EXCL lock file. Lock files older than `stale_after` seconds are broken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"Could not acquire {path} within {timeout}s")
            time.sleep(poll)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def word_com_lock(timeout: float = 120):
    with _thread_lock, file_lock(settings.scratch_dir() / "word_com.lock", timeout=timeout):
        yield
