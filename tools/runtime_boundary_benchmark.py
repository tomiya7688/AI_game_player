"""開発用のC ABI、JSON、Python内の画像コピー、プロセスIPCの時間を測ります。

OS入力は送りません。Native呼出しはABI照会と決定論的なSafety検証だけです。
memoryviewの生成時間はNative bufferのzero-copyや所有権を証明しません。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import multiprocessing
import os
import platform
import sys
from pathlib import Path
from typing import Callable


if __package__:
    from .runtime_boundary_metrics import measure_operation
    from .runtime_boundary_ipc import IPC_TIMEOUT_SECONDS, measure_ipc_roundtrip
else:
    from runtime_boundary_metrics import measure_operation
    from runtime_boundary_ipc import IPC_TIMEOUT_SECONDS, measure_ipc_roundtrip

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NATIVE_ABI_VERSION = 1
NATIVE_ACTION_CLICK = 1
NATIVE_STATUS_OK = 0

LIBRARY_NAMES = (
    "kadoka_native_runtime.dll",
    "libkadoka_native_runtime.so",
    "libkadoka_native_runtime.dylib",
)


class _SafetyAction(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("viewport_width", ctypes.c_int32),
        ("viewport_height", ctypes.c_int32),
        ("hold_seconds", ctypes.c_double),
        ("max_hold_seconds", ctypes.c_double),
        ("key_code", ctypes.c_uint32),
        ("modifiers", ctypes.c_uint32),
    ]


class _SafetyResult(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("allowed", ctypes.c_int32),
        ("reason_code", ctypes.c_uint32),
    ]


def _find_library(explicit: Path | None, search_root: Path) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"native runtime library not found: {path}")
        return path
    matches = (
        sorted(
            {
                path.resolve()
                for path in search_root.rglob("*")
                if path.is_file() and path.name in LIBRARY_NAMES
            }
        )
        if search_root.exists()
        else []
    )
    if len(matches) > 1:
        raise ValueError(
            f"multiple native libraries found; specify --native-library: {matches}"
        )
    if matches:
        return matches[0]
    raise FileNotFoundError(f"native runtime library not found under {search_root}")


def _load_native(
    path: Path,
) -> tuple[ctypes.CDLL, Callable[[], int], Callable[[], None]]:
    library = ctypes.CDLL(str(path))
    abi = library.kadoka_runtime_abi_version
    abi.argtypes = []
    abi.restype = ctypes.c_uint32

    validate = library.kadoka_safety_validate_action
    validate.argtypes = [ctypes.POINTER(_SafetyAction), ctypes.POINTER(_SafetyResult)]
    validate.restype = ctypes.c_int32
    action = _SafetyAction(
        struct_size=ctypes.sizeof(_SafetyAction),
        kind=NATIVE_ACTION_CLICK,
        x=100,
        y=100,
        viewport_width=1920,
        viewport_height=1080,
        hold_seconds=0.0,
        max_hold_seconds=1.0,
        key_code=0,
        modifiers=0,
    )
    result = _SafetyResult(
        struct_size=ctypes.sizeof(_SafetyResult), allowed=0, reason_code=0
    )

    def validate_once() -> None:
        if (
            validate(ctypes.byref(action), ctypes.byref(result)) != NATIVE_STATUS_OK
            or not result.allowed
        ):
            raise RuntimeError("native safety validation benchmark call failed")

    return library, abi, validate_once


def _representative_payload() -> dict[str, object]:
    return {
        "schema": "runtime-boundary-benchmark/v1",
        "observation": {
            "screen_id": "benchmark",
            "width": 1920,
            "height": 1080,
            "ocr_text": ["START", "OPTIONS", "SCORE 1200"],
            "confidence": 0.91,
        },
        "candidates": [
            {
                "action_id": f"candidate-{index}",
                "kind": "click",
                "label": f"item-{index}",
                "x": 20 + index * 7,
                "y": 40 + index * 5,
                "confidence": 0.8,
            }
            for index in range(32)
        ],
    }


def benchmark(
    native_library: Path, samples: int, ipc_timeout_seconds: float = IPC_TIMEOUT_SECONDS
) -> dict[str, object]:
    _, abi, validate_once = _load_native(native_library)
    if int(abi()) != EXPECTED_NATIVE_ABI_VERSION:
        raise RuntimeError(f"unexpected Native Runtime ABI version: {int(abi())}")

    payload = _representative_payload()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    frame = bytearray(1920 * 1080 * 3)

    def abi_once() -> None:
        if int(abi()) != EXPECTED_NATIVE_ABI_VERSION:
            raise RuntimeError("Native Runtime ABI changed during benchmark")

    def json_roundtrip() -> None:
        value = json.loads(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        if value.get("schema") != payload["schema"]:
            raise RuntimeError("JSON boundary roundtrip mismatch")

    def copy_frame() -> None:
        copied = bytes(frame)
        if len(copied) != len(frame):
            raise RuntimeError("frame copy benchmark mismatch")

    def view_frame() -> None:
        view = memoryview(frame)
        if view.nbytes != len(frame):
            raise RuntimeError("frame view benchmark mismatch")

    metrics = [
        measure_operation("ffi_abi_version", abi_once, samples),
        measure_operation("ffi_safety_validate", validate_once, samples),
        measure_operation(
            "json_observation_candidate_batch_roundtrip",
            json_roundtrip,
            max(100, samples // 10),
        ),
        measure_operation("frame_1080p_rgb_copy", copy_frame, max(30, samples // 100)),
        measure_operation("frame_1080p_rgb_memoryview", view_frame, samples),
        measure_ipc_roundtrip(max(50, samples // 40), ipc_timeout_seconds),
    ]
    return {
        "schema": "kadoka-runtime-boundary-benchmark/v1",
        "native_library": str(native_library),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "representative_json_bytes": len(encoded),
        "frame_bytes": len(frame),
        "metrics": metrics,
        "coverage": {
            "component_hot_paths": "performance-ci / tools/performance_check.py",
            "ffi_ipc_serialization_copy": "this report",
            "end_to_end": "language-ci Windows closed-loop sample E2E",
        },
    }


def measure_runtime_boundary_cli() -> int:
    parser = argparse.ArgumentParser(
        description="Measure language-boundary FFI/IPC/serialization/copy costs"
    )
    parser.add_argument("--native-library", type=Path)
    parser.add_argument(
        "--search-root", type=Path, default=ROOT / "build" / "native-boundary"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "runtime-boundary" / "benchmark.json",
    )
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument(
        "--ipc-timeout-seconds", type=float, default=IPC_TIMEOUT_SECONDS
    )
    args = parser.parse_args()
    try:
        if args.samples < 100:
            raise ValueError("samples must be at least 100")
        if not math.isfinite(args.ipc_timeout_seconds) or args.ipc_timeout_seconds <= 0:
            raise ValueError("IPC timeout must be finite and positive")
        native_library = _find_library(args.native_library, args.search_root)
        report = benchmark(native_library, args.samples, args.ipc_timeout_seconds)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        print(f"RUNTIME BOUNDARY BENCHMARK ERROR: {error}", file=sys.stderr)
        return 1
    for metric in report["metrics"]:
        print(
            f"{metric['name']}: p50={metric['p50']:.3f}us "
            f"p95={metric['p95']:.3f}us p99={metric['p99']:.3f}us"
        )
    print(f"RUNTIME BOUNDARY BENCHMARK OK: {args.output}")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(measure_runtime_boundary_cli())
