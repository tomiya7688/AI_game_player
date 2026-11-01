"""paired replayとは別に記録する、数値soft priorだけの軽量検証方針。"""

from dataclasses import dataclass, field
from math import isfinite
from uuid import uuid4
from hashlib import sha256
import json
from typing import Any

from ai_game_player.experience import (
    ArtifactReference,
    ExperienceEvent,
    ExperienceProvenance,
)
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.knowledge_update_sample import KnowledgeUpdateSample


MAX_LIGHT_REPORT_BYTES = 1024 * 1024


# {
#   責務: [light_canonical_events: 再構築に必要な数値canonical根拠を自由文・秘密なしで保持する]
#   処理: [既存sample schemaを全件検証し、raw artifact/actor/model metadataを拒否する。
#     一件16KiB・合計512KiB以内の完全なcanonical eventを独立JSON値へ変換する]
#   引数: [events: 最大64件のtyped事実] 戻り値: [list: Builderへそのまま再投入できるevent本体]
#   エラー: [ValueError: 型・数値契約・privacy・byte予算違反]
# }
def light_canonical_events(events: tuple[ExperienceEvent, ...]) -> list[dict[str, Any]]:
    if type(events) is not tuple or not 1 <= len(events) <= 64:
        raise ValueError("light evidence requires a bounded canonical batch")
    result = []
    size = 0
    for event in events:
        if (
            type(event) is not ExperienceEvent
            or type(event.payload) is not dict
            or type(event.artifacts) is not tuple
            or event.artifacts
            or event.model_id is not None
            or event.model_version is not None
            or (
                event.provenance is not None
                and (
                    type(event.provenance) is not ExperienceProvenance
                    or event.provenance.actor_id is not None
                    or event.provenance.model_id is not None
                    or event.provenance.model_version is not None
                )
            )
            or len(event.timestamp) > 128
        ):
            raise ValueError(
                "light evidence must contain numeric facts without raw metadata"
            )
        KnowledgeUpdateSample.from_event(event)
        value = event.to_dict()
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        size += len(encoded)
        if len(encoded) > 16 * 1024 or size > MAX_LIGHT_REPORT_BYTES // 2:
            raise ValueError("light canonical evidence exceeds byte budget")
        result.append(json.loads(encoded))
    return result


