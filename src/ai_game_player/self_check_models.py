from dataclasses import dataclass
from enum import Enum
from math import isfinite

from ai_game_player.decision_context import EvaluatorEvidence
from ai_game_player.outcome_models import OutcomeEvent


# {
#   責務: [
#     SelfCheckCategory: deterministicなSelf-Check検出項目を識別する
#   ]
#   フィールド: [
#     REPEATED_CYCLE: 操作と状態の周期的な反復
#     STAGNATION: 連続した進展なし
#     OUTCOME_MISMATCH: 期待結果と実際の結果の不一致
#     SCENE_DURATION: 設定された一般的なScene時間の超過
#     EVALUATOR_DISAGREEMENT: 信頼できる評価器同士の対立
#   ]
# }
class SelfCheckCategory(str, Enum):
    REPEATED_CYCLE = "repeated_cycle"
    STAGNATION = "stagnation"
    OUTCOME_MISMATCH = "outcome_mismatch"
    SCENE_DURATION = "scene_duration_anomaly"
    EVALUATOR_DISAGREEMENT = "evaluator_disagreement"


# {
#   責務: [
#     SelfCheckSeverity: Self-Checkイベントの重要度を表す
#   ]
#   フィールド: [
#     WARNING: 継続時に停滞や判断誤りを招く可能性
#     CRITICAL: 成功・失敗の終端結果が期待と逆
#   ]
# }
class SelfCheckSeverity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


# {
#   責務: [
#     SelfCheckConfig: 汎用的なrolling検出の上限と判定しきい値を保持する
#   ]
#   フィールド: [
#     history_limit: 保持する直近step数
#     minimum_cycle_repetitions: Cycle判定に必要な反復回数
#     maximum_cycle_period: 検出する周期の最大step数
#     stagnation_limit: 停滞と判定する連続step数
#     scene_duration_limit_seconds: Scene時間の共通上限（秒）
#     minimum_outcome_confidence: Outcome判定を確定根拠として使う最低confidence
#     evaluator_disagreement_threshold: 評価器対立の絶対scoreしきい値
#     minimum_evaluator_weight: 評価器対立に使う最低confidence×reliability
#   ]
# }
@dataclass(frozen=True)
class SelfCheckConfig:
    history_limit: int = 20
    minimum_cycle_repetitions: int = 2
    maximum_cycle_period: int = 3
    stagnation_limit: int = 3
    scene_duration_limit_seconds: float = 300.0
    minimum_outcome_confidence: float = 0.5
    evaluator_disagreement_threshold: float = 0.35
    minimum_evaluator_weight: float = 0.5

    # {
    #   責務: [
    #     __post_init__: rolling検出に使う設定値の範囲と整合性を検証する
    #   ]
    #   処理: [
    #     1: step数の下限を検証する
    #     2: 履歴上限がCycle窓と停滞窓の両方を保持できることを確認する
    #     3: 時間と評価器Evidence weightの有限範囲を検証する
    #   ]
    #   引数: [
    #     self: 検証対象の設定
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     設定が範囲外または相互に矛盾する場合はValueErrorを送出する
    #   ]
    # }
    def __post_init__(self) -> None:
        step_limits = (
            self.history_limit,
            self.minimum_cycle_repetitions,
            self.maximum_cycle_period,
            self.stagnation_limit,
        )
        if any(type(limit) is not int for limit in step_limits):
            raise ValueError("history and detection limits must be integers")
        if self.minimum_cycle_repetitions < 2 or self.maximum_cycle_period < 1:
            raise ValueError("cycle repetitions must be at least two and cycle period must be positive")
        if self.stagnation_limit < 2:
            raise ValueError("stagnation limit must be at least two")
        required_history = max(
            self.minimum_cycle_repetitions * self.maximum_cycle_period,
            self.stagnation_limit,
        )
        if self.history_limit < required_history:
            raise ValueError("history limit must contain every configured detection window")
        if not isfinite(self.scene_duration_limit_seconds) or self.scene_duration_limit_seconds <= 0:
            raise ValueError("scene duration limit must be finite and positive")
        if not isfinite(self.minimum_outcome_confidence) or not 0 <= self.minimum_outcome_confidence <= 1:
            raise ValueError("minimum outcome confidence must be between 0 and 1")
        if not isfinite(self.evaluator_disagreement_threshold) or not 0 < self.evaluator_disagreement_threshold <= 1:
            raise ValueError("evaluator disagreement threshold must be between 0 and 1")
        if not isfinite(self.minimum_evaluator_weight) or not 0 <= self.minimum_evaluator_weight <= 1:
            raise ValueError("minimum evaluator weight must be between 0 and 1")


