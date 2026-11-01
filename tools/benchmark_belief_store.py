"""CPU-only Belief contract/Storeのcost・p50/p95・保持量を再現可能に測る。"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import time
import tracemalloc

from ai_game_player.belief_candidate_store import (
    BeliefCandidateStore,
    BeliefStorePolicy,
)
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)


@dataclass(frozen=True)
class BenchmarkState:
    tokens: tuple[int, ...]


def benchmark(iterations: int, candidate_count: int, capacity: int) -> dict:
    if (
        not 2 <= iterations <= 10000
        or not 1 <= candidate_count <= 256
        or not 1 <= capacity <= 256
    ):
        raise ValueError("benchmark sizes are out of range")
    clock_id = "benchmark-process-clock"
    policy = BeliefStorePolicy(
        max_candidates=capacity,
        max_input_candidates=max(capacity, candidate_count),
        max_update_ns=1_000_000_000,
    )
    store = BeliefCandidateStore(BenchmarkState, clock_id=clock_id, policy=policy)
    samples = []
    peak_candidates = peak_input = peak_bytes = 0
    started_cpu = time.process_time_ns()
    tracemalloc.start()
    try:
        for sequence in range(iterations):
            started = time.perf_counter_ns()
            observed = time.monotonic_ns()
            context = BeliefContext(
                "synthetic",
                "scene",
                "session",
                f"snapshot{sequence}",
                (f"frame{sequence}",),
                sequence,
                sequence,
                1,
                "capture",
                1,
                clock_id,
            )
            evidence = BeliefEvidenceRef(
                f"evidence{sequence}",
                f"frame{sequence}",
                BeliefEvidenceKind.OBSERVATION,
                "fake-recognizer",
                "v1",
                context,
                observed,
                context.frame_ids[0],
            )
            candidates = tuple(
                WorldStateHypothesis(
                    f"h{index}",
                    TypedStateRef(
                        "synthetic",
                        "state-v1",
                        BenchmarkState,
                        BenchmarkState((index, sequence % 16)),
                    ),
                    context,
                    BeliefWeight(
                        BeliefWeightKind.CALIBRATED_PROBABILITY,
                        0.9 / candidate_count,
                        "synthetic-calibration-v1",
                    ),
                    HypothesisOrigin.OBSERVATION_HYPOTHESIS,
                    (evidence,),
                    BeliefFreshness(observed, observed + policy.max_age_ns),
                )
                for index in range(candidate_count)
            )
            snapshot = store.publish(context, candidates, unknown_mass=0.1)
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
            peak_candidates = max(peak_candidates, len(snapshot.candidates))
            peak_input = max(peak_input, snapshot.diagnostics.peak_candidates)
            peak_bytes = max(peak_bytes, len(snapshot.to_bytes()))
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "schema_version": 1,
        "iterations": iterations,
        "input_candidates": candidate_count,
        "capacity": capacity,
        "scope": "CPU synthetic adapter + immutable typed-state validation + candidate store; tracemalloc enabled",
        "update_p50_ms": statistics.median(samples),
        "update_p95_ms": sorted(samples)[max(0, int(len(samples) * 0.95 + 0.9999) - 1)],
        "cpu_total_ms": (time.process_time_ns() - started_cpu) / 1_000_000,
        "traced_current_bytes": current,
        "traced_peak_bytes": peak,
        "peak_retained_candidates": peak_candidates,
        "peak_input_candidates": peak_input,
        "peak_snapshot_bytes": peak_bytes,
        "snapshot_byte_budget": policy.max_snapshot_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark(args.iterations, args.candidates, args.capacity)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
