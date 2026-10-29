from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_game_player.event_journal import EVENT_ENVELOPE_SCHEMA_VERSION, EventEnvelope, EventJournal


EVENT_RATE_PER_SECOND = 1
DEFAULT_EVENT_COUNT = 30 * 60 * EVENT_RATE_PER_SECOND
DEFAULT_PAYLOAD_BYTES = 128
DEFAULT_JSONL_FSYNC_BATCH_SIZE = 1
NANOSECONDS_PER_MILLISECOND = 1_000_000
SECONDS_PER_MINUTE = 60
BENCHMARK_SESSION_ID = "benchmark-session"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the production SQLite WAL journal against a JSONL reference"
    )
    parser.add_argument("--events", type=int, default=DEFAULT_EVENT_COUNT)
    parser.add_argument("--payload-bytes", type=int, default=DEFAULT_PAYLOAD_BYTES)
    parser.add_argument(
        "--jsonl-fsync-every",
        type=int,
        default=DEFAULT_JSONL_FSYNC_BATCH_SIZE,
        help="fsync the JSONL reference after this many records (default: each record)",
    )
    arguments = parser.parse_args()
    if arguments.events <= 0 or arguments.payload_bytes < 0 or arguments.jsonl_fsync_every <= 0:
        parser.error("--events and --jsonl-fsync-every must be positive; --payload-bytes cannot be negative")

    with tempfile.TemporaryDirectory(prefix="ai-game-player-event-bench-") as temporary_directory:
        benchmark_root = Path(temporary_directory)
        monotonic_epoch_id = uuid4().hex
        payload = {"sample": "x" * arguments.payload_bytes}
        jsonl_report = _benchmark_jsonl_reference(
            benchmark_root / "events.jsonl",
            arguments.events,
            arguments.jsonl_fsync_every,
            payload,
            monotonic_epoch_id,
        )
        sqlite_report = _benchmark_sqlite_wal(
            benchmark_root / "events.sqlite3",
            arguments.events,
            payload,
        )
        report = {
            "workload": {
                "events": arguments.events,
                "assumed_event_rate_per_second": EVENT_RATE_PER_SECOND,
                "equivalent_session_minutes": round(
                    arguments.events / EVENT_RATE_PER_SECOND / SECONDS_PER_MINUTE,
                    2,
                ),
                "payload_bytes": arguments.payload_bytes,
            },
            "durability_policy": {
                "jsonl": f"flush and fsync after every {arguments.jsonl_fsync_every} record(s)",
                "sqlite_wal": "SQLite synchronous=FULL and a committed transaction per append",
            },
            "backends": {
                "jsonl_reference": jsonl_report,
                "sqlite_wal_production": sqlite_report,
            },
        }
        print(json.dumps(report, indent=2))
    return 0


def _benchmark_jsonl_reference(
    path: Path,
    event_count: int,
    fsync_batch_size: int,
    payload: dict[str, str],
    monotonic_epoch_id: str,
) -> dict[str, float | int]:
    append_latencies_ns: list[int] = []
    pending_event_started_ns: list[int] = []
    write_started_ns = time.perf_counter_ns()
    with path.open("wb") as stream:
        for sequence in range(1, event_count + 1):
            event_started_ns = time.perf_counter_ns()
            event = _create_benchmark_event(sequence, payload, monotonic_epoch_id)
            encoded_event = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            stream.write(encoded_event + b"\n")
            append_latencies_ns.append(0)
            pending_event_started_ns.append(event_started_ns)
            if sequence % fsync_batch_size == 0 or sequence == event_count:
                stream.flush()
                os.fsync(stream.fileno())
                durable_at_ns = time.perf_counter_ns()
                batch_start_index = len(append_latencies_ns) - len(pending_event_started_ns)
                for batch_offset, event_started_ns in enumerate(pending_event_started_ns):
                    append_latencies_ns[batch_start_index + batch_offset] = durable_at_ns - event_started_ns
                pending_event_started_ns.clear()
    write_duration_ns = time.perf_counter_ns() - write_started_ns

    recovery_started_ns = time.perf_counter_ns()
    recovered_count = 0
    expected_sequence = 1
    recovered_event_ids: set[str] = set()
    previous_monotonic_ns_by_epoch: dict[str, int] = {}
    with path.open("rb") as stream:
        stored_lines = stream.readlines()
    for line_number, stored_line in enumerate(stored_lines, start=1):
        if not stored_line.endswith(b"\n") and line_number == len(stored_lines):
            break
        event = EventEnvelope.from_dict(json.loads(stored_line.decode("utf-8")))
        if (
            event.session_id != BENCHMARK_SESSION_ID
            or event.sequence != expected_sequence
            or event.event_id in recovered_event_ids
        ):
            raise RuntimeError(f"JSONL reference recovered an invalid event at line {line_number}")
        previous_monotonic_ns = previous_monotonic_ns_by_epoch.get(event.monotonic_epoch_id)
        if previous_monotonic_ns is not None and event.monotonic_ns < previous_monotonic_ns:
            raise RuntimeError(
                f"JSONL reference recovered decreasing monotonic time at line {line_number}"
            )
        recovered_event_ids.add(event.event_id)
        previous_monotonic_ns_by_epoch[event.monotonic_epoch_id] = event.monotonic_ns
        recovered_count += 1
        expected_sequence += 1
    recovery_duration_ns = time.perf_counter_ns() - recovery_started_ns
    if recovered_count != event_count:
        raise RuntimeError("JSONL reference did not recover the expected event count")
    return _backend_report(
        append_latencies_ns,
        write_duration_ns,
        recovery_duration_ns,
        recovered_count,
        path.stat().st_size,
    )