# {
#   責務: [
#     SelfCheckSample: 1つの汎用ゲームstepに関する検出入力を保持する
#   ]
#   フィールド: [
#     actual_outcome: stepに対応するOutcomeEvent
#     state_signature: ゲーム固有schemaに依存しない状態識別子
#     expected_outcome: 任意の期待結果status
#     scene_id: 汎用Scene識別子。既知のゲームpresetを要求しない
#     scene_elapsed_seconds: 現在のScene継続時間
#     evaluator_evidence: 判定に用いた評価器Evidence
#   ]
# }
@dataclass(frozen=True)
class SelfCheckSample:
    actual_outcome: OutcomeEvent
    state_signature: str
    expected_outcome: str | None = None
    scene_id: str = "unknown"
    scene_elapsed_seconds: float = 0.0
    evaluator_evidence: tuple[EvaluatorEvidence, ...] = ()

    # {
    #   責務: [
    #     __post_init__: Self-Check入力の識別子とScene時間を検証する
    #   ]
    #   処理: [
    #     1: 状態・Scene識別子の空値を拒否する
    #     2: 期待結果がOutcomeEventのstatus集合に含まれるか確認する
    #     3: Scene時間が有限かつ非負であることを確認する
    #   ]
    #   引数: [
    #     self: 検証対象のstep情報
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     識別子、期待結果、Scene時間が無効な場合はValueErrorを送出する
    #   ]
    # }
    def __post_init__(self) -> None:
        if not self.state_signature.strip() or not self.scene_id.strip():
            raise ValueError("self-check state and scene identifiers must not be empty")
        if self.expected_outcome is not None and self.expected_outcome not in OutcomeEvent.VALID_STATUSES:
            raise ValueError(f"invalid expected outcome: {self.expected_outcome}")
        if not isfinite(self.scene_elapsed_seconds) or self.scene_elapsed_seconds < 0:
            raise ValueError("scene elapsed seconds must be finite and non-negative")


# {
#   責務: [
#     SelfCheckEvent: 検出結果と根拠を利用者・後続処理へ伝える
#   ]
#   フィールド: [
#     category: 検出項目
#     severity: 検出の重要度
#     step_count: Monitor開始から検出時点までのstep数
#     scene_id: 検出時に観測した汎用Scene識別子
#     reason: 検出理由
#     evidence: 判定を再確認できる値の一覧
#   ]
#   処理: [
#     to_dict: self-check-event/v1形式へ変換する
#   ]
# }
@dataclass(frozen=True)
class SelfCheckEvent:
    category: SelfCheckCategory
    severity: SelfCheckSeverity
    step_count: int
    scene_id: str
    reason: str
    evidence: tuple[str, ...]

    # {
    #   責務: [
    #     __post_init__: 外部へ返すSelf-Checkイベントの完全性を検証する
    #   ]
    #   処理: [
    #     1: step数と識別情報の妥当性を確認する
    #     2: 再確認に使うEvidenceが存在することを確認する
    #   ]
    #   引数: [
    #     self: 検証対象のイベント
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     不完全なイベントの場合はValueErrorを送出する
    #   ]
    # }
    def __post_init__(self) -> None:
        if self.step_count < 1 or not self.scene_id.strip() or not self.reason.strip():
            raise ValueError("self-check event identity and reason must be valid")
        if not self.evidence or any(not item.strip() for item in self.evidence):
            raise ValueError("self-check event evidence must not be empty")

    # {
    #   責務: [
    #     to_dict: イベントを保存・表示用のversioned辞書へ変換する
    #   ]
    #   処理: [
    #     1: Enum値とEvidenceをJSON互換値に変換する
    #     2: schema versionと検出根拠を返す
    #   ]
    #   引数: [
    #     self: 変換対象のイベント
    #   ]
    #   戻り値: [
    #     result: self-check-event/v1の辞書
    #   ]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "self-check-event/v1",
            "category": self.category.value,
            "severity": self.severity.value,
            "step_count": self.step_count,
            "scene_id": self.scene_id,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }
