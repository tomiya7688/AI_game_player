"""汎用int/曖昧2048の制限process ExpectimaxをCPUだけで計測する。"""

import argparse
from collections import Counter
from dataclasses import asdict
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
import time
import tracemalloc
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
from ai_game_player.bounded_planner import BoundedExpectimaxPlanner
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.planning_2048 import reference_2048_registration
from ai_game_player.planning_contract import (
    PlanningDomain,
    PlanningRegistration,
    SearchBudget,
)
from ai_game_player.transition_model import ActionTransition, ChanceOutcome
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)
from tools.browser_probe_support import publish_validation_report


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = tuple(
    f"src/ai_game_player/{name}.py"
    for name in (
        "bounded_planner",
        "expectimax_search",
        "planning_contract",
        "planning_result",
        "planning_wire",
        "planning_worker",
        "planning_worker_limits",
        "planning_2048",
        "evaluation_quality",
    )
) + ("tools/benchmark_expectimax.py",)


# {
#   責務: [IntModel: 計測用int状態の純粋な遷移とchanceモデルを提供する]
#   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
# }
class IntModel:
    # {
    #   責務: [apply_action: 計測用の加算・減算抽象操作だけを適用する]
    #   引数: [state, action_id] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def apply_action(self, state, action_id):
        if action_id not in ("plus", "minus"):
            raise ValueError("unsupported int action")
        return ActionTransition(state + (1 if action_id == "plus" else -1), True, 0)

    # {
    #   責務: [chance_outcomes: 再現可能な二分岐の校正確率を返す]
    #   引数: [transition] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def chance_outcomes(self, transition):
        return (
            ChanceOutcome(transition.state - 1, 0.2),
            ChanceOutcome(transition.state + 1, 0.8),
        )


# {
#   責務: [IntQuality: ゲーム攻略成績と独立した有界int qualityを定義する]
#   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
# }
class IntQuality:
    # {
    #   責務: [evaluate: int値をtanhで正規化し式の既知性だけを支持率とする]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def evaluate(self, state):
        return NumericQuality(
            value=math.tanh(state / 10), confidence=1.0, available=True
        )


# {
#   責務: [int_domain: trustedな計測専用codec・model・legal・qualityを組み合わせる]
#   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def int_domain():
    # {
    #   責務: [decode: 有界canonical JSONから既知int型だけを復号する]
    #   引数: [raw] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def decode(raw):
        value = json.loads(raw)
        if set(value) != {"int"}:
            raise ValueError("int codec contract mismatch")
        return int(value["int"])

    return PlanningDomain(
        int, decode, IntModel(), lambda _state: ("plus", "minus"), IntQuality()
    )


# {
#   責務: [_child_usage: 対応OSでchild CPU累積値とRSS high-waterを読む]
#   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def _child_usage():
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (
            usage.ru_utime + usage.ru_stime,
            usage.ru_maxrss * (1024 if platform.system() == "Linux" else 1),
        )
    except (ImportError, AttributeError):
        return None


# {
#   責務: [_summarize: 有限測定列の中央値・最近順位p95・最大値を算出する]
#   引数: [values] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def _summarize(values):
    values = sorted(values)
    return {
        "p50": statistics.median(values),
        "p95": values[math.ceil(len(values) * 0.95) - 1],
        "maximum": values[-1],
    }


