from __future__ import annotations

import argparse
import ctypes
import json
import multiprocessing
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
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


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _measure(name: str, operation: Callable[[], None], samples: int) -> dict[str, object]:
    values: list[float] = []
    for _ in range(max(10, samples // 20)):
        operation()
    for _ in range(samples):
        started = time.perf_counter_ns()
        operation()
        values.append((time.perf_counter_ns() - started) / 1000.0)
    return {
        "name": name,
        "unit": "us/op",
        "samples": samples,
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "mean": round(statistics.fmean(values), 3),
    }


def _find_library(explicit: Path | None, search_root: Path) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"native runtime library not found: {path}")
        return path
    for name in LIBRARY_NAMES:
        matches = list(search_root.rglob(name)) if search_root.exists() else []
        if matches:
            return matches[0].resolve()
    raise FileNotFoundError(f"native runtime library not found under {search_root}")


def _load_native(path: Path) -> tuple[ctypes.CDLL, Callable[[], int], Callable[[], None]]:
    library = ctypes.CDLL(str(path))
    abi = library.kadoka_runtime_abi_version
    abi.argtypes = []
    abi.restype = ctypes.c_uint32

    validate = library.kadoka_safety_validate_action
    validate.argtypes = [ctypes.POINTER(_SafetyAction), ctypes.POINTER(_SafetyResult)]
    validate.restype = ctypes.c_int32
    action = _SafetyAction(
        ctypes.sizeof(_SafetyAction),
        1,
        100,
        100,
        1920,
        1080,
        0.0,
        1.0,
        0,
        0,
    )
    result = _SafetyResult(ctypes.sizeof(_SafetyResult), 0, 0)

    def validate_once() -> None:
        if validate(ctypes.byref(action), ctypes.byref(result)) != 0 or not result.allowed:
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


def _ipc_worker(connection) -> None:
    try:
        while True:
            value = connection.recv_bytes()
            if value == b"__stop__":
                return
            connection.send_bytes(value)
    finally:
        connection.close()


def _measure_ipc(samples: int) -> dict[str, object]:
    parent, child = multiprocessing.Pipe(duplex=True)
    process = multiprocessing.Process(target=_ipc_worker, args=(child,))
    process.start()
    child.close()
    payload = b"k" * 4096
    try:
        def roundtrip() -> None:
            parent.send_bytes(payload)
            received = parent.recv_bytes()
            if received != payload:
                raise RuntimeError("IPC benchmark payload mismatch")

        result = _measure("ipc_pipe_4k_roundtrip", roundtrip, samples)
        parent.send_bytes(b"__stop__")
        process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
            raise RuntimeError("IPC benchmark worker did not exit")
        if process.exitcode != 0:
            raise RuntimeError(f"IPC benchmark worker exited with {process.exitcode}")
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)


def benchmark(native_library: Path, samples: int) -> dict[str, object]:
    _, abi, validate_once = _load_native(native_library)
    if int(abi()) != 1:
        raise RuntimeError(f"unexpected Native Runtime ABI version: {int(abi())}")

    payload = _representative_payload()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    frame = bytearray(1920 * 1080 * 3)

    def abi_once() -> None:
        if int(abi()) != 1:
            raise RuntimeError("Native Runtime ABI changed during benchmark")

    def json_roundtrip() -> None:
        value = json.loads(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
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
        _measure("ffi_abi_version", abi_once, samples),
        _measure("ffi_safety_validate", validate_once, samples),
        _measure("json_observation_candidate_batch_roundtrip", json_roundtrip, max(100, samples // 10)),
        _measure("frame_1080p_rgb_copy", copy_frame, max(30, samples // 100)),
        _measure("frame_1080p_rgb_memoryview", view_frame, samples),
        _measure_ipc(max(50, samples // 40)),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure language-boundary FFI/IPC/serialization/copy costs")
    parser.add_argument("--native-library", type=Path)
    parser.add_argument("--search-root", type=Path, default=ROOT / "build" / "native-boundary")
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "runtime-boundary" / "benchmark.json")
    parser.add_argument("--samples", type=int, default=2000)
    args = parser.parse_args()
    if args.samples < 100:
        raise ValueError("samples must be at least 100")
    native_library = _find_library(args.native_library, args.search_root)
    report = benchmark(native_library, args.samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    for metric in report["metrics"]:
        print(
            f"{metric['name']}: p50={metric['p50']:.3f}us "
            f"p95={metric['p95']:.3f}us p99={metric['p99']:.3f}us"
        )
    print(f"RUNTIME BOUNDARY BENCHMARK OK: {args.output}")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
