from collections import deque

from ai_game_player.self_check_models import (
    SelfCheckCategory,
    SelfCheckConfig,
    SelfCheckEvent,
    SelfCheckSample,
    SelfCheckSeverity,
)


# {
#   責務: [
#     SelfCheckMonitor: ゲーム固有presetに依存せず、step履歴から異常兆候を検出する
#   ]
#   フィールド: [
#     config: 有限履歴と検出しきい値
#     _history: 直近stepのrolling履歴
#     _active_categories: 継続中の検出を重複通知しないための集合
#     _step_count: Monitor開始後のstep数
#   ]
#   処理: [
#     observe: stepを履歴へ追加し、異常兆候の新規イベントを返す
#     reset: 履歴と通知状態を初期化する
#   ]
# }
class SelfCheckMonitor:
    # {
    #   責務: [
    #     __init__: 有限サイズの履歴とSelf-Check検出状態を初期化する
    #   ]
    #   処理: [
    #     1: 設定を既定値または指定値で確定する
    #     2: 上限付きrolling履歴とイベント状態を用意する
    #   ]
    #   引数: [
    #     config: 履歴・周期・停滞・時間・評価器判定の設定
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def __init__(self, config: SelfCheckConfig | None = None) -> None:
        self.config = config or SelfCheckConfig()
        self._history: deque[SelfCheckSample] = deque(maxlen=self.config.history_limit)
        self._active_categories: set[SelfCheckCategory] = set()
        self._step_count = 0

    # {
    #   責務: [
    #     observe: 1step分の事実を蓄積し、新たに成立したSelf-Checkイベントを返す
    #   ]
    #   処理: [
    #     1: 入力stepを有限rolling履歴へ追加する
    #     2: 周期、停滞、期待差、Scene時間、評価器対立を独立して判定する
    #     3: 継続中の同一カテゴリを抑制し、新規イベントだけを返す
    #   ]
    #   引数: [
    #     sample: 実際のOutcomeと汎用状態・Scene・評価器情報
    #   ]
    #   戻り値: [
    #     events: 今stepで新たに発生したSelf-Checkイベント
    #   ]
    # }
    def observe(self, sample: SelfCheckSample) -> tuple[SelfCheckEvent, ...]:
        self._history.append(sample)
        self._step_count += 1
        detected_events = tuple(
            event
            for event in (
                self._detect_repeated_cycle(),
                self._detect_stagnation(),
                self._detect_outcome_mismatch(sample),
                self._detect_scene_duration(sample),
                self._detect_evaluator_disagreement(sample),
            )
            if event is not None
        )
        active_categories = {event.category for event in detected_events}
        # 同じ兆候が継続する間は通知を一度に制限し、条件が消えた後は再通知できるようにする。
        new_events = tuple(
            event for event in detected_events if event.category not in self._active_categories
        )
        self._active_categories = active_categories
        return new_events

    # {
    #   責務: [
    #     reset: 次の独立Sessionに向けて履歴と通知抑制状態を消去する
    #   ]
    #   処理: [
    #     1: rolling履歴を空にする
    #     2: step数と継続中カテゴリを初期化する
    #   ]
    #   引数: [
    #     self: 初期化するMonitor
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def reset(self) -> None:
        self._history.clear()
        self._active_categories.clear()
        self._step_count = 0

    # {
    #   責務: [
    #     _detect_repeated_cycle: 直近stepに同じaction/state列が反復しているか判定する
    #   ]
    #   処理: [
    #     1: 設定された周期長ごとに必要なrolling区間を確認する
    #     2: Scene・action ID・状態識別子の列が反復していれば根拠付き警告を作る
    #   ]
    #   引数: [
    #     self: 履歴と判定設定を所有するMonitor
    #   ]
    #   戻り値: [
    #     event: 反復Cycleを検出したイベント。検出なしの場合はNone
    #   ]
    # }
    def _detect_repeated_cycle(self) -> SelfCheckEvent | None:
        history = tuple(self._history)
        for period in range(1, self.config.maximum_cycle_period + 1):
            window_size = period * self.config.minimum_cycle_repetitions
            if len(history) < window_size:
                continue
            recent = history[-window_size:]
            cycle_pattern = tuple(
                (sample.scene_id, sample.actual_outcome.action_id, sample.state_signature)
                for sample in recent[:period]
            )
            if not all(
                (sample.scene_id, sample.actual_outcome.action_id, sample.state_signature)
                == cycle_pattern[index % period]
                for index, sample in enumerate(recent)
            ):
                continue

            # 状態と操作の両方を含め、同じ操作でも進展したケースをCycle扱いしない。
            scene_ids = ",".join(scene for scene, _, _ in cycle_pattern)
            action_ids = ",".join(action_id for _, action_id, _ in cycle_pattern)
            state_signatures = ",".join(state for _, _, state in cycle_pattern)
            return self._create_event(
                SelfCheckCategory.REPEATED_CYCLE,
                SelfCheckSeverity.WARNING,
                f"action/state pattern repeated with period {period}",
                (
                    f"period={period}",
                    f"repetitions={self.config.minimum_cycle_repetitions}",
                    f"scenes={scene_ids}",
                    f"actions={action_ids}",
                    f"states={state_signatures}",
                ),
                recent[-1].scene_id,
            )
        return None

    # {
    #   責務: [
    #     _detect_stagnation: Outcomeが連続してunchangedのままか判定する
    #   ]
    #   処理: [
    #     1: 設定されたstep数分の履歴を取り出す
    #     2: 全stepがunchangedなら停滞の根拠を作る
    #   ]
    #   引数: [
    #     self: rolling履歴を所有するMonitor
    #   ]
    #   戻り値: [
    #     event: 停滞を検出したイベント。検出なしの場合はNone
    #   ]
    # }
    def _detect_stagnation(self) -> SelfCheckEvent | None:
        window_size = self.config.stagnation_limit
        if len(self._history) < window_size:
            return None
        recent = tuple(self._history)[-window_size:]
        # unknownは未判定であり「進展なし」の証拠ではないため、停滞stepに数えない。
        if not all(
            sample.actual_outcome.status == "unchanged"
            and sample.actual_outcome.confidence >= self.config.minimum_outcome_confidence
            for sample in recent
        ):
            return None
        action_ids = ",".join(sample.actual_outcome.action_id for sample in recent)
        return self._create_event(
            SelfCheckCategory.STAGNATION,
            SelfCheckSeverity.WARNING,
            "no state progress was observed in consecutive steps",
            (f"unchanged_steps={window_size}", f"actions={action_ids}"),
            recent[-1].scene_id,
        )

    # {
    #   責務: [
    #     _detect_outcome_mismatch: 既知の実際のOutcomeと期待Outcomeの食い違いを判定する
    #   ]
    #   処理: [
    #     1: 期待値があり、実際値が不明でない場合だけ比較する
    #     2: 成功と失敗が逆転した場合はcritical、それ以外はwarningにする
    #   ]
    #   引数: [
    #     sample: 比較対象の期待値とOutcomeEvent
    #   ]
    #   戻り値: [
    #     event: 結果不一致イベント。該当しない場合はNone
    #   ]
    # }
    def _detect_outcome_mismatch(self, sample: SelfCheckSample) -> SelfCheckEvent | None:
        expected = sample.expected_outcome
        actual = sample.actual_outcome.status
        # Unknownは期待との差を確定できないため、誤った不一致イベントを作らない。
        if (
            expected is None
            or expected == actual
            or actual == "unknown"
            or expected == "unknown"
            or sample.actual_outcome.confidence < self.config.minimum_outcome_confidence
        ):
            return None
        terminal_results = {expected, actual}
        severity = (
            SelfCheckSeverity.CRITICAL
            if terminal_results == {"success", "failure"}
            else SelfCheckSeverity.WARNING
        )
        return self._create_event(
            SelfCheckCategory.OUTCOME_MISMATCH,
            severity,
            "actual outcome differs from the expected outcome",
            (
                f"expected={expected}",
                f"actual={actual}",
                f"action_id={sample.actual_outcome.action_id}",
            ),
            sample.scene_id,
        )

    # {
    #   責務: [
    #     _detect_scene_duration: Scene時間が汎用上限を超えたか判定する
    #   ]
    #   処理: [
    #     1: stepから経過時間とScene識別子を取得する
    #     2: 上限を超えた場合に測定値と設定値を記録する
    #   ]
    #   引数: [
    #     sample: Scene識別子と経過秒数を持つstep
    #   ]
    #   戻り値: [
    #     event: Scene時間異常イベント。超過なしの場合はNone
    #   ]
    # }
    def _detect_scene_duration(self, sample: SelfCheckSample) -> SelfCheckEvent | None:
        duration_limit = self.config.scene_duration_limit_seconds
        if sample.scene_elapsed_seconds <= duration_limit:
            return None
        return self._create_event(
            SelfCheckCategory.SCENE_DURATION,
            SelfCheckSeverity.WARNING,
            "scene duration exceeded the configured generic limit",
            (
                f"elapsed_seconds={sample.scene_elapsed_seconds:.1f}",
                f"limit_seconds={duration_limit:.1f}",
            ),
            sample.scene_id,
        )

    # {
    #   責務: [
    #     _detect_evaluator_disagreement: 信頼度を満たす正負の評価器Evidenceが対立するか調べる
    #   ]
    #   処理: [
    #     1: 最低weightとscoreしきい値を満たす正負Evidenceを分ける
    #     2: 両側が存在する場合に評価器名・scoreを根拠として記録する
    #   ]
    #   引数: [
    #     sample: 評価器Evidenceを含むstep
    #   ]
    #   戻り値: [
    #     event: 評価器対立イベント。対立なしの場合はNone
    #   ]
    # }
    def _detect_evaluator_disagreement(self, sample: SelfCheckSample) -> SelfCheckEvent | None:
        threshold = self.config.evaluator_disagreement_threshold
        evidence_weight_floor = self.config.minimum_evaluator_weight
        # confidenceとreliabilityの積が低いEvidenceは、対立判定に使わない。
        confident_evidence = tuple(
            evidence
            for evidence in sample.evaluator_evidence
            if evidence.confidence * evidence.reliability >= evidence_weight_floor
        )
        positive = tuple(evidence for evidence in confident_evidence if evidence.score >= threshold)
        negative = tuple(evidence for evidence in confident_evidence if evidence.score <= -threshold)
        if not positive or not negative:
            return None

        evidence_summary = tuple(
            f"{evidence.evaluator}={evidence.score:.3f}"
            for evidence in (*positive, *negative)
        )
        return self._create_event(
            SelfCheckCategory.EVALUATOR_DISAGREEMENT,
            SelfCheckSeverity.WARNING,
            "confident evaluators disagree about the current action",
            evidence_summary,
            sample.scene_id,
        )

    # {
    #   責務: [
    #     _create_event: 検出根拠にMonitorのstep位置とScene識別子を付ける
    #   ]
    #   処理: [
    #     1: 検出項目、重要度、説明をイベント契約へまとめる
    #     2: 現在のstep数とEvidenceを保持して返す
    #   ]
    #   引数: [
    #     category: 検出項目
    #     severity: 判定した重要度
    #     reason: 検出理由
    #     evidence: 再確認用の値
    #     scene_id: 検出したScene識別子
    #   ]
    #   戻り値: [
    #     event: 検出内容を保持したSelfCheckEvent
    #   ]
    # }
    def _create_event(
        self,
        category: SelfCheckCategory,
        severity: SelfCheckSeverity,
        reason: str,
        evidence: tuple[str, ...],
        scene_id: str,
    ) -> SelfCheckEvent:
        return SelfCheckEvent(category, severity, self._step_count, scene_id, reason, evidence)
