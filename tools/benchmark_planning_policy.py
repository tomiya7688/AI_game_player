"""有限synthetic world/channelによるRisk/EVSI PolicyのCPU/RAM診断。実プレイ性能の証拠ではない。"""

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import platform
import random
import statistics
import subprocess
import time
import tracemalloc

from ai_game_player.planning_information import PlanningObservationModel
from ai_game_player.planning_policy_contract import PlanningRiskPolicyConfig
from ai_game_player.planning_reobservation import PlanningDecisionEpisode
from ai_game_player.planning_result import WeightedValue
from ai_game_player.planning_risk_policy import PlanningRiskPolicy
from tools.browser_probe_support import publish_validation_report
from tools.planning_policy_fixture import make_belief, make_plan


ROOT = Path(__file__).resolve().parents[1]
SOURCES = tuple(sorted((ROOT / "src/ai_game_player").glob("planning_*.py"))) + (
    Path(__file__),
    ROOT / "tools/planning_policy_fixture.py",
)


def scenario(world_count, action_count, seed):
    rng = random.Random(seed)
    belief = make_belief(tuple(1 / world_count for _ in range(world_count)))
    distributions = {}
    for action in range(action_count):
        distributions[f"action{action}"] = tuple(
            (
                WeightedValue(
                    0.9
                    if index % action_count == action
                    else 0.1 + rng.random() * 0.01,
                    0.99,
                ),
                WeightedValue(0.05, 0.01),
            )
            for index in range(world_count)
        )
    result = make_plan(belief, distributions)
    signal_count = min(world_count, 16)
    model = PlanningObservationModel(
        belief.context,
        belief.hypothesis_set_version,
        tuple(item.hypothesis_id for item in belief.candidates),
        tuple(
            tuple(
                1.0 if world % signal_count == signal else 0.0
                for world in range(world_count)
            )
            for signal in range(signal_count)
        ),
        "synthetic.sensor",
        "v1",
        "synthetic.calibration",
        0.01,
        "evaluator",
        "v1",
    )
    return belief, result, model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--seed", type=int, default=566)
    parser.add_argument(
        "--output", type=Path, default=Path("build/planning-policy/report.json")
    )
    args = parser.parse_args(argv)
    if not 10 <= args.iterations <= 1000:
        parser.error("iterations must be 10..1000")
    reports = []
    for world_count, action_count in ((2, 2), (16, 8), (64, 16)):
        belief, plan, model = scenario(world_count, action_count, args.seed)
        settings = PlanningRiskPolicyConfig(max_processing_ns=1_000_000_000)

        def evaluate():
            # Each sample is an independent Goal episode; runtime must retain its episode across frames.
            episode = PlanningDecisionEpisode(belief.context, "benchmark.goal", 1000)
            clock_origin = time.monotonic_ns()
            policy = PlanningRiskPolicy(
                episode,
                config=settings,
                clock_ns=lambda: 1000 + time.monotonic_ns() - clock_origin,
            )
            return policy.decide(
                plan,
                belief,
                context=belief.context,
                goal_id="benchmark.goal",
                allowed_action_ids=plan.allowed_action_ids,
                run_generation=3,
                deadline_ns=10**9,
                observation_model=model,
            )

        for _ in range(5):
            evaluate()
        samples = []
        dispositions = Counter()
        cpu_started = time.process_time_ns()
        for _ in range(args.iterations):
            started = time.perf_counter_ns()
            decision = evaluate()
            samples.append((time.perf_counter_ns() - started) / 1e6)
            dispositions[decision.outcome.value] += 1
        cpu_ms = (time.process_time_ns() - cpu_started) / 1e6
        tracemalloc.start()
        decision = evaluate()
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        samples.sort()
        reports.append(
            {
                "world_count": world_count,
                "action_count": action_count,
                "absolute_leaf_atoms": sum(
                    len(item.outcomes) for item in plan.action_values
                ),
                "signal_count": len(model.signal_likelihoods),
                "iterations": args.iterations,
                "p50_ms": statistics.median(samples),
                "p95_ms": samples[
                    min(len(samples) - 1, (95 * len(samples) + 99) // 100 - 1)
                ],
                "total_cpu_ms": cpu_ms,
                "traced_retained_bytes": retained,
                "traced_peak_bytes": peak,
                "dispositions": dict(dispositions),
                "instrumented_decision": asdict(decision),
                "settings": asdict(settings),
            }
        )
    report = {
        "schema": "planning.policy-benchmark/v1",
        "seed": args.seed,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "source_sha256": {
            str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
            for path in SOURCES
        },
        "fixture_semantics": "synthetic calibrated worlds and observation channel; independent episode per diagnostic sample",
        "latency_includes": "policy/episode construction and decide; immutable fixture construction excluded; tracemalloc measured separately",
        "os_input": False,
        "reports": reports,
    }
    publish_validation_report(args.output, report)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "reports": [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in ("instrumented_decision", "settings")
                    }
                    for row in reports
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
