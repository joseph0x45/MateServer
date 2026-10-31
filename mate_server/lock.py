"""One server per data directory.

Two servers on one data directory would mean two pollers on one SQLite file and two cloud sessions
on one account, evicting each other. An OS file lock is held for the process's whole life; the OS
releases it however the process dies, so a stale lock can never wedge the service shut.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_handle = None


def acquire(data_dir: Path) -> bool:
    global _handle
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "server.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _handle = f
    return True


def release() -> None:
    global _handle
    if _handle is not None:
        try:
            _handle.close()
        finally:
            _handle = None