# {
#   責務: [KnowledgeLightValidationPolicy: 複数の検証済み効果と小さいprior差だけを公開候補にする]
#   フィールド: [min_verified_samples: 新しい因果証拠の下限, max_prior_delta: 一回の公開差上限,
#     max_work_ms/max_snapshot_bytes: 検証仕事の計測上限, schema_version: この方針の版]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeLightValidationPolicy:
    min_verified_samples: int = 4
    max_prior_delta: float = 0.25
    max_work_ms: float = 20.0
    max_snapshot_bytes: int = 1024 * 1024
    schema_version: int = 1

    # {
    #   責務: [__post_init__: 緩めて無条件公開する設定を拒否する]
    #   処理: [最低4件・有限差・時間/byte上限を要求する]
    #   引数: [self: 方針] 戻り値: [なし] エラー: [ValueError: 不正予算]
    # }
    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.min_verified_samples) is not int
            or not 4 <= self.min_verified_samples <= 64
            or type(self.max_snapshot_bytes) is not int
            or not 1024 <= self.max_snapshot_bytes <= 1024 * 1024
            or any(
                type(v) not in (int, float) or not isfinite(v) or v <= 0
                for v in (self.max_prior_delta, self.max_work_ms)
            )
            or self.max_prior_delta > 0.25
            or self.max_work_ms > 1000
        ):
            raise ValueError("invalid light knowledge validation policy")

    # {
    #   責務: [assess: 観測された効果集計と固定構造を照合して公開可否を実際に判定する]
    #   処理: [scope、known増分、単調stats、差分、期限・byte、soft-only fieldを比較する]
    #   引数: [before/after: Gateが既存Builderで生成したimmutable値, work_ms: Gate実測]
    #   戻り値: [str: acceptedまたは固定却下理由。プレイ成果改善の証明ではない]
    # }
    def assess(
        self, before: KnowledgeSnapshot, after: KnowledgeSnapshot, work_ms: float
    ) -> str:
        if before.scope != after.scope or after.is_expired():
            return "scope_or_expiry"
        if (
            work_ms > self.max_work_ms
            or len(after.to_bytes()) > self.max_snapshot_bytes
        ):
            return "light_validation_budget"
        old, new = before.to_dict(), after.to_dict()
        for field_name in (
            "weight_hints",
            "threshold_hints",
            "roi_hints",
            "transition_priors",
            "candidate_reliability",
            "expires_at",
            "update_rule",
        ):
            if old[field_name] != new[field_name]:
                return "light_policy_requires_prior_only"
        known = 0
        for action, stats in new["action_outcome_stats"].items():
            previous = old["action_outcome_stats"].get(action, {})
            for name in ("count", "success", "no_effect", "failure", "unknown"):
                if stats[name] < previous.get(name, 0):
                    return "nonmonotonic_statistics"
            known += sum(
                stats[name] - previous.get(name, 0)
                for name in ("success", "no_effect", "failure")
            )
        if known < self.min_verified_samples or after.uncertainty > 0.9:
            return "insufficient_verified_evidence"
        priors = new["candidate_priors"]
        if any(
            abs(value - old["candidate_priors"].get(key, 0.0))
            > self.max_prior_delta + 1e-12
            for key, value in priors.items()
        ):
            return "light_prior_delta_exceeded"
        if not any(
            value != old["candidate_priors"].get(key, 0.0)
            for key, value in priors.items()
        ):
            return "no_soft_prior_change"
        return "accepted"

    # {
    #   責務: [bound_candidate: 統計を改変せずsoft priorの公開差だけをtrust region内へ制限する]
    #   処理: [同scopeの既存priorを中心に±max_prior_deltaへ射影し、版付き更新規則を記録する]
    #   引数: [before/after: 既存版とBuilder実集計版] 戻り値: [KnowledgeSnapshot: 不変のbounded候補]
    #   エラー: [ValueError: scope不一致]
    # }
    def bound_candidate(
        self, before: KnowledgeSnapshot, after: KnowledgeSnapshot
    ) -> KnowledgeSnapshot:
        if before.scope != after.scope:
            raise ValueError("light trust-region scope mismatch")
        old, data = before.to_dict(), after.to_dict()
        for key, value in data["candidate_priors"].items():
            center = old["candidate_priors"].get(key, 0.0)
            data["candidate_priors"][key] = center + max(
                -self.max_prior_delta, min(self.max_prior_delta, value - center)
            )
        data["update_rule_version"] += ".light1"
        data["snapshot_id"] = (
            "knowledge.light."
            + sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:24]
        )
        return KnowledgeSnapshot.from_dict(data)


# {
#   責務: [PreparedKnowledgePublication: Gateが実検証した未公開候補と、Mode CASの短いreceiptを保持する]
#   フィールド: [snapshot: 保存済み候補, generation/champion_sha256/report: 検証対象,
#     nonce: Gate instanceの登録キー, authorized_epoch: 成功したruntime公開の世代。未公開はNone]
# }
@dataclass(slots=True, eq=False)
class PreparedKnowledgePublication:
    snapshot: KnowledgeSnapshot
    generation: int
    champion_sha256: str
    report: ArtifactReference
    nonce: str = field(default_factory=lambda: uuid4().hex)
    authorized_epoch: int | None = None

    # {
    #   責務: [authorize: cached pointer交換成功後にだけMode CASのreceiptを発行する]
    #   処理: [短いmemory代入のみ。IOや評価をしない]
    #   引数: [epoch: 共通Mode gateで確定した世代] 戻り値: [なし]
    #   エラー: [ValueError: 不正または重複したauthorization]
    # }
    def authorize(self, epoch: int) -> None:
        if type(epoch) is not int or epoch < 0 or self.authorized_epoch is not None:
            raise ValueError("invalid publication authorization")
        self.authorized_epoch = epoch
