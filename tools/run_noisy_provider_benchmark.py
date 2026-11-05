from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from ai_game_player.decision_context import DecisionContext
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.event_journal import EventJournal
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.noisy_provider import NoisyLanguageProvider


class TimedNoisyLanguageProvider:
    def __init__(self, provider: NoisyLanguageProvider) -> None:
        self.provider = provider
        self.selection_times_ns: list[int] = []
        self.outcome_times_ns: list[int] = []

    def choose_context(
        self, context: DecisionContext, personality: str = ""
    ) -> ActionDecision:
        start_ns = time.perf_counter_ns()
        try:
            return self.provider.choose_context(context, personality)
        finally:
            self.selection_times_ns.append(time.perf_counter_ns() - start_ns)

    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None = None,
    ):
        start_ns = time.perf_counter_ns()
        try:
            return self.provider.assess_outcome(observation, previous)
        finally:
            self.outcome_times_ns.append(time.perf_counter_ns() - start_ns)


class OutOfSetProvider:
    def choose_context(
        self, context: DecisionContext, personality: str = ""
    ) -> ActionDecision:
        return ActionDecision(
            "out-of-set-action",
            "amber lattice follows quiet comet",
            "out_of_set_fixture",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )


def percentile_ms(samples_ns: list[int], percentile: float) -> float:
    if not samples_ns:
        raise ValueError("benchmark produced no timing samples")
    sorted_samples = sorted(samples_ns)
    nearest_rank = max(1, math.ceil(percentile * len(sorted_samples)))
    return sorted_samples[nearest_rank - 1] / 1_000_000


def summarize_timings(samples_ns: list[int]) -> dict[str, float]:
    if not samples_ns:
        raise ValueError("benchmark produced no timing samples")
    return {
        "p50_ms": statistics.median(samples_ns) / 1_000_000,
        "p95_ms": percentile_ms(samples_ns, 0.95),
    }


def run_out_of_set_fixture() -> bool:
    observation = ScreenObservation(
        "negative-fixture", 100, 80, features={"signature": "negative-v1"}
    )
    candidate = ActionCandidate("allowed-wait", "wait", "Wait", confidence=0.9)
    with tempfile.TemporaryDirectory() as temporary_directory:
        engine = GamePlayerEngine(
            Path(temporary_directory), provider=OutOfSetProvider()
        )
        try:
            engine.step(observation, [candidate])
        except ValueError:
            return (
                engine.last_reliability_result is not None
                and engine.last_reliability_result.status.value == "REJECT"
            )
    return False


def measure_engine_steps(
    engine: Any,
    provider: TimedNoisyLanguageProvider,
    candidates: list[ActionCandidate],
    step_count: int,
    *,
    offset: int = 0,
) -> tuple[list[int], list[int]]:
    step_times_ns: list[int] = []
    non_provider_core_times_ns: list[int] = []
    allowed_action_ids = {
        candidate.action_id for candidate in candidates if not candidate.dangerous
    }
    for step_index in range(offset, offset + step_count):
        observation = ScreenObservation(
            "synthetic-board",
            320,
            240,
            features={"signature": f"synthetic-state-{step_index}"},
        )
        selection_sample_start = len(provider.selection_times_ns)
        outcome_sample_start = len(provider.outcome_times_ns)
        start_ns = time.perf_counter_ns()
        decision = engine.step(
            observation, candidates, purpose="advance synthetic board"
        )
        elapsed_ns = time.perf_counter_ns() - start_ns
        provider_elapsed_ns = sum(
            provider.selection_times_ns[selection_sample_start:]
        ) + sum(provider.outcome_times_ns[outcome_sample_start:])
        if provider_elapsed_ns > elapsed_ns:
            raise RuntimeError(
                "measured provider time exceeded its enclosing Engine step"
            )
        step_times_ns.append(elapsed_ns)
        non_provider_core_times_ns.append(elapsed_ns - provider_elapsed_ns)
        if decision.action_id not in allowed_action_ids:
            raise RuntimeError(
                f"provider selected an unsafe candidate: {decision.action_id}"
            )
        if (
            engine.last_reliability_result is None
            or engine.last_reliability_result.status.value == "REJECT"
        ):
            raise RuntimeError(
                "Core rejected a noisy provider action from its allowed candidates"
            )
    return step_times_ns, non_provider_core_times_ns


