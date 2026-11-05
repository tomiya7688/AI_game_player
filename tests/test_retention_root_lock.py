import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_game_player.retention_root_lock import (
    RETENTION_LOCK_FILENAME,
    RetentionLockTimeoutError,
    RetentionRootUnsafeError,
    retention_root_lock,
)

SRC = Path(__file__).parents[1] / "src"
HOLD_LOCK_SCRIPT = """
import sys
from pathlib import Path
from ai_game_player.retention_root_lock import retention_root_lock
with retention_root_lock(Path(sys.argv[1]), timeout_seconds=10):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def make_directory_link(link: Path, target: Path) -> None:
    """Windowsはjunction、それ以外はsymlinkでdirectoryへのリンクを作る。"""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def start_lock_holder(root: Path) -> subprocess.Popen:
    environment = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
    process = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK_SCRIPT, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=root.parent,
    )
    if process.stdout.readline().strip() != "locked":
        process.kill()
        raise AssertionError("lock holder process did not acquire the lock")
    return process


def close_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


class RetentionRootLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"

    def test_lock_creates_root_and_reports_wait_milliseconds(self) -> None:
        with retention_root_lock(self.root, timeout_seconds=1) as waited_ms:
            self.assertGreaterEqual(waited_ms, 0.0)
            self.assertTrue((self.root / RETENTION_LOCK_FILENAME).is_file())

    def test_same_thread_can_reenter_and_lock_is_released_after_outermost(self) -> None:
        with retention_root_lock(self.root, timeout_seconds=1):
            with retention_root_lock(self.root, timeout_seconds=1):
                pass
            blocked = self._try_from_other_thread(timeout_seconds=0.1)
            self.assertIsInstance(blocked, RetentionLockTimeoutError)
        self.assertIsNone(self._try_from_other_thread(timeout_seconds=1))

    def test_other_thread_waits_boundedly_then_times_out(self) -> None:
        started = time.monotonic()
        with retention_root_lock(self.root, timeout_seconds=1):
            result = self._try_from_other_thread(timeout_seconds=0.2)
        self.assertIsInstance(result, RetentionLockTimeoutError)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_other_process_holding_the_lock_blocks_until_it_exits(self) -> None:
        holder = start_lock_holder(self.root)
        self.addCleanup(close_process, holder)
        started = time.monotonic()
        with self.assertRaises(RetentionLockTimeoutError):
            with retention_root_lock(self.root, timeout_seconds=0.3):
                self.fail("lock must not be granted while another process holds it")
        self.assertLess(time.monotonic() - started, 3.0)
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        with retention_root_lock(self.root, timeout_seconds=2):
            pass

    def test_lock_is_released_when_the_holder_process_crashes(self) -> None:
        holder = start_lock_holder(self.root)
        self.addCleanup(close_process, holder)
        holder.kill()
        holder.wait(timeout=10)
        with retention_root_lock(self.root, timeout_seconds=2):
            pass

    def test_invalid_timeout_is_rejected(self) -> None:
        for value in (0, -1, True, "1", None, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    with retention_root_lock(self.root, timeout_seconds=value):
                        pass

    def test_root_that_is_a_directory_link_is_rejected(self) -> None:
        real = Path(self.temp.name) / "real"
        real.mkdir()
        link = Path(self.temp.name) / "link"
        make_directory_link(link, real)
        with self.assertRaises(RetentionRootUnsafeError):
            with retention_root_lock(link, timeout_seconds=1):
                pass
        self.assertFalse((real / RETENTION_LOCK_FILENAME).exists())

    def test_lock_file_that_is_a_symlink_is_rejected(self) -> None:
        self.root.mkdir()
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"x")
        try:
            os.symlink(outside, self.root / RETENTION_LOCK_FILENAME)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"cannot create symlink: {error}")
        with self.assertRaises(RetentionRootUnsafeError):
            with retention_root_lock(self.root, timeout_seconds=1):
                pass
        self.assertEqual(outside.read_bytes(), b"x")

    def _try_from_other_thread(self, *, timeout_seconds: float):
        outcome: list[BaseException | None] = []

        def attempt() -> None:
            try:
                with retention_root_lock(self.root, timeout_seconds=timeout_seconds):
                    outcome.append(None)
            except BaseException as error:  # noqa: BLE001 - 結果として検査する
                outcome.append(error)

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        return outcome[0]


if __name__ == "__main__":
    unittest.main()
