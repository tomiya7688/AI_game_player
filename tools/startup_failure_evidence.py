"""明示opt-in時だけ、起動失敗した所有PIDの窓をcleanup前に有界で保存する。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.screen_capture import ScreenFrame
from tools.recognition_failure_evidence import _write_frame


MAX_WINDOWS = 3
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
DIAGNOSTIC_TIMEOUT_SECONDS = 5.0
CAPTURE_TIMEOUT_MS = 400


class StartupFailureEvidence:
    """既存の失敗保存設定で生成する、一起動専用の診断保存器。"""

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = Path(output_directory)

    def save(self, pid: int, phase_durations_ms: dict[str, float]) -> Path:
        """子processの期限でcapture/保存を制限する。入力や成功条件は変更しない。"""
        if type(pid) is not int or pid <= 0:
            raise ValueError("startup evidence requires an owned process ID")
        self.output_directory.mkdir(parents=True, exist_ok=True)
        directory = Path(
            tempfile.mkdtemp(prefix="startup-failure-", dir=self.output_directory)
        )
        manifest = directory / "manifest.json"
        _publish(
            manifest,
            {
                "schema": "2048-startup-evidence/v1",
                "launched_pid": pid,
                "phase_durations_ms": phase_durations_ms,
                "windows": [],
                "worker_status": "started",
                "pixels_source": "owned_hwnd_wgc_only",
            },
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tools.startup_failure_evidence",
                    "--pid",
                    str(pid),
                    "--output",
                    str(directory),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            status = "completed" if result.returncode == 0 else "worker_failed"
        except subprocess.TimeoutExpired:
            # subprocess.run kills and waits for this owned diagnostic child before returning.
            status = "worker_timeout"
        except OSError:
            status = "worker_start_failed"
        value = _read_manifest(manifest)
        value.update(
            worker_status=status,
            diagnostic_duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        _publish(manifest, value)
        return manifest


def _publish(path: Path, value: dict) -> None:
    """既存atomic JSON保存を使い、途中で終了しても直前のmanifestを残す。"""
    staged = stage_json_write(path, value)
    try:
        staged.publish()
    finally:
        staged.discard()


def _read_manifest(path: Path) -> dict:
    with path.open("rb") as stream:
        payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("startup evidence manifest exceeds budget")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("schema") != "2048-startup-evidence/v1":
        raise ValueError("invalid startup evidence manifest")
    return value


def _is_owned(handle: int, pid: int, user32=None) -> bool:
    """取得前後にHWNDの所有PIDを照合する。入力targetを登録しない。"""
    from ai_game_player.windows_abi import configure_user32_prototypes

    api = user32 or ctypes.windll.user32
    configure_user32_prototypes(api)
    owner = ctypes.c_ulong()
    return bool(
        api.IsWindow(handle)
        and api.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        and owner.value == pid
    )


def _owned_windows(pid: int) -> list[dict]:
    """所有窓のみ最大3件へ制限し、生タイトルを固定分類へ落として破棄する。"""
    from ai_game_player.windows_abi import (
        WINDOW_ENUM_CALLBACK,
        configure_user32_prototypes,
    )

    api = ctypes.windll.user32
    configure_user32_prototypes(api)
    api.IsWindowVisible.argtypes = [ctypes.c_void_p]
    api.IsWindowVisible.restype = ctypes.c_int
    records = []

    def inspect_owned(handle, _parameter):
        if not _is_owned(handle, pid, api):
            return True
        rect = (ctypes.c_long * 4)()
        if not api.GetWindowRect(handle, ctypes.byref(rect)):
            return True
        # Untitled startup windows must be included; the ordinary target selector excludes them.
        title_buffer = ctypes.create_unicode_buffer(512)
        api.GetWindowTextW(handle, title_buffer, len(title_buffer))
        title = title_buffer.value.casefold()
        category = (
            "game_title"
            if "2048" in title
            else "browser_title"
            if any(name in title for name in ("microsoft edge", "google chrome"))
            else "other_owned_title"
            if title
            else "untitled"
        )
        records.append(
            {
                "handle": int(handle),
                "visible": bool(api.IsWindowVisible(handle)),
                "rect": list(rect),
                "title_class": category,
            }
        )
        records.sort(key=lambda item: (not item["visible"], item["handle"]))
        del records[MAX_WINDOWS:]
        return True

    if not api.EnumWindows(WINDOW_ENUM_CALLBACK(inspect_owned), 0):
        raise RuntimeError("startup owned-window enumeration failed")
    return records


def _capture_owned(handle: int, pid: int, byte_budget: int) -> ScreenFrame:
    """既存WGCだけを使い、desktop矩形のGDI fallbackを診断画像へ混ぜない。"""
    from ai_game_player.runtime.native import NativeLoadStatus, discover_native_runtime

    if not _is_owned(handle, pid):
        raise ValueError("startup window ownership changed")
    loaded = discover_native_runtime()
    if loaded.status != NativeLoadStatus.AVAILABLE or loaded.runtime is None:
        raise RuntimeError("startup WGC capture unavailable")
    with loaded.runtime as runtime:
        with runtime.open_wgc_window(handle) as capture:
            frame = capture.capture(
                timeout_ms=CAPTURE_TIMEOUT_MS, max_frame_bytes=byte_budget
            )
            if not _is_owned(handle, pid):
                raise ValueError("startup window ownership changed")
            return frame


def collect_evidence(
    directory: Path, pid: int, *, enumerate_windows=None, capture=None
) -> None:
    """worker内でmetadataを先に保存し、窓別失敗でも残りの診断を継続する。"""
    manifest = directory / "manifest.json"
    value = _read_manifest(manifest)
    if value.get("launched_pid") != pid:
        raise ValueError("startup evidence process mismatch")
    records = (enumerate_windows or _owned_windows)(pid)[:MAX_WINDOWS]
    value["windows"] = records
    _publish(manifest, value)
    remaining = MAX_TOTAL_FRAME_BYTES
    started = time.monotonic()
    for index, record in enumerate(records):
        record["capture_status"] = "not_attempted"
        rect = record["rect"]
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        budget = min(MAX_FRAME_BYTES, remaining)
        if not record["visible"]:
            record["capture_status"] = "hidden_window"
        elif width <= 0 or height <= 0 or width * height * 4 > budget:
            record["capture_status"] = "size_budget"
        else:
            capture_started = time.monotonic()
            try:
                frame = (capture or _capture_owned)(record["handle"], pid, budget)
                if (
                    frame.width <= 0
                    or frame.height <= 0
                    or len(frame.bgra) != frame.width * frame.height * 4
                    or len(frame.bgra) > budget
                ):
                    raise ValueError("startup frame exceeds packed pixel budget")
                # Reserve the attempt even if the writer fails after publishing a partial file.
                remaining -= len(frame.bgra)
                # The existing recognition writer supplies PNG/BGRA hashes and capture metadata.
                record["frame"] = _write_frame(directory, f"owned-{index}", frame, {})
                record["capture_status"] = "captured"
            except Exception as error:
                record["capture_status"] = "unavailable"
                record["error_type"] = type(error).__name__
            record["capture_duration_ms"] = round(
                (time.monotonic() - capture_started) * 1000, 3
            )
        value["collection_duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        _publish(manifest, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" or args.pid <= 0:
        return 2
    collect_evidence(args.output, args.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