def write_report(output_path: Path, report: dict[str, object]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


DEFAULT_ROUNDS = 5
EXPECTED_BUDGET_NAMES = (
    "provider_selection_p95_ms",
    "provider_outcome_p95_ms",
    "core_step_total_p95_ms",
    "core_step_non_provider_p95_ms",
)
JUDGEMENT_METHOD = "median of per-round nearest-rank p95 across independent rounds"

EngineFactory = Callable[[TimedNoisyLanguageProvider], AbstractContextManager[Any]]


@contextmanager
def journaled_engine(
    provider: TimedNoisyLanguageProvider,
) -> Iterator[GamePlayerEngine]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        game_directory = Path(temporary_directory)
        with EventJournal(
            game_directory / "session.sqlite3", session_id="noisy-provider-benchmark"
        ) as journal:
            yield GamePlayerEngine(
                game_directory,
                provider=provider,
                event_journal=journal,
                migrate_legacy=False,
            )


def judge_round_p95s(round_p95s_ms: list[float]) -> float:
    """Return the judged value: the median of independent per-round p95 values.

    A transient runner stall reaches only a minority of rounds, while a real
    regression slows every round, so the median keeps regressions visible.
    """
    if len(round_p95s_ms) < 3 or len(round_p95s_ms) % 2 == 0:
        raise ValueError("budget judgement requires an odd number of at least 3 rounds")
    return float(statistics.median(round_p95s_ms))


def measure_round(
    seed: int,
    steps: int,
    warmup_steps: int,
    candidates: list[ActionCandidate],
    engine_factory: EngineFactory,
) -> dict[str, list[int]]:
    timed_provider = TimedNoisyLanguageProvider(NoisyLanguageProvider(seed))
    with engine_factory(timed_provider) as engine:
        measure_engine_steps(engine, timed_provider, candidates, warmup_steps)
        timed_provider.selection_times_ns.clear()
        timed_provider.outcome_times_ns.clear()
        engine_step_times_ns, core_without_provider_times_ns = measure_engine_steps(
            engine,
            timed_provider,
            candidates,
            steps,
            offset=warmup_steps,
        )
    engine_outcome_times_ns = list(timed_provider.outcome_times_ns)
    observation = ScreenObservation("synthetic-outcome", 320, 240)
    for _ in range(steps):
        timed_provider.assess_outcome(observation)
    return {
        "provider_selection_p95_ms": timed_provider.selection_times_ns,
        "provider_outcome_p95_ms": timed_provider.outcome_times_ns,
        "core_step_total_p95_ms": engine_step_times_ns,
        "core_step_non_provider_p95_ms": core_without_provider_times_ns,
        "engine_outcome_times_ns": engine_outcome_times_ns,
    }


def run_benchmark(
    steps: int,
    warmup_steps: int,
    seed: int,
    budget_multiplier: float,
    budgets_path: Path,
    output_path: Path,
    rounds: int = DEFAULT_ROUNDS,
    engine_factory: EngineFactory = journaled_engine,
) -> bool:
    if steps < 100:
        raise ValueError("the synthetic benchmark requires at least 100 measured steps")
    if warmup_steps < 0:
        raise ValueError("warmup steps must not be negative")
    if budget_multiplier <= 0:
        raise ValueError("budget multiplier must be positive")
    if rounds < 3 or rounds % 2 == 0:
        raise ValueError("rounds must be an odd number of at least 3")

    raw_budgets = json.loads(budgets_path.read_text(encoding="utf-8"))
    if not isinstance(raw_budgets, dict) or not raw_budgets:
        raise ValueError(
            "noisy provider performance budgets must contain a non-empty object"
        )
    budgets = {
        str(name): float(value) * budget_multiplier
        for name, value in raw_budgets.items()
    }
    if set(budgets) != set(EXPECTED_BUDGET_NAMES) or any(
        value <= 0 for value in budgets.values()
    ):
        raise ValueError(
            "noisy provider budgets must define positive limits for provider, total Core, and non-provider Core timing"
        )

    candidates = [
        ActionCandidate(f"slot-{index}", "wait", f"slot {index}", confidence=0.9)
        for index in range(4)
    ]
    candidates.append(
        ActionCandidate(
            "dangerous-slot", "wait", "dangerous slot", confidence=0.9, dangerous=True
        )
    )
    round_samples = [
        measure_round(seed, steps, warmup_steps, candidates, engine_factory)
        for _ in range(rounds)
    ]

    performance: dict[str, dict[str, Any]] = {}
    for name in EXPECTED_BUDGET_NAMES:
        round_p95s = [percentile_ms(samples[name], 0.95) for samples in round_samples]
        pooled = [sample for samples in round_samples for sample in samples[name]]
        judged_p95 = judge_round_p95s(round_p95s)
        performance[name] = {
            "p50_ms": summarize_timings(pooled)["p50_ms"],
            "p95_ms": judged_p95,
            "round_p95_ms": round_p95s,
            "worst_round_p95_ms": max(round_p95s),
            "pooled_p95_ms": percentile_ms(pooled, 0.95),
            "p95_budget_ms": budgets[name],
            "passed": judged_p95 <= budgets[name],
        }
    out_of_set_fixture_rejected = run_out_of_set_fixture()
    report: dict[str, object] = {
        "schema": "noisy-provider-benchmark/v3",
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "core_persistence": "sqlite_session_journal",
        "seed": seed,
        "rounds": rounds,
        "warmup_steps": warmup_steps,
        "measured_steps": steps,
        "total_measured_steps": steps * rounds,
        "allowed_candidate_count": sum(
            not candidate.dangerous for candidate in candidates
        ),
        "dangerous_candidate_excluded_by_core": True,
        "selection_count": sum(
            len(samples["provider_selection_p95_ms"]) for samples in round_samples
        ),
        "outcome_assessment_count": sum(
            len(samples["provider_outcome_p95_ms"]) for samples in round_samples
        ),
        "engine_outcome_assessment_count": sum(
            len(samples["engine_outcome_times_ns"]) for samples in round_samples
        ),
        "direct_outcome_assessment_count": steps * rounds,
        "outcome_timing_sources": ["engine_semantic_fallback", "direct_provider_call"],
        "non_provider_core_timing_method": "engine step duration minus provider calls measured within that step",
        "out_of_set_fixture_rejected": out_of_set_fixture_rejected,
        "percentile_method": "p50=median of pooled samples; p95=nearest-rank per round",
        "judgement_method": JUDGEMENT_METHOD,
        "measurements": performance,
        "passed": all(result["passed"] for result in performance.values())
        and out_of_set_fixture_rejected,
    }
    write_report(output_path, report)
    for name, result in performance.items():
        status = "PASS" if result["passed"] else "FAIL"
        rounds_text = ", ".join(f"{value:.3f}" for value in result["round_p95_ms"])
        print(
            f"{status} {name}: median round p95={result['p95_ms']:.6f}ms "
            f"budget={result['p95_budget_ms']:.6f}ms rounds=[{rounds_text}]"
        )
    print(
        f"{'PASS' if out_of_set_fixture_rejected else 'FAIL'} out-of-set negative fixture"
    )
    print(f"Report: {output_path}")
    return bool(report["passed"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise and time the deterministic noisy-provider Core fixture"
    )
    parser.add_argument("--steps", type=int, default=128)
    parser.add_argument("--warmup-steps", type=int, default=8)
    parser.add_argument("--seed", type=int, default=178)
    parser.add_argument(
        "--budgets",
        type=Path,
        default=Path("config/noisy_provider_performance_budgets.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("build/noisy-provider/report.json")
    )
    parser.add_argument("--budget-multiplier", type=float, default=1.0)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    args = parser.parse_args()
    try:
        passed = run_benchmark(
            args.steps,
            args.warmup_steps,
            args.seed,
            args.budget_multiplier,
            args.budgets,
            args.output,
            rounds=args.rounds,
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"NOISY PROVIDER BENCHMARK ERROR: {error}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
