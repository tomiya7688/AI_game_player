from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class CrossProcessFileLock:
    """Small dependency-free advisory file lock released automatically on process exit."""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.5, poll_seconds: float = 0.005) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("file lock timing must be positive")
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._stream: BinaryIO | None = None
        self._locked = False

    def __enter__(self) -> "CrossProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        self._stream = stream
        if os.name == "nt":
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._lock(stream)
                self._locked = True
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    stream.close()
                    self._stream = None
                    raise TimeoutError(f"cross-process file lock timed out: {self.path}")
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if self._locked:
                self._unlock(stream)
        finally:
            self._locked = False
            stream.close()

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
