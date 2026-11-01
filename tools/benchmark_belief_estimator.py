"""CPU-only fake/intと曖昧2048 fixtureでBeliefの各更新段階を計測する。"""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time
import tracemalloc
from typing import TypeVar
from uuid import uuid4

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
from ai_game_player.belief_estimator import (
    BayesianBeliefEstimator,
    BeliefEstimateResult,
)
from ai_game_player.belief_likelihood import (
    BeliefActionRef,
    BeliefEstimatorPolicy,
    BeliefLikelihoodBatch,
)
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import ReferenceGame2048TransitionModel
from ai_game_player.transition_model import (
    ActionTransition,
    ChanceOutcome,
    TransitionModel,
)
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)
from tools.browser_probe_support import publish_validation_report


StateT = TypeVar("StateT")
ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    "src/ai_game_player/belief_estimator.py",
    "src/ai_game_player/belief_likelihood.py",
    "src/ai_game_player/belief_prediction.py",
    "src/ai_game_player/belief_candidate_store.py",
    "src/ai_game_player/belief_contract.py",
    "src/ai_game_player/belief_validation.py",
    "src/ai_game_player/belief_snapshot.py",
    "src/ai_game_player/world_state_hypothesis.py",
    "src/ai_game_player/game_2048_transition.py",
    "src/ai_game_player/game_2048_state.py",
    "src/ai_game_player/transition_model.py",
    "tools/benchmark_belief_estimator.py",
)


class IntTransitionModel:
    """Pure synthetic transition; overlapping exclusive paths exercise world merging."""

    def apply_action(self, state: int, action_id: str) -> ActionTransition[int]:
        if action_id != "increment":
            raise ValueError("unsupported synthetic action")
        return ActionTransition(state + 1, True, 0.0)

    def chance_outcomes(
        self, after_action: ActionTransition[int]
    ) -> tuple[ChanceOutcome[int], ...]:
        return (
            ChanceOutcome(after_action.state, 0.6),
            ChanceOutcome(after_action.state + 1, 0.4),
        )


@dataclass
class StageCost:
    samples_ms: list[float] = field(default_factory=list)
    cpu_ns: int = 0
    traced_peak_bytes: int = 0
    traced_incremental_peak_bytes: int = 0
    peak_candidates: int = 0
    peak_input_candidates: int = 0
    peak_snapshot_bytes: int = 0
    peak_transition_branches: int = 0
    peak_unknown_transition_branches: int = 0
    residual_min: float = 1.0
    residual_max: float = 0.0
    final_residual: float = 1.0
    reasons: dict[str, int] = field(default_factory=dict)

    def measure(
        self,
        operation: Callable[[], BeliefEstimateResult[StateT]],
        overall_peak: list[int],
    ) -> BeliefSnapshot[StateT]:
        baseline, peak = tracemalloc.get_traced_memory()
        overall_peak[0] = max(overall_peak[0], peak)
        tracemalloc.reset_peak()
        cpu_started = time.process_time_ns()
        started = time.perf_counter_ns()
        result = operation()
        self.samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        self.cpu_ns += time.process_time_ns() - cpu_started
        _, peak = tracemalloc.get_traced_memory()
        overall_peak[0] = max(overall_peak[0], peak)
        self.traced_peak_bytes = max(self.traced_peak_bytes, peak)
        self.traced_incremental_peak_bytes = max(
            self.traced_incremental_peak_bytes, peak - baseline
        )
        if result.snapshot is None:
            raise RuntimeError(f"estimator stage failed: {result.reason}")
        snapshot = result.snapshot
        self.peak_candidates = max(self.peak_candidates, len(snapshot.candidates))
        self.peak_input_candidates = max(
            self.peak_input_candidates, snapshot.diagnostics.peak_candidates
        )
        self.peak_snapshot_bytes = max(
            self.peak_snapshot_bytes, len(snapshot.to_bytes())
        )
        self.peak_transition_branches = max(
            self.peak_transition_branches, result.transition_branches
        )
        self.peak_unknown_transition_branches = max(
            self.peak_unknown_transition_branches, result.unknown_transition_branches
        )
        self.residual_min = min(self.residual_min, snapshot.residual_unknown_mass)
        self.residual_max = max(self.residual_max, snapshot.residual_unknown_mass)
        self.final_residual = snapshot.residual_unknown_mass
        self.reasons[result.reason] = self.reasons.get(result.reason, 0) + 1
        return snapshot

    def report(self) -> dict:
        values = sorted(self.samples_ms)
        return {
            "samples": len(values),
            "p50_ms": statistics.median(values),
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
            "max_ms": values[-1],
            "cpu_total_ms": self.cpu_ns / 1_000_000,
            "traced_peak_bytes": self.traced_peak_bytes,
            "traced_incremental_peak_bytes": self.traced_incremental_peak_bytes,
            "peak_candidates": self.peak_candidates,
            "peak_input_candidates": self.peak_input_candidates,
            "peak_snapshot_bytes": self.peak_snapshot_bytes,
            "peak_transition_branches": self.peak_transition_branches,
            "peak_unknown_transition_branches": self.peak_unknown_transition_branches,
            "residual_unknown_mass": {
                "minimum": self.residual_min,
                "maximum": self.residual_max,
                "final": self.final_residual,
            },
            "result_reasons": self.reasons,
        }


