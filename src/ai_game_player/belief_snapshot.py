"""有限候補と未表現状態の質量を持つversioned BeliefSnapshot。"""

from dataclasses import dataclass, asdict
import math
from typing import Generic

from ai_game_player.belief_contract import BeliefContext, BeliefWeightKind
from ai_game_player.belief_validation import (
    belief_id,
    belief_integer,
    belief_json,
    belief_number,
)
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    StateT,
    WorldStateHypothesis,
)


BELIEF_SCHEMA_VERSION = 1


# {
#   責務: [BeliefDiagnostics: 更新コストと切詰め・相関の件数を有界metadataへ固定する]
#   フィールド: [input_candidates/peak_candidates: 有界処理件数, duplicate_ids/duplicate_worlds: 再送/同一worldの除去,
#     dropped_candidates: K/byte/stale/zeroによる除外件数, correlation_groups/evidence_refs: 独立票にしない証拠統計,
#     update_latency_ns: 同じclock originで測った更新所要ns]
# }
@dataclass(frozen=True, slots=True)
class BeliefDiagnostics:
    input_candidates: int = 0
    peak_candidates: int = 0
    duplicate_ids: int = 0
    duplicate_worlds: int = 0
    dropped_candidates: int = 0
    correlation_groups: int = 0
    evidence_refs: int = 0
    update_latency_ns: int = 0

    # {
    #   責務: [__post_init__: 診断値を非負整数へ限定する]
    #   処理: [boolや無限値を診断へ通さない]
    #   引数: [self: 診断値]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正な件数/時間]
    # }
    def __post_init__(self) -> None:
        for value in asdict(self).values():
            belief_integer(value)


