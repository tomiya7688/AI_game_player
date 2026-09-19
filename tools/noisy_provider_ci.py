from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from pathlib import Path
from time import perf_counter_ns

from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.testing.noisy_language_provider import NoisyLanguageProvider


ROOT = Path(__file__).resolve().parents[1]


class SyntheticNoisySource:
    def __init__(self, candidates_per_step: int) -> None:
        if candidates_per_step < 1:
            raise ValueError("candidates_per_step must be positive")
        self.candidates_per_step = candidates_per_step
        self.step = 0

    def read(self):
        self.step += 1
        phase = self.step % 11
        observation = ScreenObservation(
            "noisy-ci-screen",
            640,
            480,
            [f"phase {phase}", "nonsense environment"],
            {
                "signature": f"noisy-{phase}",
                "mean_brightness": float(80 + phase),
            },
        )
        candidates = [
            ActionCandidate(
                f"wait-{index}",
                "wait",
                f"candidate {index}",
                confidence=1.0,
            )
            for index in range(self.candidates_per_step)
        ]
        return observation, candidates


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(statistics.fmean(values), 6) if values else 0.0,
        "p50_ms": round(percentile(values, 0.50), 6),
        "p95_ms": round(percentile(values, 0.95), 6),
        "max_ms": round(max(values), 6) if values else 0.0,
    }


def run_noisy_ci(config: dict[str, object], budget_multiplier: float) -> dict[str, object]:
    if budget_multiplier <= 0:
        raise ValueError("budget_multiplier must be positive")

    warmup_steps = int(config["warmup_steps"])
    measured_steps = int(config["measured_steps"])
    candidates_per_step = int(config["candidates_per_step"])
    seed = int(config["seed"])
    budgets = dict(config["budgets"])

    provider = NoisyLanguageProvider(seed=seed)
    source = SyntheticNoisySource(candidates_per_step)

    total_ms: list[float] = []
    provider_ms: list[float] = []
    core_ms: list[float] = []
    decisions: list[str] = []
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="kadoka-noisy-ci-") as directory:
        pipeline = DecisionPipeline(
            source,
            Path(directory),
            provider=provider,
            dry_run=True,
            external_watchdog=False,
        )
        try:
            for step in range(warmup_steps + measured_steps):
                provider.consume_elapsed_ms()
                started = perf_counter_ns()
                try:
                    result = pipeline.run_and_execute(purpose="exercise Kadoka without relying on model quality")
                except Exception as exc:
                    errors.append(f"step={step}: {type(exc).__name__}: {exc}")
                    break
                elapsed = (perf_counter_ns() - started) / 1_000_000.0
                provider_elapsed = provider.consume_elapsed_ms()

                if not result.action_id.startswith("wait-"):
                    errors.append(f"step={step}: ungrounded action {result.action_id}")
                    break
                if result.executed or result.mode != "dry_run":
                    errors.append(f"step={step}: noisy CI unexpectedly executed OS input")
                    break

                decisions.append(result.action_id)
                if step >= warmup_steps:
                    total_ms.append(elapsed)
                    provider_ms.append(provider_elapsed)
                    core_ms.append(max(0.0, elapsed - provider_elapsed))
        finally:
            pipeline.close()

    provider_budget = float(budgets["provider_p95_ms"]) * budget_multiplier
    core_budget = float(budgets["non_provider_core_p95_ms"]) * budget_multiplier
    provider_summary = summarize(provider_ms)
    core_summary = summarize(core_ms)
    total_summary = summarize(total_ms)

    report: dict[str, object] = {
        "schema": "kadoka.noisy-provider-ci-result/1",
        "seed": seed,
        "warmup_steps": warmup_steps,
        "requested_measured_steps": measured_steps,
        "completed_measured_steps": len(total_ms),
        "decision_calls": provider.decision_calls,
        "outcome_calls": provider.outcome_calls,
        "unique_actions_selected": len(set(decisions)),
        "errors": errors,
        "timing": {
            "total_step": total_summary,
            "provider": provider_summary,
            "non_provider_core": core_summary,
        },
        "budgets": {
            "provider_p95_ms": provider_budget,
            "non_provider_core_p95_ms": core_budget,
        },
    }
    report["passed"] = bool(
        not errors
        and len(total_ms) == measured_steps
        and provider_summary["p95_ms"] <= provider_budget
        and core_summary["p95_ms"] <= core_budget
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic bad-AI robustness and non-LLM core performance CI."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "noisy_provider_ci.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "performance" / "noisy_provider_report.json",
    )
    parser.add_argument("--budget-multiplier", type=float, default=1.0)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = run_noisy_ci(config, args.budget_multiplier)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    timing = report["timing"]
    print(
        "NOISY CI "
        f"steps={report['completed_measured_steps']} "
        f"provider_p95={timing['provider']['p95_ms']:.3f}ms "
        f"core_p95={timing['non_provider_core']['p95_ms']:.3f}ms "
        f"total_p95={timing['total_step']['p95_ms']:.3f}ms "
        f"actions={report['unique_actions_selected']}"
    )
    if report["passed"]:
        print("NOISY PROVIDER CI OK")
        return 0

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
