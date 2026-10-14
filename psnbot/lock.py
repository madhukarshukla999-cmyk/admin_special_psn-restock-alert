"""Make sure only one copy of the bot runs per folder.

Two copies would send every alert twice and fight over Telegram's /menu (HTTP 409).
The OS releases the lock automatically if the process dies.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, path: str | Path, wait: float = 0):
        self.path = Path(path)
        self.wait = wait        # seconds to wait for a previous copy to exit (e.g. after auto-update)
        self._fh = None

    def _try_lock(self) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.wait
        while not self._try_lock():
            if time.monotonic() >= deadline:
                self._fh.close()
                raise AlreadyRunning(f"Another copy of the bot is already running (lock: {self.path})")
            time.sleep(1)
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
