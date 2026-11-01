"""6軸Adapterのbootstrap/actual/hypotheticalと実Session追加費用を分けて測る。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from typing import Any

from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.evaluation_prediction import (
    AxisPrediction,
    CandidatePrediction,
    EvaluationPredictionBatch,
    evaluation_observation_reference,
)
from ai_game_player.execution_receipt import ExecutionReceipt, ExecutionObservation
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome_models import OutcomeEvent, OutcomeEvidence
from ai_game_player.runtime_evaluation_adapter import RuntimeEvaluationAdapter
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRuntimeConfiguration,
)
from ai_game_player.run_control import RunController
from ai_game_player.runtime_mode_config import RuntimeModeConfig, RuntimeMode


SCOPE = KnowledgeScope("fixture", "scene", "profile", "local", "isolated")
OBSERVATION = ScreenObservation(
    "fixture", 200, 200, features={"signature": "fixed-observation"}
)


def percentiles(values: list[int]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": ordered[(len(ordered) - 1) // 2] / 1e6,
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] / 1e6,
    }


def candidates(count: int) -> list[ActionCandidate]:
    return [
        ActionCandidate(
            f"candidate.{index}",
            "click",
            f"Control {index}",
            x=index + 1,
            y=10,
            confidence=0.95,
        )
        for index in range(count)
    ]


def measure_adapter(case: str, count: int, iterations: int) -> dict[str, Any]:
    items = candidates(count)
    context = DecisionContextBuilder().build(OBSERVATION, items, items)
    adapter = RuntimeEvaluationAdapter(scope=SCOPE, goal_id="goal")
    adapter.apply(context, OBSERVATION)
    actual = None
    if case == "previous_actual":
        receipt = ExecutionReceipt(
            context.snapshot_id,
            items[0].action_id,
            0,
            "fixture",
            "before",
            "executed",
            True,
        )
        evidence = OutcomeEvidence(
            "state_delta",
            "state_delta",
            "changed",
            0.9,
            0.95,
            "state_delta/v1",
            {
                "state_evidence_available": True,
                "changed_fields": {"progress": {"before": 0, "after": 0.5}},
            },
        )
        actual = ExecutionObservation(
            receipt,
            OutcomeEvent(
                items[0].action_id,
                "changed",
                0.85,
                False,
                False,
                (evidence,),
                "fixture observed state",
            ),
            "fixture",
            "after",
        )
    if case == "candidate_hypothetical":
        adapter.set_predictions(
            EvaluationPredictionBatch(
                "fixture.1",
                evaluation_observation_reference(OBSERVATION),
                SCOPE,
                "goal",
                tuple(
                    CandidatePrediction(
                        item.action_id,
                        (
                            AxisPrediction(
                                "progress", 0.5, 0.9, "fixture.predictor", "1"
                            ),
                            AxisPrediction(
                                "survival", 0.8, 0.8, "fixture.predictor", "1"
                            ),
                        ),
                    )
                    for item in items
                ),
            )
        )
    target = replace(context, snapshot_id="next-fixture-step")
    times = []
    projected = None
    for index in range(iterations + 20):
        started = time.perf_counter_ns()
        projected = adapter.apply(target, OBSERVATION, actual, 0)
        elapsed = time.perf_counter_ns() - started
        if index >= 20:
            times.append(elapsed)
    projection = projected.evaluation_projection
    value = projection.to_dict()
    return {
        "case": case,
        "candidates": count,
        "iterations": iterations,
        **percentiles(times),
        "actual_available": value["previous_actual"]["quality"]["available"],
        "hypothetical_available": sum(
            quality.available
            for quality in projection.candidate_qualities(target.snapshot_id).values()
        ),
        "serialized_bytes": len(json.dumps(value, separators=(",", ":")).encode()),
    }


class Source:
    def read(self):
        return OBSERVATION, candidates(2)


class BenchmarkPassthrough:
    """測定対照だけのfixture。製品の評価/Safety設定を切り替えるAPIではない。"""

    def apply(self, context, *_arguments):
        return context


def measure_session(enabled: bool, iterations: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        config = SessionRuntimeConfiguration(
            "ローカル規則",
            "",
            "",
            Path(directory),
            True,
            None,
            "mouse",
            None,
            runtime_mode=RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY),
        )
        runtime = RuntimeComposition().create_session_runtime(
            config, source=Source(), controller=RunController()
        )
        if not enabled:
            runtime.pipeline.engine.evaluation_adapter = BenchmarkPassthrough()
        times = []
        try:
            for _ in range(iterations):
                started = time.perf_counter_ns()
                runtime.run()
                times.append(time.perf_counter_ns() - started)
            return {
                "evaluation_enabled": enabled,
                "iterations": iterations,
                **percentiles(times),
                "backend": "decision_only_no_os_input",
                "candidate_evidence": "unavailable",
            }
        finally:
            runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--session-iterations", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 20 <= args.iterations <= 2000 or not 10 <= args.session_iterations <= 200:
        parser.error("adapter iterations20..2000/session iterations10..200 required")
    result = {
        "schema": "runtime-evaluation-benchmark/v1",
        "kind": "fixture_no_game_accuracy_claim",
        "adapter": [
            measure_adapter(case, count, args.iterations)
            for case, count in (
                ("bootstrap", 2),
                ("previous_actual", 2),
                ("candidate_hypothetical", 2),
                ("candidate_hypothetical", 64),
            )
        ],
        "session": [
            measure_session(enabled, args.session_iterations)
            for enabled in (False, True)
        ],
    }
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