def _benchmark_sqlite_wal(
    path: Path,
    event_count: int,
    payload: dict[str, str],
) -> dict[str, float | int]:
    append_latencies_ns: list[int] = []
    write_started_ns = time.perf_counter_ns()
    journal = EventJournal(path, session_id=BENCHMARK_SESSION_ID)
    try:
        for sequence in range(1, event_count + 1):
            event_started_ns = time.perf_counter_ns()
            journal.append(
                "benchmark.sample",
                status="ok",
                frame_id=f"frame-{sequence}",
                correlation_id="benchmark-run",
                payload=payload,
            )
            append_latencies_ns.append(time.perf_counter_ns() - event_started_ns)
    finally:
        journal.close()
    write_duration_ns = time.perf_counter_ns() - write_started_ns

    recovery_started_ns = time.perf_counter_ns()
    with EventJournal(path) as recovered_journal:
        recovered_count = recovered_journal.last_sequence
    recovery_duration_ns = time.perf_counter_ns() - recovery_started_ns
    if recovered_count != event_count:
        raise RuntimeError("SQLite WAL journal did not recover the expected event count")
    return _backend_report(
        append_latencies_ns,
        write_duration_ns,
        recovery_duration_ns,
        recovered_count,
        path.stat().st_size,
    )


def _create_benchmark_event(
    sequence: int,
    payload: dict[str, str],
    monotonic_epoch_id: str,
) -> EventEnvelope:
    return EventEnvelope(
        schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
        event_id=uuid4().hex,
        session_id=BENCHMARK_SESSION_ID,
        sequence=sequence,
        timestamp_utc=_current_utc_timestamp(),
        monotonic_ns=time.monotonic_ns(),
        monotonic_epoch_id=monotonic_epoch_id,
        event_type="benchmark.sample",
        status="ok",
        frame_id=f"frame-{sequence}",
        correlation_id="benchmark-run",
        payload=payload,
    )


def _backend_report(
    append_latencies_ns: list[int],
    write_duration_ns: int,
    recovery_duration_ns: int,
    recovered_count: int,
    stored_bytes: int,
) -> dict[str, float | int]:
    sorted_latencies_ns = sorted(append_latencies_ns)
    percentile_95_index = max(0, math.ceil(len(sorted_latencies_ns) * 0.95) - 1)
    write_seconds = write_duration_ns / 1_000_000_000
    return {
        "append_events_per_second": round(len(append_latencies_ns) / write_seconds, 2),
        "append_p50_ms": round(
            _median(sorted_latencies_ns) / NANOSECONDS_PER_MILLISECOND,
            4,
        ),
        "append_p95_ms": round(
            sorted_latencies_ns[percentile_95_index] / NANOSECONDS_PER_MILLISECOND,
            4,
        ),
        "write_duration_ms": round(write_duration_ns / NANOSECONDS_PER_MILLISECOND, 2),
        "recovery_duration_ms": round(recovery_duration_ns / NANOSECONDS_PER_MILLISECOND, 2),
        "recovered_events": recovered_count,
        "stored_bytes": stored_bytes,
    }


def _median(sorted_values: list[int]) -> float:
    value_count = len(sorted_values)
    middle_index = value_count // 2
    if value_count % 2:
        return float(sorted_values[middle_index])
    return (sorted_values[middle_index - 1] + sorted_values[middle_index]) / 2


def _current_utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
