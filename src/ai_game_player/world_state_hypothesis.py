"""ゲーム非依存の型付き不変状態参照と、観測事実ではない世界状態候補。"""

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Generic, TypeVar

from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
)
from ai_game_player.belief_validation import belief_id, canonical_belief_state


StateT = TypeVar("StateT")


# {
#   責務: [HypothesisOrigin: 観測から作る仮説、未観測の予測、後続のposteriorを区別する]
#   フィールド: [値: どの種別も観測事実そのものにはならない]
# }
class HypothesisOrigin(str, Enum):
    OBSERVATION_HYPOTHESIS = "observation_hypothesis"
    PREDICTION = "prediction"
    POSTERIOR_HYPOTHESIS = "posterior_hypothesis"


# {
#   責務: [TypedStateRef: ゲーム固有の不変stateとcanonical identityを対応付ける]
#   フィールド: [game_id/state_schema_id: adapterが定義する状態契約, state_type/state: 同じ型の不変状態,
#     canonical_bytes: deep検証後に固定するtyped payload。raw画像や秘密情報は状態に含めない,
#     digest: 同じgame/schema/type/stateをdedupするSHA-256]
# }
@dataclass(frozen=True, slots=True)
class TypedStateRef(Generic[StateT]):
    game_id: str
    state_schema_id: str
    state_type: type[StateT]
    state: StateT
    canonical_bytes: bytes = field(init=False, repr=False, compare=False)
    digest: str = field(init=False)

    # {
    #   責務: [__post_init__: ゲーム固有の値を型付きcanonical identityへ固定する]
    #   処理: [型の完全一致、不変性、循環/size/nesting予算を検査してからhashを計算する]
    #   引数: [self: 対象state参照]
    #   戻り値: [なし]
    #   エラー: [ValueError: ID/型/不変性/サイズの違反]
    # }
    def __post_init__(self) -> None:
        belief_id(self.game_id)
        belief_id(self.state_schema_id)
        if (
            not isinstance(self.state_type, type)
            or type(self.state) is not self.state_type
        ):
            raise ValueError("belief state must exactly match its declared type")
        canonical = canonical_belief_state(self.state)
        object.__setattr__(self, "canonical_bytes", canonical)
        digest = hashlib.sha256()
        digest.update(self.game_id.encode("ascii") + b"\0")
        digest.update(self.state_schema_id.encode("ascii") + b"\0")
        digest.update(canonical)
        object.__setattr__(self, "digest", digest.hexdigest())

    # {
    #   責務: [to_dict: 任意importを要求しないtyped state payloadを出力する]
    #   処理: [検証済みcanonical JSONだけを読み戻し、画像や実ゲームOracleへ接続しない]
    #   引数: [self: 対象参照]
    #   戻り値: [dict: versioned Snapshotに埋め込む状態参照]
    # }
    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "state_schema_id": self.state_schema_id,
            "digest": self.digest,
            "typed_value": json.loads(self.canonical_bytes),
        }


