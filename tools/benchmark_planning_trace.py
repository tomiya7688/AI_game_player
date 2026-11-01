"""Controlled trace-cache copy cost with identical synthetic planning data; no play claims."""

import argparse
import copy
from dataclasses import asdict, replace
import gc
import json
from pathlib import Path
import statistics
import subprocess
from hashlib import sha256
import tempfile
import time
import tracemalloc

from ai_game_player.decision_context import DecisionContextBuilder, DecisionTraceStore
from ai_game_player.event_journal import EventJournal
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.planning_result import WeightedValue
from tools.browser_probe_support import new_probe_directory, publish_validation_report
from tools.planning_comparison_source import source_fingerprint
from tools.planning_policy_fixture import make_belief, make_plan


def measure(records, samples):
    times = []
    for _ in range(samples):
        gc.collect()
        start = time.perf_counter_ns()
        copied = copy.deepcopy(records)
        times.append((time.perf_counter_ns() - start) / 1e6)
        del copied
    gc.collect()
    tracemalloc.start()
    copied = copy.deepcopy(records)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del copied
    return {
        "copy_p50_ms": statistics.median(times),
        "copy_p95_ms": sorted(times)[int((len(times) - 1) * 0.95)],
        "peak_copy_allocation_bytes": peak,
        "serialized_bytes": len(json.dumps(records).encode()),
        "samples_ms": times,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=180)
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument(
        "--output", type=Path, default=Path("build/planning-comparison/trace-cost")
    )
    args = parser.parse_args(argv)
    if not 1 <= args.records <= 500 or not 3 <= args.samples <= 31:
        parser.error("bounded records1..500/samples3..31 required")
    belief = make_belief()
    distributions = {
        action: tuple(
            tuple(
                WeightedValue((index / 31) * 0.6 + 0.2, 1 / 32) for index in range(32)
            )
            for _ in belief.candidates
        )
        for action in ("a", "b")
    }
    prediction = make_plan(belief, distributions)
    audit = {
        "schema": "planning.runtime/v1",
        "status": "select",
        "prediction": asdict(prediction),
        "actual_outcome": False,
    }
    candidates = [
        ActionCandidate("a", "key", "LEFT", confidence=1),
        ActionCandidate("b", "key", "RIGHT", confidence=1),
    ]
    observation = ScreenObservation(
        "fixture", 100, 100, features={"signature": "stable"}
    )
    context = replace(
        DecisionContextBuilder().build(observation, candidates, candidates),
        snapshot_id=belief.context.snapshot_id,
    )
    full = {
        "snapshot_id": context.snapshot_id,
        "context": context.to_dict(),
        "planning_prediction": audit,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        with EventJournal(path / "events.db") as journal:
            store = DecisionTraceStore(
                path / "trace.json",
                event_adapter=LegacyEventAdapter(
                    journal, path / "trace.json", "decision.trace", migrate_legacy=False
                ),
            )
            reference = {
                **full,
                "planning_prediction": store._planning_trace_value(context, audit),
            }
    output = new_probe_directory(args.output)
    report = {
        "schema_version": 1,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "git_status": subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ),
        "source_at_start": source_fingerprint(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "synthetic deepcopy of Journal adapter cache; not end-to-end latency or actual play",
        "fixture": "same two-world/two-action/32-chance-atom result for both layouts; only full-payload vs stable eventref differs",
        "records": args.records,
        "samples": args.samples,
        "full": measure(
            [copy.deepcopy(full) for _ in range(args.records)], args.samples
        ),
        "reference": measure(
            [copy.deepcopy(reference) for _ in range(args.records)], args.samples
        ),
    }
    report["source_at_end"] = source_fingerprint()
    report["source_consistent"] = report["source_at_start"] == report["source_at_end"]
    publish_validation_report(output / "report.json", report)
    print(output / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