def evidence(context: BeliefContext, observed: int) -> BeliefEvidenceRef:
    return BeliefEvidenceRef(
        "evidence." + context.snapshot_id,
        "correlation." + context.snapshot_id,
        BeliefEvidenceKind.OBSERVATION,
        "synthetic-recognizer",
        "v1",
        context,
        observed,
        context.frame_ids[-1],
    )


def benchmark_case(
    name: str,
    state_type: type[StateT],
    states: tuple[tuple[StateT, float], ...],
    model: TransitionModel[StateT],
    action_id: str,
    *,
    iterations: int,
    capacity: int,
    seed: int,
    store_policy: BeliefStorePolicy,
    estimator_policy: BeliefEstimatorPolicy,
) -> dict:
    stages = {name: StageCost() for name in ("initialize", "predict", "observe")}
    rng = random.Random(seed)
    overall_peak = [0]
    cpu_started = time.process_time_ns()
    tracemalloc.start()
    try:
        for iteration in range(iterations):
            observed = time.monotonic_ns()
            context = BeliefContext(
                name,
                "fixture",
                f"session{iteration}",
                f"snapshot{iteration}.0",
                (f"frame{iteration}.0",),
                0,
                0,
                1,
                "synthetic-capture",
                1,
                "benchmark.monotonic",
            )
            store = BeliefCandidateStore(
                state_type, clock_id=context.clock_id, policy=store_policy
            )
            estimator = BayesianBeliefEstimator(store, policy=estimator_policy)
            source = evidence(context, observed)
            candidates = tuple(
                WorldStateHypothesis(
                    f"hypothesis{index}",
                    TypedStateRef(name, "fixture-state-v1", state_type, state),
                    context,
                    BeliefWeight(
                        BeliefWeightKind.CALIBRATED_PROBABILITY,
                        weight,
                        "synthetic-initial-calibration-v1",
                    ),
                    HypothesisOrigin.OBSERVATION_HYPOTHESIS,
                    (source,),
                    BeliefFreshness(observed, observed + store_policy.max_age_ns),
                )
                for index, (state, weight) in enumerate(states)
            )
            unknown = max(0.0, 1.0 - math.fsum(weight for _, weight in states))
            prior = stages["initialize"].measure(
                lambda: estimator.initialize(context, candidates, unknown_mass=unknown),
                overall_peak,
            )
            receipt = BeliefActionRef(
                action_id,
                f"receipt{iteration}",
                context.snapshot_id,
                context.action_epoch,
                context,
            )
            prediction = stages["predict"].measure(
                lambda: estimator.predict(
                    prior,
                    receipt,
                    model,
                    model_id="fixture-transition",
                    model_version="v1",
                ),
                overall_peak,
            )
            current = time.monotonic_ns()
            next_context = replace(
                context,
                snapshot_id=f"snapshot{iteration}.1",
                frame_ids=(f"frame{iteration}.1",),
                frame_sequence=1,
                turn=1,
            )
            observation = evidence(next_context, current)
            favored = rng.randrange(max(1, len(prediction.candidates)))
            # Synthetic densities share one measure; >1 deliberately tests density/probability separation.
            likelihoods = tuple(
                (item.state_ref.digest, 2.0 if index == favored else 0.5)
                for index, item in enumerate(prediction.candidates)
            )
            batch = BeliefLikelihoodBatch(
                next_context,
                BeliefFreshness(current, current + store_policy.max_age_ns),
                (observation,),
                observation.correlation_id,
                likelihoods,
                "synthetic-density-v1",
                0.75,
            )
            posterior = stages["observe"].measure(
                lambda: estimator.observe(prediction, batch), overall_peak
            )
            for snapshot in (prior, prediction, posterior):
                if (
                    len(snapshot.candidates) > capacity
                    or len(snapshot.to_bytes()) > store_policy.max_snapshot_bytes
                ):
                    raise RuntimeError(
                        "benchmark snapshot exceeded configured capacity"
                    )
                mass = (
                    math.fsum(item.weight.value for item in snapshot.candidates)
                    + snapshot.residual_unknown_mass
                )
                if (
                    snapshot.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
                    and not math.isclose(mass, 1.0, abs_tol=1e-12)
                ):
                    raise RuntimeError(
                        "benchmark posterior probability mass is inconsistent"
                    )
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        overall_peak[0] = max(overall_peak[0], peak_bytes)
    finally:
        tracemalloc.stop()
    return {
        "iterations": iterations,
        "seed": seed,
        "initial_candidates": len(states),
        "capacity_k": capacity,
        "cpu_total_ms_including_setup_and_diagnostics": (
            time.process_time_ns() - cpu_started
        )
        / 1_000_000,
        "traced_current_bytes": current_bytes,
        "traced_peak_bytes": overall_peak[0],
        "stages": {name: stage.report() for name, stage in stages.items()},
    }


