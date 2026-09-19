from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


DEFAULT_LIMIT_MIB = 6144
DEFAULT_SAMPLE_SECONDS = 0.25


def parse_memory_used_mib(text: str) -> list[int] | None:
    values: list[int] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value:
            continue
        if value.casefold() in {"n/a", "na", "not supported"}:
            return None
        try:
            values.append(int(value))
        except ValueError:
            return None
    return values or None


def query_total_vram_used_mib() -> int | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    values = parse_memory_used_mib(result.stdout)
    return sum(values) if values is not None else None


def run_with_vram_budget(
    command: Sequence[str],
    *,
    limit_mib: int = DEFAULT_LIMIT_MIB,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
) -> tuple[int, dict[str, object]]:
    if not command:
        raise ValueError("command must not be empty")
    if limit_mib <= 0:
        raise ValueError("limit_mib must be positive")
    if sample_seconds <= 0:
        raise ValueError("sample_seconds must be positive")

    baseline = query_total_vram_used_mib()
    measurement_available = baseline is not None
    peak_total = baseline if baseline is not None else None

    started = time.monotonic()
    process = subprocess.Popen(list(command))

    while process.poll() is None:
        if measurement_available:
            current = query_total_vram_used_mib()
            if current is None:
                measurement_available = False
                peak_total = None
            else:
                peak_total = max(int(peak_total), current)
        time.sleep(sample_seconds)

    exit_code = int(process.returncode or 0)
    if measurement_available:
        final = query_total_vram_used_mib()
        if final is None:
            measurement_available = False
            peak_total = None
        else:
            peak_total = max(int(peak_total), final)

    peak_increment = None
    budget_exceeded = False
    if measurement_available and baseline is not None and peak_total is not None:
        peak_increment = max(0, peak_total - baseline)
        budget_exceeded = peak_increment > limit_mib

    report: dict[str, object] = {
        "schema": "kadoka.vram-budget-result/1",
        "measurement_available": measurement_available,
        "measurement_method": "nvidia-smi total memory.used baseline delta",
        "baseline_total_mib": baseline if measurement_available else None,
        "peak_total_mib": peak_total if measurement_available else None,
        "peak_increment_mib": peak_increment,
        "limit_mib": limit_mib,
        "budget_exceeded": budget_exceeded,
        "command_exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }

    if exit_code != 0:
        return exit_code, report
    if budget_exceeded:
        return 2, report
    return 0, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one Kadoka acceptance command and, when NVIDIA VRAM telemetry is "
            "available, fail if its peak total VRAM increase exceeds the budget."
        )
    )
    parser.add_argument("--limit-mib", type=int, default=DEFAULT_LIMIT_MIB)
    parser.add_argument("--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    exit_code, report = run_with_vram_budget(
        command,
        limit_mib=args.limit_mib,
        sample_seconds=args.sample_seconds,
    )

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(serialized)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")

    if not report["measurement_available"]:
        print(
            "VRAM measurement unavailable; functional command result remains authoritative.",
            file=sys.stderr,
        )
    elif report["budget_exceeded"]:
        print(
            f"VRAM budget exceeded: {report['peak_increment_mib']} MiB > "
            f"{report['limit_mib']} MiB.",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