# {
#   責務: [BeliefSnapshot: 予測を観測事実へ変えず候補とunknownを計画入力へ渡す]
#   フィールド: [hypothesis_set_version: Store内の単調版, context: 同じ観測/実行/時計scope,
#     candidates: 不変の同種候補, weight_kind: 確率/score/unknown, residual_unknown_mass: 未表現質量,
#     created_at_ns/expires_at_ns: clock_idに結び付く有効期間, update_reason: 固定reason ID,
#     diagnostics: 有界コスト統計, schema_version: 機械可読契約版]
# }
@dataclass(frozen=True, slots=True)
class BeliefSnapshot(Generic[StateT]):
    hypothesis_set_version: int
    context: BeliefContext
    candidates: tuple[WorldStateHypothesis[StateT], ...]
    weight_kind: BeliefWeightKind
    residual_unknown_mass: float
    created_at_ns: int
    expires_at_ns: int
    update_reason: str
    diagnostics: BeliefDiagnostics
    schema_version: int = BELIEF_SCHEMA_VERSION

    # {
    #   責務: [__post_init__: Snapshot全体の質量・型・scopeの一貫性を検証する]
    #   処理: [確率質量を保持し、scoreを確率へ正規化しない。重複worldや混在を拒否する]
    #   引数: [self: Snapshot]
    #   戻り値: [なし]
    #   エラー: [ValueError: 版/質量/候補契約の矛盾]
    # }
    def __post_init__(self) -> None:
        belief_integer(self.hypothesis_set_version, 1)
        belief_integer(self.schema_version, 1, 1)
        belief_integer(self.created_at_ns)
        belief_integer(self.expires_at_ns, self.created_at_ns + 1)
        belief_id(self.update_reason)
        belief_number(self.residual_unknown_mass, 1.0)
        if (
            not isinstance(self.context, BeliefContext)
            or not isinstance(self.diagnostics, BeliefDiagnostics)
            or not isinstance(self.weight_kind, BeliefWeightKind)
            or type(self.candidates) is not tuple
            or len(self.candidates) > 256
        ):
            raise ValueError("belief snapshot requires bounded immutable contracts")
        if any(
            not isinstance(item, WorldStateHypothesis)
            or item.context != self.context
            or item.weight.kind != self.weight_kind
            for item in self.candidates
        ):
            raise ValueError("belief snapshot candidate scope or weight kind mismatch")
        if len({item.state_ref.digest for item in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("belief snapshot contains duplicate worlds")
        if len({item.hypothesis_id for item in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("belief snapshot contains duplicate hypothesis IDs")
        if any(
            not item.freshness.observed_at_ns
            <= self.created_at_ns
            < item.freshness.expires_at_ns
            or self.expires_at_ns > item.freshness.expires_at_ns
            for item in self.candidates
        ):
            raise ValueError("belief snapshot exceeds candidate freshness")
        if (
            self.candidates
            and len(
                {
                    (
                        item.state_ref.state_type,
                        item.state_ref.state_schema_id,
                        item.origin,
                        item.weight.calibration_id,
                    )
                    for item in self.candidates
                }
            )
            != 1
        ):
            raise ValueError(
                "belief snapshot mixes state types, origin, or calibration"
            )
        if self.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY:
            if not math.isclose(
                math.fsum(item.weight.value for item in self.candidates)
                + self.residual_unknown_mass,
                1.0,
                abs_tol=1e-12,
            ):
                raise ValueError("belief probability plus unknown mass must equal one")
        elif self.residual_unknown_mass != 1.0:
            raise ValueError(
                "uncalibrated belief must retain full unknown probability mass"
            )
        if not self.candidates and self.weight_kind != BeliefWeightKind.UNKNOWN:
            raise ValueError("empty belief must explicitly be unknown")

    # {
    #   責務: [reobserve_requested: 低coverage/未校正/未観測予測を再観測可能な結果へする]
    #   処理: [残余半分以上、空候補、score、predictionのいずれかで再観測を要求する]
    #   引数: [self: Snapshot]
    #   戻り値: [bool: hostがabstain/reobserveを検討すべき場合True。Action決定はしない]
    # }
    @property
    def reobserve_requested(self) -> bool:
        return (
            self.weight_kind != BeliefWeightKind.CALIBRATED_PROBABILITY
            or self.residual_unknown_mass >= 0.5
            or any(
                item.origin == HypothesisOrigin.PREDICTION for item in self.candidates
            )
        )

    # {
    #   責務: [entropy_lower_bound_bits: 校正済み分布だけに粗いentropy下限を表示する]
    #   処理: [unknownを一つのbucketとして扱う。未校正scoreには定義しない]
    #   引数: [self: Snapshot]
    #   戻り値: [float|None: bits単位の下限、または確率不明]
    # }
    @property
    def entropy_lower_bound_bits(self) -> float | None:
        if self.weight_kind != BeliefWeightKind.CALIBRATED_PROBABILITY:
            return None
        masses = [item.weight.value for item in self.candidates] + [
            self.residual_unknown_mass
        ]
        return -math.fsum(value * math.log2(value) for value in masses if value > 0)

    # {
    #   責務: [ranking_weights: 比較用重みを和1のrank表示へ変換する]
    #   処理: [確率でもscoreでも順位表示だけに使い、元重みとunknown massを書き換えない]
    #   引数: [self: Snapshot]
    #   戻り値: [tuple: candidate順の比較重み。確率の再校正結果ではない]
    # }
    @property
    def ranking_weights(self) -> tuple[float, ...]:
        if not self.candidates:
            return ()
        maximum = max(item.weight.value for item in self.candidates)
        if maximum == 0:
            return tuple(0.0 for _ in self.candidates)
        scaled = tuple(item.weight.value / maximum for item in self.candidates)
        total = math.fsum(scaled)
        return tuple(value / total for value in scaled)

    # {
    #   責務: [to_dict: 版付きの不変仮説集合をJournal/診断用へ投影する]
    #   処理: [出所と残余を保持し、raw画像や任意import手順を含めない]
    #   引数: [self: Snapshot]
    #   戻り値: [dict: JSONへ保存できる独立データ]
    # }
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "hypothesis_set_version": self.hypothesis_set_version,
            "context": self.context.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "weight_kind": self.weight_kind.value,
            "residual_unknown_mass": self.residual_unknown_mass,
            "created_at_ns": self.created_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "update_reason": self.update_reason,
            "diagnostics": asdict(self.diagnostics),
            "reobserve_requested": self.reobserve_requested,
            "entropy_lower_bound_bits": self.entropy_lower_bound_bits,
        }

    # {
    #   責務: [to_bytes: Snapshotの実byte数を評価できるcanonical JSONを生成する]
    #   処理: [版と全出所metadataを同じ決定的encodingで出力する]
    #   引数: [self: Snapshot]
    #   戻り値: [bytes: machine-readable Snapshot]
    # }
    def to_bytes(self) -> bytes:
        return belief_json(self.to_dict())