def source_fingerprints() -> dict[str, str]:
    return {
        name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_PATHS
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=32)
    parser.add_argument("--int-candidates", type=int, default=8)
    parser.add_argument("--seed", type=int, default=564)
    parser.add_argument(
        "--output", type=Path, default=Path("build/belief-estimator/report.json")
    )
    args = parser.parse_args(argv)
    if (
        not 2 <= args.iterations <= 1000
        or not 2 <= args.capacity <= 256
        or not 2 <= args.int_candidates <= 64
        or not 0 <= args.seed < 2**32
    ):
        parser.error(
            "iterations=2..1000, capacity=2..256, int-candidates=2..64, seed=0..2^32-1 required"
        )
    fixture_path = ROOT / "tests/fixtures/belief/ambiguous_2048.json"
    raw = fixture_path.read_bytes()
    if len(raw) > 65536:
        parser.error("ambiguous 2048 fixture exceeds byte budget")
    fixture = json.loads(raw)
    # Instrumented cost runs use an explicit 1s deadline, not the production 100ms default.
    store_policy = BeliefStorePolicy(
        max_candidates=args.capacity,
        max_input_candidates=max(args.capacity, args.int_candidates, 256),
        max_update_ns=1_000_000_000,
    )
    estimator_policy = BeliefEstimatorPolicy(max_update_ns=1_000_000_000)
    git_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True, timeout=10
        ).strip()
    )
    before = source_fingerprints()
    report = {
        "schema_version": 1,
        "run_id": uuid4().hex,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "git_sha": git_sha,
        "git_dirty": dirty,
        "source_sha256": before,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
        },
        "config": {
            "iterations": args.iterations,
            "capacity": args.capacity,
            "int_candidates": args.int_candidates,
            "seed": args.seed,
            "store_policy": asdict(store_policy),
            "estimator_policy": asdict(estimator_policy),
        },
        "fixture": {
            "path": fixture_path.relative_to(ROOT).as_posix(),
            "sha256": sha256(raw).hexdigest(),
            "schema_version": fixture["schema_version"],
        },
        "measurement_scope": {
            "stage_latency": "Estimator API call only; input preparation and report serialization excluded",
            "heap": "tracemalloc includes Python setup, candidates, all stages and diagnostics; per-stage incremental peak subtracts live baseline; not RSS",
            "lifecycle": "fresh Store/Estimator per iteration; does not measure long-session correlation-history endurance",
            "instrumented_deadline": "explicit 1s deadline; does not establish production 100ms latency acceptance",
            "likelihood": "synthetic densities 2.0/0.5 and unknown 0.75; no recognizer accuracy claim",
            "real_play": "not_run: no DOM, live game input, recognition or gameplay success measurement",
        },
        "cases": {},
    }
    exit_code = 0
    try:
        rng = random.Random(args.seed)
        offset = rng.randrange(10000)
        int_states = tuple(
            (offset + index, 0.9 / args.int_candidates)
            for index in range(args.int_candidates)
        )
        board_states = tuple(
            (Game2048State(item["cells"]), item["probability"])
            for item in fixture["candidates"]
        )
        report["cases"]["fake_int"] = benchmark_case(
            "fake-int",
            int,
            int_states,
            IntTransitionModel(),
            "increment",
            iterations=args.iterations,
            capacity=args.capacity,
            seed=args.seed,
            store_policy=store_policy,
            estimator_policy=estimator_policy,
        )
        report["cases"]["ambiguous_2048"] = benchmark_case(
            "2048",
            Game2048State,
            board_states,
            ReferenceGame2048TransitionModel(),
            "2048-move-right",
            iterations=args.iterations,
            capacity=args.capacity,
            seed=args.seed,
            store_policy=store_policy,
            estimator_policy=estimator_policy,
        )
        after = source_fingerprints()
        report["source_changed_during_run"] = before != after
        if before != after:
            raise RuntimeError(
                "benchmark source files changed during measurement; rerun after edits finish"
            )
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"type": type(error).__name__, "reason": str(error)[:300]}
        exit_code = 1
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    archived = publish_validation_report(args.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "archive": str(archived),
                "latest": str(args.output),
                "git_sha": git_sha,
                "cases": report["cases"],
                "failure": report.get("failure"),
            },
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