# {
#   責務: [WorldStateHypothesis: 一つのplausible worldと重み・鮮度・証拠を束ねる]
#   フィールド: [hypothesis_id: 分布内の候補ID。同じIDの再送は同一内容だけ許可,
#     state_ref: 不変の型付きゲーム状態, context: frame/turn/実行scope, weight: 確率か未校正score,
#     origin: 仮説/予測/posteriorの区別, evidence_refs: 最大16の出所参照, freshness: 元証拠由来の期限,
#     prior_action_ref/predicted_outcome_ref: #564が使う任意参照。実行・成功の事実として扱わない]
# }
@dataclass(frozen=True, slots=True)
class WorldStateHypothesis(Generic[StateT]):
    hypothesis_id: str
    state_ref: TypedStateRef[StateT]
    context: BeliefContext
    weight: BeliefWeight
    origin: HypothesisOrigin
    evidence_refs: tuple[BeliefEvidenceRef, ...]
    freshness: BeliefFreshness
    prior_action_ref: str | None = None
    predicted_outcome_ref: str | None = None

    # {
    #   責務: [__post_init__: 仮説が別scopeや予測Frameを実観測として混ぜないか検証する]
    #   処理: [出所・型・重複証拠・相関groupのsnapshot結合を検査する。証拠を票として加算しない]
    #   引数: [self: 構築中の候補]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正契約、scope混在、予測と実観測の誤表示]
    # }
    def __post_init__(self) -> None:
        belief_id(self.hypothesis_id)
        if (
            not isinstance(self.state_ref, TypedStateRef)
            or not isinstance(self.context, BeliefContext)
            or not isinstance(self.weight, BeliefWeight)
            or not isinstance(self.freshness, BeliefFreshness)
            or not isinstance(self.origin, HypothesisOrigin)
        ):
            raise ValueError("belief hypothesis requires typed immutable contracts")
        if self.state_ref.game_id != self.context.game_id:
            raise ValueError("belief game scope mismatch")
        if (
            type(self.evidence_refs) is not tuple
            or not 1 <= len(self.evidence_refs) <= 16
        ):
            raise ValueError("belief hypothesis evidence must be a bounded tuple")
        seen = {}
        for evidence in self.evidence_refs:
            if (
                not isinstance(evidence, BeliefEvidenceRef)
                or evidence.context != self.context
            ):
                raise ValueError("belief evidence scope mismatch")
            if evidence.captured_at_ns > self.freshness.observed_at_ns:
                raise ValueError(
                    "belief freshness must not precede its latest evidence"
                )
            if evidence.evidence_id in seen and seen[evidence.evidence_id] != evidence:
                raise ValueError("conflicting belief evidence identity")
            seen[evidence.evidence_id] = evidence
        object.__setattr__(
            self, "evidence_refs", tuple(seen[key] for key in sorted(seen))
        )
        has_observation = any(
            item.kind == BeliefEvidenceKind.OBSERVATION for item in self.evidence_refs
        )
        has_prediction = any(
            item.kind == BeliefEvidenceKind.PREDICTION for item in self.evidence_refs
        )
        if self.origin == HypothesisOrigin.PREDICTION:
            if has_observation or not has_prediction:
                raise ValueError("prediction must not claim an observed target state")
        elif not has_observation:
            raise ValueError(
                "observation/posterior hypothesis requires actual observation evidence"
            )
        for reference in (self.prior_action_ref, self.predicted_outcome_ref):
            if reference is not None:
                belief_id(reference)

    # {
    #   責務: [to_dict: 候補と出所をversioned Snapshotへ投影する]
    #   処理: [型付き状態と有界metadataを独立辞書にする。実観測事実というflagは作らない]
    #   引数: [self: 対象候補]
    #   戻り値: [dict: JSON保存可能な仮説]
    # }
    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "state_ref": self.state_ref.to_dict(),
            "context": self.context.to_dict(),
            "weight": {
                "kind": self.weight.kind.value,
                "value": self.weight.value,
                "calibration_id": self.weight.calibration_id,
            },
            "origin": self.origin.value,
            "evidence_refs": [
                {
                    "evidence_id": item.evidence_id,
                    "correlation_id": item.correlation_id,
                    "kind": item.kind.value,
                    "source_id": item.source_id,
                    "source_version": item.source_version,
                    "context": item.context.to_dict(),
                    "captured_at_ns": item.captured_at_ns,
                    "frame_id": item.frame_id,
                }
                for item in self.evidence_refs
            ],
            "freshness": {
                "observed_at_ns": self.freshness.observed_at_ns,
                "expires_at_ns": self.freshness.expires_at_ns,
            },
            "prior_action_ref": self.prior_action_ref,
            "predicted_outcome_ref": self.predicted_outcome_ref,
        }