# {
#   責務: [measure_case: cold workerを含む反復測定と予算・質量・heap量を集計する]
#   引数: [name, registration, states, allowed, budget, iterations, warmup] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def measure_case(name, registration, states, allowed, budget, iterations, warmup):
    timings, worker_timings = [], []
    outcomes, reasons, depths, enforcement = Counter(), Counter(), Counter(), Counter()
    continuations = Counter()
    peak_nodes = peak_cache = peak_trace = peak_bytes = peak_branches = 0
    unknown_min, unknown_max = 1.0, 0.0
    cpu_started = time.process_time_ns()
    child_before = _child_usage()
    tracemalloc.start()
    try:
        for index in range(warmup + iterations):
            now = time.monotonic_ns()
            context = BeliefContext(
                registration.game_id,
                "fixture",
                f"session{index}",
                f"snapshot{index}",
                (f"frame{index}",),
                index,
                1,
                1,
                "synthetic",
                1,
                "expectimax-benchmark",
            )
            evidence = BeliefEvidenceRef(
                f"evidence{index}",
                f"frame{index}",
                BeliefEvidenceKind.OBSERVATION,
                "fixture",
                "v1",
                context,
                now,
                context.frame_ids[0],
            )
            hypotheses = tuple(
                WorldStateHypothesis(
                    f"h{offset}",
                    TypedStateRef(
                        registration.game_id,
                        registration.state_schema_id,
                        type(state),
                        state,
                    ),
                    context,
                    BeliefWeight(
                        BeliefWeightKind.CALIBRATED_PROBABILITY,
                        probability,
                        "synthetic-fixture-calibration.v1",
                    ),
                    HypothesisOrigin.OBSERVATION_HYPOTHESIS,
                    (evidence,),
                    BeliefFreshness(now, now + 5_000_000_000),
                )
                for offset, (state, probability) in enumerate(states)
            )
            store = BeliefCandidateStore(
                type(states[0][0]),
                clock_id=context.clock_id,
                policy=BeliefStorePolicy(max_age_ns=5_000_000_000),
            )
            belief = store.publish(context, hypotheses)
            planner = BoundedExpectimaxPlanner(
                (registration,), clock_id=context.clock_id
            )
            started = time.perf_counter_ns()
            result = planner.plan(
                belief,
                allowed,
                registration_id=registration.registration_id,
                budget=budget,
                current_belief=lambda: store.read(context),
            )
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if index < warmup:
                continue
            timings.append(elapsed)
            worker_timings.append(result.counters.worker_elapsed_ns / 1_000_000)
            outcomes[result.outcome.value] += 1
            reasons[result.reason] += 1
            depths[str(result.completed_depth)] += 1
            enforcement[result.limit_enforcement] += 1
            continuations[result.continuation_semantics] += 1
            peak_nodes = max(peak_nodes, result.counters.nodes_visited)
            peak_branches = max(peak_branches, result.counters.chance_branches)
            peak_cache = max(peak_cache, result.counters.peak_cache_entries)
            peak_trace = max(
                [peak_trace] + [len(action.outcomes) for action in result.action_values]
            )
            for action in result.action_values:
                unknown_min = min(unknown_min, action.trace_unknown_mass)
                unknown_max = max(unknown_max, action.trace_unknown_mass)
            from ai_game_player.planning_wire import result_to_dict

            peak_bytes = max(
                peak_bytes, len(json.dumps(result_to_dict(result), indent=2).encode())
            )
            if (
                result.counters.nodes_visited > budget.max_nodes
                or result.completed_depth > budget.max_depth
                or result.counters.peak_cache_entries > budget.max_cache_entries
            ):
                raise RuntimeError("planner counter exceeds configured budget")
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    child_after = _child_usage()
    return {
        "case": name,
        "registration": asdict(registration),
        "continuation_semantics": dict(continuations),
        "iterations": iterations,
        "warmup_iterations": warmup,
        "host_cold_spawn_plan_ms": _summarize(timings),
        "adopted_checkpoint_worker_elapsed_ms": _summarize(worker_timings),
        "parent_cpu_ms_including_setup_and_warmup": (
            time.process_time_ns() - cpu_started
        )
        / 1_000_000,
        "child_cpu_ms_including_setup_and_warmup": (child_after[0] - child_before[0])
        * 1000
        if child_after and child_before
        else None,
        "child_rss_high_water_bytes_cumulative": child_after[1]
        if child_after
        else None,
        "parent_traced_current_bytes": current,
        "parent_traced_peak_bytes": peak,
        "outcomes": dict(outcomes),
        "reasons": dict(reasons),
        "completed_depths": dict(depths),
        "limit_enforcement": dict(enforcement),
        "peak_reported_nodes": peak_nodes,
        "peak_reported_chance_branches": peak_branches,
        "peak_reported_cache_entries": peak_cache,
        "peak_trace_values": peak_trace,
        "peak_result_bytes": peak_bytes,
        "trace_unknown_mass_range": [unknown_min, unknown_max],
    }


