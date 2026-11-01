"""Stop優先のMode/Phase制御。遅いprobeとJournal IOを実行停止のlockから分離する。"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import threading
from typing import Any, TypeVar
from uuid import uuid4

from ai_game_player.event_journal import EventJournal
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.runtime_mode_config import (
    PHASE_REQUIRED_OPERATION,
    MODE_CAPABILITIES,
    RuntimeMode,
    RuntimeModeConfig,
    RuntimeOperation,
    RuntimePhase,
    validate_runtime_identifier,
)
from ai_game_player.runtime_mode_state import (
    RuntimeModeSnapshot,
    RuntimeStepToken,
    read_runtime_mode_checkpoint,
)
from ai_game_player.runtime_mode_recorder import RuntimeModeRecorder


ResultT = TypeVar("ResultT")
_TRANSITIONS = {
    RuntimePhase.IDLE: frozenset(
        {RuntimePhase.INFER, RuntimePhase.LEARN, RuntimePhase.PAUSED}
    ),
    RuntimePhase.INFER: frozenset(
        {RuntimePhase.LEARN, RuntimePhase.EVALUATE, RuntimePhase.PAUSED}
    ),
    RuntimePhase.LEARN: frozenset({RuntimePhase.EVALUATE, RuntimePhase.PAUSED}),
    RuntimePhase.EVALUATE: frozenset(
        {
            RuntimePhase.PROMOTE,
            RuntimePhase.INFER,
            RuntimePhase.LEARN,
            RuntimePhase.PAUSED,
        }
    ),
    RuntimePhase.PROMOTE: frozenset(
        {RuntimePhase.INFER, RuntimePhase.LEARN, RuntimePhase.IDLE, RuntimePhase.PAUSED}
    ),
    RuntimePhase.PAUSED: frozenset(
        {RuntimePhase.INFER, RuntimePhase.LEARN, RuntimePhase.IDLE}
    ),
    RuntimePhase.ERROR: frozenset(),
    RuntimePhase.STOPPING: frozenset(),
}


# {
#   責務: [RuntimeModeTransitionError: Mode/PhaseやStep世代が要求操作を許さないことを表す]
#   フィールド: [なし: 固定reason codeをValueError本文に保持する]
# }
class RuntimeModeTransitionError(ValueError):
    pass


# {
#   責務: [RuntimeModePersistenceError: 状態記録の失敗または有界queue超過による停止を表す]
#   フィールド: [なし: 元のIO例外は例外連鎖で保持し、metadataへ自由文を保存しない]
# }
class RuntimeModePersistenceError(RuntimeError):
    pass


# {
#   責務: [RuntimeModeController: 既存RunControllerと協調しMode/Phase・Step所有・停止世代を制御する]
#   フィールド: [run_control: Sessionが共有する停止/再開制御, _state: 不変metadata Snapshot,
#     _lock: 短い状態CASのlock, _recorder: 停止から独立した有界Journal/checkpoint記録,
#     _stop_latch: lock待ちより先に新規操作を禁止するEvent,
#     _stop_signal_lock/_stop_revision/_stop_pending: 明示再開が新しいStopを消去しないための短い照合,
#     _held_input_probe: 実InputLedger等が保持入力の有無を返す関数。未指定/不明は安全境界を拒否,
#     _active_token/_owner_id/_step_sequence: Stepの世代とArtifact版の所有,
#     _run_generation: 明示再開時に固定した既存RunController世代]
# }
class RuntimeModeController:
    # {
    #   責務: [__init__: 安全なIDLEから共通Mode制御を構成する]
    #   処理: [軽量更新Modeを既定にし、UIやworkerを起動せず既存停止制御と記録先を保持する]
    #   引数: [config: 検証済みMode宣言, run_control: Sessionの停止制御,
    #     held_input_probe: held key/mouseがあればTrueを返す関数, journal: 任意Session Journal,
    #     state_path: 明示flush時のcheckpoint先, max_pending_events: 記録待ちmetadataの上限1〜4096]
    #   戻り値: [なし]
    #   エラー: [ValueError: configまたはqueue容量が不正]
    # }
    def __init__(
        self,
        config: RuntimeModeConfig | None = None,
        *,
        run_control: RunController | None = None,
        held_input_probe: Callable[[], bool] | None = None,
        journal: EventJournal | None = None,
        state_path: Path | None = None,
        max_pending_events: int = 128,
    ) -> None:
        selected = config or RuntimeModeConfig()
        if (
            not isinstance(selected, RuntimeModeConfig)
            or type(max_pending_events) is not int
            or not 1 <= max_pending_events <= 4096
        ):
            raise ValueError("runtime mode controller configuration is invalid")
        self.run_control = run_control or RunController()
        self._run_generation = self.run_control.rearm_token
        self._held_input_probe = held_input_probe
        self._recorder = RuntimeModeRecorder(journal, state_path, max_pending_events)
        self._lock = threading.RLock()
        self._stop_latch = threading.Event()
        self._stop_signal_lock = threading.Lock()
        self._stop_revision = 0
        self._stop_pending = 0
        self._state = RuntimeModeSnapshot(selected, selected)
        self._owner_id = uuid4().hex
        self._active_token: RuntimeStepToken | None = None
        self._step_sequence = 0

    # {
    #   責務: [snapshot: 現在の不変Mode/Phase metadataを取得する]
    #   処理: [状態lock内で参照を取得する。probeやIOは実行しない]
    #   引数: [self: 対象Controller]
    #   戻り値: [RuntimeModeSnapshot: 一時点の宣言。実行許可はpermits/run_if_allowedで再検証する]
    # }
    @property
    def snapshot(self) -> RuntimeModeSnapshot:
        with self._lock:
            return self._state

    # {
    #   責務: [request_mode: 実効Modeを変えず利用者の選択を次の安全境界へ予約する]
    #   処理: [Step中でもselected/pendingだけを記録し、現在のArtifactと実効世代を維持する]
    #   引数: [config: 次に適用したい検証済みMode宣言]
    #   戻り値: [RuntimeModeSnapshot: 予約を含む状態]
    #   エラー: [ValueError: config不正, RuntimeModePersistenceError: 記録queue超過]
    # }
    def request_mode(self, config: RuntimeModeConfig) -> RuntimeModeSnapshot:
        if not isinstance(config, RuntimeModeConfig):
            raise ValueError("runtime mode request requires a validated configuration")
        return self._metadata_change(
            "runtime.mode_requested",
            selected_config=config,
            pending_config=config,
            pending_reason=None,
        )

    # {
    #   責務: [apply_pending_mode: Stepとheld inputが無い境界で予約Modeを適用する]
    #   処理: [学習job段階の途中は拒否し、IDLEへ戻して世代を進める。停止や再開世代競合を最終CASで拒否する]
    #   引数: [self: 対象Controller]
    #   戻り値: [RuntimeModeSnapshot: 適用後状態。予約なしなら元状態]
    #   エラー: [RuntimeModeTransitionError: 境界不成立, ExecutionCancelled: 停止/旧世代]
    # }
    def apply_pending_mode(self) -> RuntimeModeSnapshot:
        if self.snapshot.pending_config is None:
            return self.snapshot

        def apply(state: RuntimeModeSnapshot) -> RuntimeModeSnapshot:
            if state.phase not in {
                RuntimePhase.IDLE,
                RuntimePhase.INFER,
                RuntimePhase.PAUSED,
            }:
                raise RuntimeModeTransitionError("mode_change_requires_safe_boundary")
            if state.pending_config is None:
                return state
            return self._change_locked(
                "runtime.mode_applied",
                invalidate=True,
                effective_config=state.pending_config,
                phase=RuntimePhase.IDLE,
                pending_config=None,
                pending_reason=None,
                degrade_reason=state.pending_reason,
                evaluation_version=None,
                evaluation_passed=False,
            )

        return self._admit(apply, require_boundary=True)

    # {
    #   責務: [transition: Modeが許すPhase間を安全境界で遷移する]
    #   処理: [遷移表、Mode能力、Step/held、評価済みcandidateを検証して実効世代を更新する]
    #   引数: [phase: 次のRuntimePhase]
    #   戻り値: [RuntimeModeSnapshot: 遷移後のmetadata]
    #   エラー: [RuntimeModeTransitionError: 不正遷移や能力不足, ExecutionCancelled: 停止]
    # }
    def transition(self, phase: RuntimePhase) -> RuntimeModeSnapshot:
        if not isinstance(phase, RuntimePhase):
            raise RuntimeModeTransitionError("invalid_phase")
        if phase == RuntimePhase.STOPPING:
            return self.request_stop()
        if phase == RuntimePhase.ERROR:
            return self.fail("explicit_error")

        def change(state: RuntimeModeSnapshot) -> RuntimeModeSnapshot:
            if phase == state.phase:
                return state
            if phase not in _TRANSITIONS[state.phase]:
                raise RuntimeModeTransitionError("invalid_phase_transition")
            operation = PHASE_REQUIRED_OPERATION.get(phase)
            if (
                operation is not None
                and operation not in state.effective_config.operations(phase)
            ):
                raise RuntimeModeTransitionError("mode_forbids_phase")
            if phase == RuntimePhase.PROMOTE and not (
                state.evaluation_passed and state.evaluation_version is not None
            ):
                raise RuntimeModeTransitionError("promotion_requires_evaluation")
            return self._change_locked(
                "runtime.phase_changed",
                invalidate=True,
                phase=phase,
                evaluation_version=state.evaluation_version
                if phase == RuntimePhase.PROMOTE
                else None,
                evaluation_passed=state.evaluation_passed
                if phase == RuntimePhase.PROMOTE
                else False,
            )

        return self._admit(
            change,
            require_boundary=True,
            check_held=phase
            in {RuntimePhase.LEARN, RuntimePhase.EVALUATE, RuntimePhase.PROMOTE},
        )

    # {
    #   責務: [begin_step: 推論StepのMode世代とArtifact版を固定する]
    #   処理: [INFERと単独Step所有を検証し、既存RunControllerの再開世代をtokenへ結び付ける]
    #   引数: [artifact_version: このStepが読み取る既存Champion/Snapshotの版]
    #   戻り値: [RuntimeStepToken: finish_stepまで固定された所有token]
    #   エラー: [RuntimeModeTransitionError: 推論不可/二重Step, ExecutionCancelled: 停止/旧世代]
    # }
    def begin_step(self, artifact_version: str) -> RuntimeStepToken:
        validate_runtime_identifier(artifact_version, "artifact version")

        def begin(state: RuntimeModeSnapshot) -> RuntimeStepToken:
            if (
                state.phase != RuntimePhase.INFER
                or RuntimeOperation.INFERENCE
                not in state.effective_config.operations(state.phase)
                or state.step_active
            ):
                raise RuntimeModeTransitionError("inference_step_not_available")
            self._step_sequence += 1
            token = RuntimeStepToken(
                self._owner_id,
                self._step_sequence,
                state.epoch,
                self._run_generation,
                artifact_version,
            )
            self._change_locked(
                None, step_active=True, artifact_version=artifact_version
            )
            self._active_token = token
            return token

        return self._admit(begin)

    # {
    #   責務: [finish_step: 停止後も所有していたStepの完了を回収する]
    #   処理: [一致するtokenだけを解放し、停止やPhaseを勝手に再開しない]
    #   引数: [token: 同じControllerでbegin_stepが発行したtoken]
    #   戻り値: [RuntimeModeSnapshot: Step所有を解除した状態]
    #   エラー: [RuntimeModeTransitionError: 他Controller/旧Step/二重完了]
    # }
    def finish_step(self, token: RuntimeStepToken) -> RuntimeModeSnapshot:
        with self._lock:
            if self._active_token is None or token != self._active_token:
                raise RuntimeModeTransitionError("step_token_does_not_match")
            self._active_token = None
            return self._change_locked(None, step_active=False)

    # {
    #   責務: [permits: 現在の停止・Mode・Phaseで操作を開始できるか照合する]
    #   処理: [Safetyは独立して必須とし、他操作は既存Run世代・停止latch・能力表を確認する]
    #   引数: [operation: 宣言済み操作種別]
    #   戻り値: [bool: 現時点のadmission。後の公開時にはrun_if_allowedで再検証する]
    # }
    def permits(self, operation: RuntimeOperation) -> bool:
        if not isinstance(operation, RuntimeOperation):
            return False
        if operation == RuntimeOperation.SAFETY_CORE:
            return True
        state = self.snapshot
        if (
            self._stop_latch.is_set()
            or state.stop_requested
            or operation not in state.effective_config.operations(state.phase)
        ):
            return False
        try:
            self.run_control.ensure_running(self._run_generation)
        except ExecutionCancelled:
            return False
        if operation == RuntimeOperation.PROMOTION:
            return (
                not state.step_active
                and state.evaluation_passed
                and self._held_is_clear()
            )
        return True

    # {
    #   責務: [run_if_allowed: 計算結果の短い公開処理をStopと世代CASで直列化する]
    #   処理: [遅いprobeはlock外で実行し、最終Run→mode lock内で許可とepochを再検証する]
    #   引数: [operation: 公開対象の操作, expected_epoch: 計算開始時に捕捉した実効世代,
    #     commit: 準備済みの短いメモリ公開処理。probe/Journal/長いIOや推論を含めない]
    #   戻り値: [ResultT: commitの結果]
    #   エラー: [RuntimeModeTransitionError: 禁止/世代不一致, ExecutionCancelled: 停止, Exception: commitの失敗]
    # }
    def run_if_allowed(
        self,
        operation: RuntimeOperation,
        expected_epoch: int,
        commit: Callable[[], ResultT],
    ) -> ResultT:
        if not isinstance(operation, RuntimeOperation):
            raise RuntimeModeTransitionError("invalid_operation")
        if operation == RuntimeOperation.SAFETY_CORE:
            return commit()

        def publish(state: RuntimeModeSnapshot) -> ResultT:
            if operation not in state.effective_config.operations(state.phase):
                raise RuntimeModeTransitionError("mode_forbids_operation")
            if operation == RuntimeOperation.PROMOTION and not state.evaluation_passed:
                raise RuntimeModeTransitionError("promotion_requires_evaluation")
            return commit()

        return self._admit(
            publish,
            expected_epoch=expected_epoch,
            require_boundary=operation == RuntimeOperation.PROMOTION,
        )

    # {
    #   責務: [record_evaluation: 外部Gateが評価したcandidateの版と合否をPhaseへ結び付ける]
    #   処理: [EVALUATE段階と計算世代を確認する。本番ChampionやArtifact自体は変更しない]
    #   引数: [candidate_version: 評価対象版, passed: 外部評価合格, expected_epoch: 評価開始世代]
    #   戻り値: [RuntimeModeSnapshot: 評価結果metadata]
    #   エラー: [ValueError: 不正な版/合否, RuntimeModeTransitionError: 段階/世代不一致]
    # }
    def record_evaluation(
        self, candidate_version: str, passed: bool, *, expected_epoch: int
    ) -> RuntimeModeSnapshot:
        validate_runtime_identifier(candidate_version, "candidate version")
        if type(passed) is not bool:
            raise ValueError("evaluation result must be boolean")

        def record(state: RuntimeModeSnapshot) -> RuntimeModeSnapshot:
            if state.phase != RuntimePhase.EVALUATE:
                raise RuntimeModeTransitionError("evaluation_phase_required")
            return self._change_locked(
                "runtime.evaluation_recorded",
                evaluation_version=candidate_version,
                evaluation_passed=passed,
            )

        return self._admit(record, expected_epoch=expected_epoch)

    # {
    #   責務: [degrade: 学習を先に軽くする要求を安全境界まで予約する]
    #   処理: [並行学習/交互実行→Light→Inference Onlyの順に下げ、Learning OnlyはPAUSEDへ移す]
    #   引数: [reason: 資源不足等を表す固定reason code]
    #   戻り値: [RuntimeModeSnapshot: 予約状態。呼出側が境界でapply_pending_modeを実行する]
    #   エラー: [RuntimeModeTransitionError: これ以上軽いModeがない]
    # }
    def degrade(self, reason: str) -> RuntimeModeSnapshot:
        validate_runtime_identifier(reason, "degrade reason")
        state = self.snapshot
        targets = {
            RuntimeMode.INFERENCE_WITH_LEARNING: RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION,
            RuntimeMode.ALTERNATING_INFER_LEARN: RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION,
            RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION: RuntimeMode.INFERENCE_ONLY,
        }
        if state.mode == RuntimeMode.LEARNING_ONLY:
            return self._admit(
                lambda _: self._change_locked(
                    "runtime.learning_degraded",
                    invalidate=True,
                    phase=RuntimePhase.PAUSED,
                    degrade_reason=reason,
                ),
                expected_epoch=state.epoch,
            )
        if state.mode not in targets:
            raise RuntimeModeTransitionError("no_lighter_runtime_mode")
        pending = RuntimeModeConfig(
            targets[state.mode], state.effective_config.resource_policy_id
        )
        if state.pending_config is not None and (
            MODE_CAPABILITIES[state.pending_config.mode]
            <= MODE_CAPABILITIES[pending.mode]
        ):
            pending = state.pending_config
        return self._metadata_change(
            "runtime.degrade_requested",
            expected_version=state.version,
            pending_config=pending,
            pending_reason=reason,
        )

    # {
    #   責務: [request_stop: 全Mode/Phaseの新しい操作をIOやprobeを待たず停止する]
    #   処理: [latchを先に立て、既存RunControllerを止め、世代とPhaseをSTOPPINGへ変更する]
    #   引数: [reason: 固定の停止reason code]
    #   戻り値: [RuntimeModeSnapshot: 停止中の状態。held inputのreleaseは既存Executor/FailSafeの責務]
    # }
    def request_stop(self, reason: str = "stop_requested") -> RuntimeModeSnapshot:
        validate_runtime_identifier(reason, "stop reason")
        self._signal_stop()
        try:
            self.run_control.stop(reason)
            with self._lock:
                return self._change_locked(
                    "runtime.stop_requested",
                    invalidate=True,
                    force=True,
                    phase=RuntimePhase.STOPPING,
                    stop_requested=True,
                    pending_config=None,
                    pending_reason=None,
                    evaluation_version=None,
                    evaluation_passed=False,
                )
        finally:
            self._complete_stop_signal()

    # {
    #   責務: [fail: 学習やruntimeの失敗をERRORと停止へ反映しArtifact版を維持する]
    #   処理: [停止を先に適用し、外部Championを書き換えず失敗metadataを記録待ちにする]
    #   引数: [reason: 固定の失敗reason code]
    #   戻り値: [RuntimeModeSnapshot: ERROR状態]
    # }
    def fail(self, reason: str) -> RuntimeModeSnapshot:
        validate_runtime_identifier(reason, "failure reason")
        self._signal_stop()
        try:
            self.run_control.stop(reason)
            with self._lock:
                return self._change_locked(
                    "runtime.mode_error",
                    invalidate=True,
                    force=True,
                    phase=RuntimePhase.ERROR,
                    stop_requested=True,
                    degrade_reason=reason,
                    pending_config=None,
                    pending_reason=None,
                    evaluation_version=None,
                    evaluation_passed=False,
                )
        finally:
            self._complete_stop_signal()

    # {
    #   責務: [recover_to_idle: 入力とStepが解放された失敗/停止状態を、再開しないIDLEへ戻す]
    #   処理: [held probeをlock外で検査し、停止latchを維持したまま世代を進める]
    #   引数: [self: 対象Controller]
    #   戻り値: [RuntimeModeSnapshot: 明示再開を待つ安全IDLE]
    #   エラー: [RuntimeModeTransitionError: 未解放/不明入力、Step残留、回復対象でないPhase]
    # }
    def recover_to_idle(self) -> RuntimeModeSnapshot:
        captured = self.snapshot
        if not self._held_is_clear():
            raise RuntimeModeTransitionError("held_input_not_clear")
        with self._lock:
            if (
                self._state.version != captured.version
                or self._state.step_active
                or self._state.phase not in {RuntimePhase.ERROR, RuntimePhase.STOPPING}
            ):
                raise RuntimeModeTransitionError("recovery_boundary_not_available")
            return self._change_locked(
                "runtime.recovered_idle",
                invalidate=True,
                phase=RuntimePhase.IDLE,
                stop_requested=True,
                evaluation_version=None,
                evaluation_passed=False,
            )

    # {
    #   責務: [resume_at_boundary: hostが明示再開した既存RunController世代を採用する]
    #   処理: [Stepとheld inputが無いIDLE/PAUSEDだけで停止latchを解除する。学習や昇格は再開しない]
    #   引数: [self: 対象Controller。hostが先にRunController.startを行う]
    #   戻り値: [RuntimeModeSnapshot: Phase=IDLEの新しい実効世代]
    #   エラー: [RuntimeModeTransitionError: 境界不成立, ExecutionCancelled: hostが未再開/世代競合]
    # }
    def resume_at_boundary(self) -> RuntimeModeSnapshot:
        captured = self.snapshot
        with self._stop_signal_lock:
            stop_revision = self._stop_revision
        if not self._held_is_clear():
            raise RuntimeModeTransitionError("held_input_not_clear")
        generation = self.run_control.rearm_token
        result = []

        def resume() -> None:
            with self._lock:
                if (
                    self._state.version != captured.version
                    or self._state.step_active
                    or self._state.phase not in {RuntimePhase.IDLE, RuntimePhase.PAUSED}
                ):
                    raise RuntimeModeTransitionError("resume_boundary_not_available")
                with self._stop_signal_lock:
                    if self._stop_pending or self._stop_revision != stop_revision:
                        raise ExecutionCancelled("runtime stop superseded resume")
                    self._run_generation = generation
                    self._stop_latch.clear()
                    result.append(
                        self._change_locked(
                            "runtime.explicit_resume",
                            invalidate=True,
                            phase=RuntimePhase.IDLE,
                            stop_requested=False,
                            evaluation_version=None,
                            evaluation_passed=False,
                        )
                    )

        try:
            self.run_control.run_if_current(generation, resume)
        except RuntimeModePersistenceError:
            self.run_control.stop("mode_journal_backpressure")
            raise
        return result[0]

    # {
    #   責務: [_signal_stop: Run lockへ到達する前に新しい停止要求を可視にする]
    #   処理: [短いsignal lockでlatchと停止版を更新し、途中の明示再開による消去を防ぐ]
    #   引数: [self: 対象Controller]
    #   戻り値: [なし]
    # }
    def _signal_stop(self) -> None:
        with self._stop_signal_lock:
            self._stop_latch.set()
            self._stop_revision += 1
            self._stop_pending += 1

    # {
    #   責務: [_complete_stop_signal: Run停止とmetadata反映が終わった要求を回収する]
    #   処理: [新しい停止が進行中である間はresumeがlatchを消せないよう件数を保持する]
    #   引数: [self: 対象Controller]
    #   戻り値: [なし]
    # }
    def _complete_stop_signal(self) -> None:
        with self._stop_signal_lock:
            self._stop_pending -= 1

    # {
    #   責務: [flush: 有界metadata queueと最新checkpointを停止用lock外で保存する]
    #   処理: [他のflushとだけ直列化し、Journal記録成功後にqueueを進めて原子的JSONを保存する]
    #   引数: [self: 対象Controller。hot pathやRunController commit callback内から呼ばない]
    #   戻り値: [bool: 別flushが実行中ならFalse。全pendingを記録できた場合True]
    #   エラー: [RuntimeModePersistenceError: IO失敗。ControllerをERRORにして既存RunControllerも止める]
    # }
    def flush(self) -> bool:
        try:
            return self._recorder.flush(lambda: self.snapshot)
        except Exception as error:
            self.fail("mode_persistence_failure")
            raise RuntimeModePersistenceError(
                "runtime mode persistence failed"
            ) from error

    # {
    #   責務: [restore: checkpointから利用者のMode選択を復元し、処理途中を再開しない]
    #   処理: [有界JSONを検証し、版・破損はdefault Lightへ戻す。正常時もPhase=IDLE/停止latchありで構成する]
    #   引数: [state_path: 保存metadata先, arguments: 既存RunController/probe/Journal等のconstructor引数]
    #   戻り値: [RuntimeModeController: 明示再開が必要な安全IDLE]
    # }
    @classmethod
    def restore(cls, state_path: Path, **arguments: Any) -> "RuntimeModeController":
        recovered = None
        reason = "crash_recovery"
        try:
            recovered = read_runtime_mode_checkpoint(state_path)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            reason = "invalid_checkpoint"
        result = cls(
            None if recovered is None else recovered.selected_config,
            state_path=state_path,
            **arguments,
        )
        result._stop_latch.set()
        with result._lock:
            if recovered is not None:
                result._state = replace(
                    recovered,
                    phase=RuntimePhase.IDLE,
                    version=recovered.version + 1,
                    epoch=recovered.epoch + 1,
                    stop_requested=True,
                    step_active=False,
                    evaluation_version=None,
                    evaluation_passed=False,
                )
            else:
                result._state = replace(
                    result._state, stop_requested=True, degrade_reason=reason
                )
            result._change_locked(
                "runtime.mode_restored", force=True, stop_requested=True
            )
        return result

    # {
    #   責務: [_held_is_clear: 入力解放の根拠を停止用lock外で取得する]
    #   処理: [probeなし・例外・bool以外を不明として拒否する]
    #   引数: [self: 対象Controller]
    #   戻り値: [bool: probeが厳密にFalseを返した場合だけTrue]
    # }
    def _held_is_clear(self) -> bool:
        try:
            return (
                self._held_input_probe is not None and self._held_input_probe() is False
            )
        except Exception:
            return False

    # {
    #   責務: [_admit: 遅い境界検査と短いStop/世代CASを分離する]
    #   処理: [probeを先に取得し、RunController→modeのlock順で状態競合と停止を再検証する]
    #   引数: [work: lock内の短い状態処理, expected_epoch: 任意の計算開始世代,
    #     require_boundary: Step解放を要求するか, check_held: 境界でheld input解放を必要とするか]
    #   戻り値: [ResultT: workの結果]
    #   エラー: [RuntimeModeTransitionError: 不成立/競合, ExecutionCancelled: 停止/再開世代不一致]
    # }
    def _admit(
        self,
        work: Callable[[RuntimeModeSnapshot], ResultT],
        *,
        expected_epoch: int | None = None,
        require_boundary: bool = False,
        check_held: bool = True,
    ) -> ResultT:
        captured = self.snapshot
        if self._stop_latch.is_set():
            raise ExecutionCancelled("runtime mode is stopping")
        if expected_epoch is not None and (
            type(expected_epoch) is not int or expected_epoch < 0
        ):
            raise RuntimeModeTransitionError("invalid_expected_epoch")
        if require_boundary and check_held:
            held_clear = self._held_is_clear()
            if self._stop_latch.is_set():
                raise ExecutionCancelled("runtime mode is stopping")
            if not held_clear:
                raise RuntimeModeTransitionError("held_input_not_clear")
        result = []

        def commit() -> None:
            with self._lock:
                state = self._state
                if self._stop_latch.is_set() or state.stop_requested:
                    raise ExecutionCancelled("runtime mode is stopping")
                if state.version != captured.version or (
                    expected_epoch is not None and state.epoch != expected_epoch
                ):
                    raise RuntimeModeTransitionError("runtime_state_generation_changed")
                if require_boundary and state.step_active:
                    raise RuntimeModeTransitionError("runtime_step_still_active")
                result.append(work(state))

        try:
            self.run_control.run_if_current(self._run_generation, commit)
        except RuntimeModePersistenceError:
            self.run_control.stop("mode_journal_backpressure")
            raise
        return result[0]

    # {
    #   責務: [_metadata_change: 利用者の予約等を実効世代を変えずに記録する]
    #   処理: [短いmetadata変更だけをlock内で行い、停止時も選択の保存を可能にする]
    #   引数: [event: 固定event種別, expected_version: 読取済み状態のCAS版, changes: Snapshotの変更項目]
    #   戻り値: [RuntimeModeSnapshot: 更新後状態]
    #   エラー: [RuntimeModePersistenceError: 記録queue超過]
    # }
    def _metadata_change(
        self, event: str, *, expected_version: int | None = None, **changes: Any
    ) -> RuntimeModeSnapshot:
        try:
            with self._lock:
                if (
                    expected_version is not None
                    and self._state.version != expected_version
                ):
                    raise RuntimeModeTransitionError("runtime_state_generation_changed")
                return self._change_locked(event, **changes)
        except RuntimeModePersistenceError:
            self.run_control.stop("mode_journal_backpressure")
            raise

    # {
    #   責務: [_change_locked: IOを行わず状態と有界の記録待ちmetadataを確定する]
    #   処理: [版と必要な世代を進め、queue超過はfail-closedにする。Stop/failはqueue満杯でも適用する]
    #   引数: [event: 記録種別。Step所有変更だけならNone, invalidate: 古い結果を失効させるか,
    #     force: 停止/失敗を必ず適用するか, changes: 新しいmetadata値]
    #   戻り値: [RuntimeModeSnapshot: 新状態]
    #   エラー: [RuntimeModePersistenceError: 通常変更のJournal queue上限超過]
    # }
    def _change_locked(
        self,
        event: str | None,
        *,
        invalidate: bool = False,
        force: bool = False,
        **changes: Any,
    ) -> RuntimeModeSnapshot:
        old = self._state
        if event is not None and self._recorder.full and not force:
            self._stop_latch.set()
            self._state = replace(
                old,
                phase=RuntimePhase.ERROR,
                version=old.version + 1,
                epoch=old.epoch + 1,
                stop_requested=True,
                degrade_reason="journal_backpressure",
                evaluation_version=None,
                evaluation_passed=False,
            )
            raise RuntimeModePersistenceError(
                "runtime mode journal queue capacity reached"
            )
        self._state = replace(
            old, version=old.version + 1, epoch=old.epoch + int(invalidate), **changes
        )
        if event is not None:
            payload = {"event": event, **self._state.to_dict()}
            self._recorder.enqueue(
                f"runtime-mode.{self._owner_id}.{self._state.version}", payload
            )
        return self._state
