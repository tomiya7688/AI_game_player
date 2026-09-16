from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def cross_process_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 0.5,
    poll_seconds: float = 0.005,
) -> Iterator[None]:
    """Dependency-free advisory file lock released automatically on process exit."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("file lock timing must be positive")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _lock(stream)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"cross-process file lock timed out: {path}")
                time.sleep(poll_seconds)
        yield
    finally:
        try:
            if locked:
                _unlock(stream)
        finally:
            stream.close()


def _lock(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
