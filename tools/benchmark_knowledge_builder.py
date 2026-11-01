"""Measure bounded Knowledge candidate updates without models, input or history scans."""

import argparse
import json
from pathlib import Path
import platform
import random
import statistics
import time
import tracemalloc

from ai_game_player.experience import ExperienceEvent, ExperienceProvenance
from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_scope import KnowledgeScope


CREATED_AT = "2026-11-01T00:00:00Z"


def event_stream(scope, first, count, seed):
    rng = random.Random(seed)
    for sequence in range(first, first + count):
        action = f"action.{sequence % 8}"
        payload = {
            "scope": scope.to_dict(),
            "source_id": "benchmark.session",
            "source_sequence": sequence,
            "correlation_id": "benchmark.actions",
            "correlation_sequence": sequence,
            "action_generation": sequence,
            "execution_status": "executed",
            "action_id": action,
            "outcome": rng.choice(("success", "no_effect", "failure", "unknown")),
            "evidence_kind": "deterministic_action_effect",
            "verified": True,
            "confidence": 0.9,
            "candidate_reliability": 0.9,
            "from_state": "playing",
            "to_state": f"state.{sequence % 4}",
        }
        yield ExperienceEvent(
            f"event.{sequence}",
            "knowledge.action_outcome",
            CREATED_AT,
            "benchmark",
            payload,
            provenance=ExperienceProvenance(
                "deterministic_action_effect",
                confidence=0.9,
                evidence_event_id=f"receipt.{sequence}",
            ),
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=530)
    parser.add_argument(
        "--output", type=Path, default=Path("build/knowledge-builder/report.json")
    )
    args = parser.parse_args(argv)
    if not 10 <= args.iterations <= 1000 or not 1 <= args.batch_size <= 256:
        parser.error("iterations must be 10..1000 and batch-size must be 1..256")
    scope = KnowledgeScope("benchmark", "playing", "default", "player", "lineage")
    builder = KnowledgeBuilder()
    base_events = tuple(event_stream(scope, 1, args.batch_size, args.seed))
    previous = builder.build(
        None, base_events, scope=scope, version="base", created_at=CREATED_AT
    ).snapshot
    update_events = tuple(
        event_stream(scope, args.batch_size + 1, args.batch_size, args.seed + 1)
    )
    reports = {}
    for name, batch in (
        ("new_evidence", update_events),
        ("duplicate_evidence", base_events),
    ):
        samples = []
        started_cpu = time.process_time_ns()
        for _ in range(args.iterations):
            started = time.perf_counter_ns()
            result = builder.build(
                previous, batch, scope=scope, version="candidate", created_at=CREATED_AT
            )
            samples.append((time.perf_counter_ns() - started) / 1000)
        cpu_ms = (time.process_time_ns() - started_cpu) / 1_000_000
        tracemalloc.start()
        measured = builder.build(
            previous, batch, scope=scope, version="candidate", created_at=CREATED_AT
        )
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        reports[name] = {
            "p50_us": statistics.median(samples),
            "p95_us": sorted(samples)[int(len(samples) * 0.95) - 1],
            "cpu_ms_all_iterations": cpu_ms,
            "retained_traced_bytes": retained,
            "peak_traced_bytes": peak,
            "snapshot_bytes": len(measured.snapshot.to_bytes()),
            "accepted_events": result.accepted_events,
            "duplicate_events": result.duplicate_events,
            "same_snapshot_object": result.snapshot is previous,
        }
    report = {
        "schema_version": 1,
        "seed": args.seed,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "iterations": args.iterations,
        "batch_size": args.batch_size,
        "policy_fingerprint": builder.policy.fingerprint,
        "measurements": reports,
        "scope": "pure candidate builder; no Runtime publication or real gameplay",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
