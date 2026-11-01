"""実Session/Pipelineと非OS入力fixtureでlight workerの追加遅延を測る。"""

import argparse
import json
from pathlib import Path
import tempfile
import time
from typing import Any

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.light_adaptation_budget import LightResourceSample
from ai_game_player.light_utility import UtilityMeasurement
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.run_control import RunController
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRuntimeConfiguration,
)
from ai_game_player.runtime_mode_config import RuntimeMode, RuntimeModeConfig


# {
#   責務: [FixtureSource: 画面取込負荷を除外した決定的な汎用候補を生成する]
#   フィールド: [sequence: frameを区別する単調連番]
# }
class FixtureSource:
    # {
    #   責務: [__init__: 測定ごとのframe系列を初期化する]
    #   処理: [連番0] 引数: [なし] 戻り値: [なし]
    # }
    def __init__(self) -> None:
        self.sequence = 0

    # {
    #   責務: [read: 実Pipelineへ画面参照と有効候補を渡す]
    #   処理: [背景signatureだけ変える。因果成果は申告しない]
    #   引数: [なし] 戻り値: [tuple: fixture観測と候補]
    # }
    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        self.sequence += 1
        return ScreenObservation(
            "fixture", 100, 100, features={"signature": str(self.sequence)}
        ), [
            ActionCandidate(
                f"fixture-action.{self.sequence}", "key", "LEFT", confidence=0.95
            )
        ]


# {
#   責務: [FixtureInput: OS入力を送らず、実ActionExecutorのdispatch回数だけを計測する]
#   フィールド: [has_held_inputs: owned入力無し, calls: dispatch実測回数]
# }
class FixtureInput:
    has_held_inputs = False

    # {
    #   責務: [__init__: 入力fixtureの実行回数を初期化する]
    #   処理: [count0] 引数: [なし] 戻り値: [なし]
    # }
    def __init__(self) -> None:
        self.calls = 0

    # {
    #   責務: [execute: 非OSのfixture dispatchを実行する]
    #   処理: [count増分とexecuted receipt用結果を返す]
    #   引数: [candidate: 実Verifierを通過した候補] 戻り値: [ExecutionResult: fixtureのみの送信結果]
    # }
    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(candidate.action_id, True, "fixture", "no OS input")


# {
#   責務: [percentiles: 有限測定列のp50/p95を計算する]
#   処理: [nearest-rankでmsを出力] 引数: [values: ns列] 戻り値: [dict: p50/p95 ms]
# }
def percentiles(values: list[int]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50_ms": ordered[(len(ordered) - 1) // 2] / 1e6,
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] / 1e6,
    }


# {
#   責務: [measure: production compositionを使いMode/資源圧ごとの入力経路を比較する]
#   処理: [一つの固定fixtureを実Session/Pipeline/Verifier/Executorへ通しqueueとinputの遅延を測る]
#   引数: [mode: 共通Mode宣言, pressure: 学習資源圧, iterations: 有限回数]
#   戻り値: [dict: ms統計・dispatch数・queue drop。ゲーム成果を含まない]
# }
def measure(mode: RuntimeMode, pressure: bool, iterations: int) -> dict[str, Any]:
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
            runtime_mode=RuntimeModeConfig(mode),
        )
        source = FixtureSource()
        runtime = RuntimeComposition().create_session_runtime(
            config, source=source, controller=RunController()
        )
        backend = FixtureInput()
        runtime.pipeline.executor.dry_run = False
        runtime.pipeline.executor.live_executor = backend
        clock_origin = time.monotonic()
        runtime.pipeline.executor.safety_guard.clock = lambda: (
            clock_origin + source.sequence * 2.0
        )
        runtime.adaptation.runner.set_resources(LightResourceSample(pressure=pressure))
        input_times, enqueue_times = [], []
        try:
            for _ in range(iterations):
                started = time.perf_counter_ns()
                runtime.run_and_execute()
                input_times.append(time.perf_counter_ns() - started)
                started = time.perf_counter_ns()
                runtime.adaptation.runner.record_utility(
                    UtilityMeasurement("fixture_enqueue", 1)
                )
                enqueue_times.append(time.perf_counter_ns() - started)
            metrics = runtime.adaptation.runner.metrics
            return {
                "mode": mode.value,
                "resource_pressure": pressure,
                "iterations": iterations,
                "input_pipeline": percentiles(input_times),
                "enqueue": percentiles(enqueue_times),
                "fixture_dispatches": backend.calls,
                "queue_dropped": metrics.dropped,
                "worker_updates": metrics.updates,
            }
        finally:
            runtime.close()


# {
#   責務: [main: 再現可能なlight runnerの性能artifactを生成する]
#   処理: [三条件を同じfixtureで測る。実OS入力/ゲーム精度とは明示区別する]
#   引数: [CLI: --iterations 20..2000, --output 任意ローカルJSON] 戻り値: [int: 成功0]
# }
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if not 20 <= arguments.iterations <= 2000:
        parser.error("iterations must be in 20..2000")
    results = {
        "schema": "light-adaptation-benchmark/v1",
        "input_backend": "fixture_no_os_input",
        "fixture_safety_clock_step_seconds": 2,
        "results": [
            measure(mode, pressure, arguments.iterations)
            for mode, pressure in (
                (RuntimeMode.INFERENCE_ONLY, False),
                (RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION, False),
                (RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION, True),
            )
        ],
    }
    payload = json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