# {
#   責務: [fingerprints: 再現reportへ記録する関連sourceのSHA256を計算する]
#   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def fingerprints():
    return {
        name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES
    }


# {
#   責務: [main: 設定・seed・環境・source版を記録して固有かつ原子的reportを公開する]
#   引数: [argv] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--seed", type=int, default=565)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--nodes", type=int, default=5000)
    parser.add_argument("--wall-ms", type=int, default=250)
    parser.add_argument(
        "--output", type=Path, default=Path("build/expectimax/report.json")
    )
    arguments = parser.parse_args(argv)
    if (
        not 2 <= arguments.iterations <= 1000
        or not 0 <= arguments.warmup <= 20
        or not 0 <= arguments.seed < 2**32
    ):
        parser.error("iterations=2..1000, warmup=0..20, seed=0..2^32-1 required")
    budget = SearchBudget(
        max_depth=arguments.depth,
        max_nodes=arguments.nodes,
        wall_time_ms=arguments.wall_ms,
    )
    before = fingerprints()
    fixture_path = ROOT / "tests/fixtures/belief/ambiguous_2048.json"
    fixture = json.loads(fixture_path.read_text())
    report = {
        "schema_version": 1,
        "run_id": uuid4().hex,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True, timeout=10
            ).strip()
        ),
        "source_sha256": before,
        "fixture_sha256": sha256(fixture_path.read_bytes()).hexdigest(),
        "seed": arguments.seed,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "budget": asdict(budget),
        "measurement_scope": {
            "latency": "Each plan starts a fresh constrained process; includes cold process/import/JSON/cleanup, OS file cache warmed by warmup",
            "worker_timing": "adopted completed checkpoint only; killed partial deeper work is not fully reported",
            "parent_heap": "tracemalloc of host only, includes fixture setup and warmup; excludes worker heap",
            "child_rss": "getrusage cumulative child high-water since this benchmark process started; unavailable platforms return null",
            "memory_limit": "worker Linux address space / Windows private commit; not a total application RSS guarantee",
            "world_probabilities": "synthetic fixture calibration only; no pixel recognizer probability calibration claim",
            "real_play": "not_run: pure int and virtual 2048, no DOM/OS input, no gameplay strength acceptance",
        },
        "cases": [],
    }
    exit_code = 0
    try:
        offset = random.Random(arguments.seed).randrange(-3, 4)
        registration = PlanningRegistration(
            "int_benchmark.v1",
            "tools.benchmark_expectimax",
            "int_domain",
            "fake-int",
            "int-state.v1",
            "int_model",
            "1",
            "int_tanh",
            "1",
        )
        report["cases"].append(
            measure_case(
                "fake_int",
                registration,
                ((offset, 0.55), (offset + 1, 0.35)),
                ("plus", "minus"),
                budget,
                arguments.iterations,
                arguments.warmup,
            )
        )
        registration = reference_2048_registration()
        states = tuple(
            (Game2048State(item["cells"]), item["probability"])
            for item in fixture["candidates"]
        )
        report["cases"].append(
            measure_case(
                "ambiguous_2048",
                registration,
                states,
                ("2048-move-up", "2048-move-right", "2048-move-down", "2048-move-left"),
                budget,
                arguments.iterations,
                arguments.warmup,
            )
        )
        report["source_changed_during_run"] = before != fingerprints()
        if report["source_changed_during_run"]:
            raise RuntimeError("planning source changed during measurement")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"type": type(error).__name__, "reason": str(error)[:300]}
        exit_code = 1
    archived = publish_validation_report(arguments.output, report)
    print(
        json.dumps(
            {
                "archive": str(archived),
                "latest": str(arguments.output),
                "report": report,
            },
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
