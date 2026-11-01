"""実行receiptと明示検証証拠から、raw画面を含まない軽量deltaを作る。"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from uuid import uuid4

from ai_game_player.decision_context import DecisionContext
from ai_game_player.execution_receipt import ExecutionObservation
from ai_game_player.experience import ExperienceEvent, ExperienceProvenance
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_identity import LearningIdentityPolicy


# {
#   責務: [VerifiedEffectSource: アプリが明示登録する因果証拠の種類を制限する]
#   フィールド: [DETERMINISTIC/CALIBRATED/HUMAN: Builderが別途信用度を検査する証拠種別]
# }
class VerifiedEffectSource(str, Enum):
    DETERMINISTIC = "deterministic_action_effect"
    CALIBRATED = "calibrated_action_effect"
    HUMAN = "human_correction"


# {
#   責務: [VerifiedActionEffect: 信頼済み統合境界が作成した検証結果を実操作と後画面へ固定する]
#   フィールド: [snapshot_id/action_id/action_generation: receipt照合値, after_signature: 後画面,
#     outcome: 明示効果, source/confidence: 因果検証器の出所。OCR/pixel/model自己申告から作成しない]
# }
@dataclass(frozen=True, slots=True)
class VerifiedActionEffect:
    snapshot_id: str
    action_id: str
    action_generation: int
    after_signature: str
    outcome: str
    source: VerifiedEffectSource
    confidence: float

    # {
    #   責務: [__post_init__: typed証拠の有限性と既知の因果種別を検査する]
    #   処理: [文字数・enum・確信度・世代を検証する] 引数: [self: 証拠] 戻り値: [なし]
    #   エラー: [ValueError: 不正証拠]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, VerifiedEffectSource)
            or self.outcome not in {"success", "no_effect", "failure"}
            or type(self.action_generation) is not int
            or self.action_generation < 0
            or any(
                not isinstance(v, str) or not v or len(v) > 512
                for v in (self.snapshot_id, self.action_id, self.after_signature)
            )
            or type(self.confidence) not in (int, float)
            or not isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("invalid verified action-effect evidence")


# {
#   責務: [LightExecutionProjector: 有界なframe識別対応とreceiptを結び付ける]
#   フィールド: [scope/identity: 信頼済み設定, _bindings: 直近4判断のopaque key対応,
#     _source_id/_sequence: canonical dedup系列, _last_receipt: 重複観測抑止]
# }
class LightExecutionProjector:
    # {
    #   責務: [__init__: Session隔離した系列を初期化する]
    #   処理: [scope/profile一致を検査し固定容量の対応表を作る]
    #   引数: [scope: Knowledge scope, identity: profileの識別保証] 戻り値: [なし]
    #   エラー: [ValueError: scope不一致]
    # }
    def __init__(self, scope: KnowledgeScope, identity: LearningIdentityPolicy) -> None:
        if scope.profile_id != identity.profile_id:
            raise ValueError("execution projection profile mismatch")
        self.scope = scope
        self.identity = identity
        self._bindings: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._source_id = "session." + uuid4().hex
        self._sequence = 0
        self._last_receipt: str | None = None

    # {
    #   責務: [bind: 実際の判断snapshotに一意な学習keyを固定する]
    #   処理: [最大4判断だけ保持し、曖昧な候補はidentity policyにより除外する]
    #   引数: [context: 実判断材料] 戻り値: [なし]
    # }
    def bind(self, context: DecisionContext) -> None:
        if len(context.snapshot_id) > 512:
            return
        self._bindings[context.snapshot_id] = self.identity.keys(context)
        while len(self._bindings) > 4:
            self._bindings.popitem(last=False)

    # {
    #   責務: [project: receiptの正常送信だけをcanonicalな候補学習入力へ変換する]
    #   処理: [同世代・snapshot・後画面の明示証拠が揃う場合だけ効果を保持し、それ以外はunknown]
    #   引数: [observation: #628不変receiptとOutcomeの対, evidence: 任意の信頼済み因果検証結果]
    #   戻り値: [ExperienceEvent|None: raw label/画面の無い有限delta。未送信/再対応不明はNone]
    # }
    def project(
        self,
        observation: ExecutionObservation,
        evidence: VerifiedActionEffect | None = None,
    ) -> ExperienceEvent | None:
        receipt = observation.receipt
        if receipt.execution_status != "executed" or not receipt.eligible_for_learning:
            return None
        keys = self._bindings.pop(receipt.snapshot_id, {})
        action_key = keys.get(receipt.action_id)
        if action_key is None or self._last_receipt == receipt.snapshot_id:
            return None
        self._last_receipt = receipt.snapshot_id
        self._sequence += 1
        trusted = type(evidence) is VerifiedActionEffect and (
            evidence.snapshot_id == receipt.snapshot_id
            and evidence.action_id == receipt.action_id
            and evidence.action_generation == receipt.action_generation
            and evidence.after_signature == observation.after_signature
        )
        outcome = evidence.outcome if trusted and evidence is not None else "unknown"
        source = (
            evidence.source.value
            if trusted and evidence is not None
            else "execution_observation"
        )
        confidence = evidence.confidence if trusted and evidence is not None else 0.0
        event_id = (
            "effect."
            + sha256((self._source_id + ":" + str(self._sequence)).encode()).hexdigest()
        )
        return ExperienceEvent(
            event_id=event_id,
            event_type="knowledge.action_outcome",
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="light_execution_projection",
            payload={
                "scope": self.scope.to_dict(),
                "source_id": self._source_id,
                "source_sequence": self._sequence,
                "correlation_id": self._source_id,
                "correlation_sequence": self._sequence,
                "action_generation": receipt.action_generation,
                "action_id": action_key,
                "execution_status": "executed",
                "outcome": outcome,
                "evidence_kind": source,
                "verified": trusted,
                "confidence": confidence,
            },
            provenance=ExperienceProvenance(
                source=source, confidence=confidence, evidence_event_id=event_id
            ),
        )
