"""本番SessionのStep所有・実行観測と、有界軽量workerのcomposition。"""

from pathlib import Path
import inspect
import time
from uuid import uuid4

from ai_game_player.execution_receipt import ExecutionObservation
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_identity import LearningIdentityPolicy
from ai_game_player.light_adaptation import LightAdaptationRunner
from ai_game_player.light_adaptation_budget import LightAdaptationBudget
from ai_game_player.light_execution_projection import (
    LightExecutionProjector,
    VerifiedActionEffect,
)
from ai_game_player.light_utility import UtilityMeasurement
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.run_control import RunController
from ai_game_player.runtime_mode_config import RuntimeModeConfig, RuntimePhase
from ai_game_player.runtime_mode_controller import RuntimeModeController
from ai_game_player.runtime_mode_state import RuntimeStepToken


# {
#   責務: [SessionLightAdaptation: production SessionのStep境界へModeとlight workerを接続する]
#   フィールド: [pipeline: 実Pipeline, mode: Session共有RunControlのMode gate,
#     runner: bounded worker, projector: receiptと学習keyの対応, _step_started/_busy: 所有Stepの測定]
# }
class SessionLightAdaptation:
    # {
    #   責務: [__init__: Sessionの標準推論を保ち、明示scope/profileだけを共有学習に使う]
    #   処理: [scope省略時はSession固有opaque scopeで隔離し、未知IDは学習しない。
    #     Mode/workerを作りcached版adapterを実Engineへ接続する]
    #   引数: [pipeline/root/controller: Session本番部品, config: Mode宣言,
    #     scope/identity: 任意の信頼済み設定, budget: 有限更新予算]
    #   戻り値: [なし] エラー: [ValueError/OSError: 構成または初期保存の失敗]
    # }
    def __init__(
        self,
        pipeline: DecisionPipeline,
        root: Path,
        controller: RunController,
        *,
        config: RuntimeModeConfig | None = None,
        scope: KnowledgeScope | None = None,
        identity: LearningIdentityPolicy | None = None,
        budget: LightAdaptationBudget | None = None,
    ) -> None:
        self.pipeline = pipeline
        self._busy = False
        self._step_started = 0.0
        self.mode = RuntimeModeController(
            config,
            run_control=controller,
            held_input_probe=self._held_inputs,
            journal=pipeline.event_journal,
        )
        self.mode.transition(RuntimePhase.INFER)
        selected_scope = scope or KnowledgeScope(
            "session." + uuid4().hex, "unresolved", "unavailable", "local", "isolated"
        )
        self.runner = LightAdaptationRunner(
            selected_scope, root, self.mode, identity=identity, budget=budget
        )
        self.projector = LightExecutionProjector(selected_scope, self.runner.identity)
        pipeline.engine.knowledge_adapter = self.runner.adapter
        self.runner.start()

    # {
    #   責務: [_held_inputs: worker公開境界でowned key/mouseとStep実行中状態をIOなしで検査する]
    #   処理: [未知custom backendはfail closed、dryrun/未作成backendは保持無しとする]
    #   引数: [なし] 戻り値: [bool: 保持または判定不能ならTrue]
    # }
    def _held_inputs(self) -> bool:
        if self._busy:
            return True
        executor = self.pipeline.executor
        if executor.dry_run or executor.live_executor is None:
            return False
        held = inspect.getattr_static(executor.live_executor, "has_held_inputs", None)
        if type(held) is bool:
            return held
        from ai_game_player.windows_input import WindowsInputExecutor

        if type(executor.live_executor) is WindowsInputExecutor:
            return executor.live_executor.has_held_inputs
        return True

    # {
    #   責務: [begin_step: safe境界でだけpending Modeと知識を切り替え、一版のStepを所有する]
    #   処理: [公開はmemoryだけ、Artifact版をtokenに固定してからbusyにする]
    #   引数: [なし] 戻り値: [RuntimeStepToken: 同期Stepの所有token]
    # }
    def begin_step(self) -> RuntimeStepToken:
        if self.mode.snapshot.pending_config is not None:
            self.mode.apply_pending_mode()
        self.runner.publish_at_boundary()
        current = self.runner.capture(self.runner.scope)
        token = self.mode.begin_step(current.version if current else "static")
        self._busy = True
        self._step_started = time.monotonic()
        return token

    # {
    #   責務: [finish_step: 実判断の識別対応と数値時間を有限queueへ渡してStepを解放する]
    #   処理: [通常Outcomeをmodeで止めず、学習enqueueだけrunner gateへ任せる]
    #   引数: [token: beginで所有したStep] 戻り値: [なし]
    # }
    def finish_step(self, token: RuntimeStepToken) -> None:
        try:
            context = self.pipeline.engine.last_decision_context
            if context is not None:
                self.projector.bind(context)
            observed = self.pipeline.engine.last_execution_observation
            if observed is not None:
                self.observe_execution(observed)
            self.runner.record_utility(
                UtilityMeasurement(
                    "decision_pipeline", (time.monotonic() - self._step_started) * 1000
                )
            )
        finally:
            self._busy = False
            self.mode.finish_step(token)

    # {
    #   責務: [observe_execution: 実行記録を保ちつつ、学習用の明示効果とunknownを分ける]
    #   処理: [receiptとprofileの再対応を検証し、有限canonical deltaだけをenqueueする]
    #   引数: [observation: #628実行観測, evidence: 任意の信頼済み因果証拠]
    #   戻り値: [bool: 学習queueへ採用したか]
    # }
    def observe_execution(
        self,
        observation: ExecutionObservation,
        evidence: VerifiedActionEffect | None = None,
    ) -> bool:
        event = self.projector.project(observation, evidence)
        return self.runner.enqueue(event) if event is not None else False

    # {
    #   責務: [close: Session停止時に新規学習をlatchしworkerを終了する]
    #   処理: [Mode StopはIO/probeを待たず、worker joinと記録flushを後で行う]
    #   引数: [なし] 戻り値: [なし] エラー: [RuntimeError: 終了未確認]
    # }
    def close(self) -> None:
        self.mode.request_stop()
        self.runner.close()
        self.mode.flush()
