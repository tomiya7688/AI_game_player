"""294 disposable IPC comparison; no game/input/network; Linux timings aren't Windows proof."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
import tracemalloc

from tools.browser_probe_support import publish_validation_report

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.planning_file_publisher import replace_planning_file
from ai_game_player.planning_file_reader import read_planning_bytes
from ai_game_player.planning_wire import (
    _check_json_nesting,
    decode_planning_json,
    publish_planning_json,
    read_planning_json,
)


def legacy_publish(path, value, maximum):
    # Exact former 294 pipeline: check encoded bytes, serialize again with fsync, rename.
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode(
        "utf-8"
    )
    if len(encoded) > maximum:
        raise ValueError("planning message byte budget exceeded")
    _check_json_nesting(encoded)
    staged = stage_json_write(path, value)
    try:
        replace_planning_file(staged.temporary_path, staged.target_path)
    finally:
        staged.discard()


def legacy_read(path, maximum):
    raw = read_planning_bytes(path, maximum)
    return raw, read_planning_json(path, maximum)


def single_read(path, maximum):
    raw = read_planning_bytes(path, maximum)
    return raw, decode_planning_json(raw, maximum)


def summarize(rows):
    result = {}
    for name in ("wall_ms", "cpu_ms"):
        values = sorted(row[name] for row in rows)
        result[name] = {
            "p50": statistics.median(values),
            "p95": values[(95 * len(values) + 99) // 100 - 1],
            "min": min(values),
            "max": max(values),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("build/planning-disposable-ipc/report.json")
    )
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 1000:
        parser.error("iterations must be in [1,1000]")
    repetitions = args.iterations
    root = Path(__file__).resolve().parents[1]
    samples = []
    with tempfile.TemporaryDirectory(prefix="planning-ipc-benchmark-") as directory:
        path = Path(directory) / "checkpoint.json"
        for string_size in (1024, 16384, 65536):
            value = {
                "nonce": "0" * 32,
                "data": "x" * string_size,
                "probabilities": [0.9, 0.1],
            }
            encoded = json.dumps(
                value, ensure_ascii=False, indent=2, allow_nan=False
            ).encode("utf-8")
            maximum = len(encoded)
            methods = {
                "previous_durable_double_encode": lambda: legacy_publish(
                    path, value, maximum
                ),
                "disposable_single_encode": lambda: publish_planning_json(
                    path, value, maximum
                ),
                "previous_double_read": lambda: legacy_read(path, maximum),
                "single_read_and_decode": lambda: single_read(path, maximum),
            }
            for method in methods.values():
                for _ in range(3):
                    method()
            rows = {name: [] for name in methods}
            for index in range(repetitions):
                ordered = list(methods.items())
                if index % 2:
                    ordered.reverse()
                for name, method in ordered:
                    started_cpu = time.process_time_ns()
                    started = time.perf_counter_ns()
                    method()
                    rows[name].append(
                        {
                            "wall_ms": (time.perf_counter_ns() - started) / 1e6,
                            "cpu_ms": (time.process_time_ns() - started_cpu) / 1e6,
                        }
                    )
                assert path.read_bytes() == encoded
                assert read_planning_json(path, maximum) == value
            summary = {}
            for name, method in methods.items():
                tracemalloc.start()
                for _ in range(3):
                    method()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                summary[name] = {
                    **summarize(rows[name]),
                    "tracemalloc_peak_bytes": peak,
                }
            samples.append({"encoded_bytes": maximum, "methods": summary})
        result = {
            "schema_version": 1,
            "legacy_pipeline_reference_sha": "294cab92d6970aa1cd3485f21d180786d76c848b",
            "base_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip(),
            "candidate_diff_sha256": sha256(
                subprocess.check_output(["git", "diff", "--", "src"], cwd=root)
            ).hexdigest(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "filesystem": subprocess.check_output(
                ["stat", "-f", "-c", "%T", directory], text=True
            ).strip()
            if os.name == "posix"
            else "unmeasured",
            "repetitions": repetitions,
            "warmup": 3,
            "seed": None,
            "tracemalloc_repetitions_separate_from_latency": 3,
            "scope": "planning disposable IPC only; no actual game/input; not Windows latency or causal fsync proof",
            "samples": samples,
        }
    archived = publish_validation_report(args.output, result)
    print(json.dumps({"latest": str(args.output), "archive": str(archived)}))
    for sample in samples:
        print(
            sample["encoded_bytes"],
            {
                name: tuple(
                    round(value["wall_ms"][quantile], 3) for quantile in ("p50", "p95")
                )
                for name, value in sample["methods"].items()
            },
        )


if __name__ == "__main__":
    main()
