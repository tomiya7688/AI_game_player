"""Seeded headless fixture comparison. This does not satisfy Windows field-play gates."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import random
import statistics
import subprocess
import tempfile
import time

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.game_2048_transition import (
    ReferenceGame2048TransitionModel,
    DIRECTIONS,
)
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.planner_factory import reference_2048_planner_provider
from ai_game_player.planning_contract import SearchBudget
from ai_game_player.run_control import RunController
from tools.browser_probe_support import new_probe_directory, publish_validation_report
from tools.run_2048_campaign import CyclingDirectionProvider


class HeadlessSource:
    def __init__(self, seed):
        self.model = ReferenceGame2048TransitionModel()
        self.state = self.model.restart(seed)
        self.rng = random.Random(seed)
        self.sequence = 0
        self.source_id = f"headless.{seed}"
        self.invalid_actions = 0
        self.reobserve_count = 0

    def read(self):
        self.sequence += 1
        state = asdict(self.state)
        signature = sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        observation = ScreenObservation(
            "headless_fixture",
            500,
            500,
            features={
                "signature": signature,
                "capture_metadata": {
                    "source_id": self.source_id,
                    "frame_id": f"{self.source_id}.frame{self.sequence}",
                    "source_generation": 0,
                    "monotonic_ns": time.monotonic_ns(),
                },
                "game_2048": {
                    "board": [list(row) for row in self.state.cells],
                    "score": self.state.score,
                    "confidence": 1.0,
                    "game_over": self.state.over,
                    "state_signature": signature,
                },
            },
        )
        return observation, [
            ActionCandidate("2048-move-" + d.lower(), "key", d, confidence=1)
            for d in DIRECTIONS
        ]

    def read_fresh(self):
        self.reobserve_count += 1
        return self.read()


class HeadlessExecutor:
    def __init__(self, source):
        self.source = source
        self.input_dispatch_started = False

    def execute(self, candidate):
        self.input_dispatch_started = True
        transition = self.source.model.apply_action(
            self.source.state, candidate.action_id
        )
        if transition.changed:
            self.source.state = self.source.model.sample_spawn(
                transition, self.source.rng.getrandbits(32)
            )
        else:
            self.source.invalid_actions += 1
        return ExecutionResult(
            candidate.action_id,
            True,
            "headless_fixture",
            "fixture model input; no OS input",
        )


def _resources():
    try:
        import resource

        parent = resource.getrusage(resource.RUSAGE_SELF)
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {
            "parent_cpu_seconds": parent.ru_utime + parent.ru_stime,
            "worker_cpu_seconds": child.ru_utime + child.ru_stime,
            "parent_peak_rss_kib": parent.ru_maxrss,
            "worker_peak_rss_kib": child.ru_maxrss,
        }
    except ImportError:
        return None


def _quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def run_episode(provider_name, seed, max_steps, *, depth=1, wall_time_ms=400):
    source, controller = HeadlessSource(seed), RunController()
    budget = SearchBudget(
        max_depth=depth,
        max_nodes=10000,
        wall_time_ms=wall_time_ms,
        allow_conditional_selection=True,
    )
    provider = (
        reference_2048_planner_provider(
            controller=controller, allow_conditional=True, search_budget=budget
        )
        if provider_name == "planner"
        else BaselineDecisionProvider(min_margin=0, min_recognition=0.65)
        if provider_name == "baseline"
        else CyclingDirectionProvider()
    )
    start, resources_before = time.monotonic(), _resources()
    samples, plans = [], []
    withheld = None
    with tempfile.TemporaryDirectory(prefix="kadoka-planning-comparison-") as directory:
        pipeline = DecisionPipeline(
            source, Path(directory), provider, controller=controller, dry_run=True
        )
        pipeline.executor.live_executor = HeadlessExecutor(source)
        pipeline.executor.dry_run = False
        orchestrator = DecisionOrchestrator(pipeline)
        try:
            for _ in range(max_steps):
                if source.state.terminated:
                    break
                began = time.monotonic_ns()
                result = orchestrator.run(
                    execute=True, purpose="headless planning comparison"
                )
                elapsed = (time.monotonic_ns() - began) / 1e6
                if isinstance(result, DecisionDisposition):
                    withheld = result.to_dict()
                    break
                samples.append(elapsed)
                plan = getattr(provider, "last_planning_result", None)
                policy = getattr(provider, "last_policy_decision", None)
                if plan is not None:
                    plans.append(
                        {
                            "completed_depth": plan.completed_depth,
                            "reason": plan.reason,
                            "continuation_semantics": getattr(
                                plan, "continuation_semantics", "one_step"
                            ),
                            "counters": asdict(plan.counters),
                            "probability_semantics": plan.probability_semantics,
                            "global_risk_known": policy.global_risk_known
                            if policy is not None
                            else None,
                        }
                    )
            observation, _ = source.read()
            pipeline.complete_execution_observation(observation)
            controller.stop()
            try:
                pipeline.run_and_execute()
            except RuntimeError:
                stop_rejected = True
            else:
                stop_rejected = False
            receipts = [
                record.get("execution_receipt")
                for record in pipeline.engine.trace.recent(max_steps * 4 + 20)
                if record.get("execution_receipt")
            ]
        finally:
            pipeline.close()
    resources_after = _resources()
    resource_delta = None
    if resources_before is not None and resources_after is not None:
        resource_delta = dict(resources_after)
        for key in ("parent_cpu_seconds", "worker_cpu_seconds"):
            resource_delta[key] -= resources_before[key]
    return {
        "provider": provider_name,
        "seed": seed,
        "max_steps": max_steps,
        "actions": len(samples),
        "score": source.state.score,
        "max_tile": max(map(max, source.state.cells)),
        "terminal": source.state.terminated,
        "survival_steps": len(samples),
        "wall_seconds": time.monotonic() - start,
        "invalid_actions": source.invalid_actions,
        "invalid_action_rate": source.invalid_actions / max(1, len(samples)),
        "extra_capture_count": source.reobserve_count,
        "extra_capture_per_action": source.reobserve_count / max(1, len(samples)),
        "withheld": withheld,
        "latency_ms": {
            "p50": statistics.median(samples) if samples else None,
            "p95": _quantile(samples, 0.95),
            "samples": samples,
        },
        "resources": resource_delta,
        "resource_semantics": "Linux ru_maxrss is process-lifetime high-water KiB, not per-episode delta; CPU includes waited workers",
        "nodes": sum(p["counters"]["nodes_visited"] for p in plans),
        "plans": plans,
        "receipt_count": len(receipts),
        "stopped_input_rejected": stop_rejected,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=("cycling", "baseline", "planner"),
        default=["cycling", "baseline", "planner"],
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=[479, 480, 481])
    parser.add_argument("--max-steps", type=int, default=180)
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--wall-time-ms", type=int, default=400)
    parser.add_argument(
        "--output", type=Path, default=Path("build/planning-comparison/headless")
    )
    args = parser.parse_args(argv)
    if (
        not 1 <= len(args.seeds) <= 10
        or not 1 <= args.max_steps <= 500
        or not 1 <= args.depth <= 3
        or not 1 <= args.wall_time_ms <= 2000
    ):
        parser.error("comparison requires bounded seeds/steps/depth/time")
    output = new_probe_directory(args.output)
    report = {
        "schema_version": 1,
        "mode": "headless_fixture",
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "rng": "same Python Random(seed), accepted move sample_spawn from getrandbits32; separate from Chromium LCG test RNG",
        "decision_state": "public model board/score fixture only; no browser/OS or hidden RNG supplied to provider",
        "quality_claim": "conditional single-world experiment; global world risk unknown, no calibration or statistical significance claim",
        "windows_actual": "Unverified; existing Windows600s gate remains required",
        "baseline_profile": {"min_margin": 0, "min_recognition": 0.65},
        "planning_budget": {
            "max_depth": args.depth,
            "max_nodes": 10000,
            "wall_time_ms": args.wall_time_ms,
        },
        "episodes": [
            run_episode(
                p, s, args.max_steps, depth=args.depth, wall_time_ms=args.wall_time_ms
            )
            for s in args.seeds
            for p in args.providers
        ],
    }
    path = output / "report.json"
    publish_validation_report(path, report)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
