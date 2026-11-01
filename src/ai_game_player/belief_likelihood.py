"""相関した認識証拠を一つの観測尤度として渡す、不変のゲーム非依存契約。"""

from dataclasses import dataclass
from math import isfinite

from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
)
from ai_game_player.belief_validation import belief_id, belief_integer, belief_number


# {
#   責務: [BeliefActionRef: 実行元が確認した直前操作と仮説の世代を照合する]
#   フィールド: [action_id: 遷移モデルへ渡す許可操作ID, receipt_ref: 実行記録の参照,
#     prior_snapshot_id: 操作判断元のsnapshot, action_epoch: 実行世代,
#     source_context: 実行判断元のSession/Scene/Frame/source世代全体,
#     execution_status: executedだけが確定遷移を予測できる。送信不明は推測しない]
# }
@dataclass(frozen=True, slots=True)
class BeliefActionRef:
    action_id: str
    receipt_ref: str
    prior_snapshot_id: str
    action_epoch: int
    source_context: BeliefContext
    execution_status: str = "executed"

    # {
    #   責務: [__post_init__: 実行元が渡す参照の型と有界性を確認する]
    #   処理: [文字列や世代を検証する。参照だけで実行の真偽を証明する型ではない]
    #   引数: [self: trusted runtime adapterが実ExecutionReceiptから投影する値]
    #   戻り値: [なし]
    #   エラー: [ValueError: 参照/世代/statusが契約外]
    # }
    def __post_init__(self) -> None:
        for value in (self.action_id, self.receipt_ref, self.prior_snapshot_id):
            belief_id(value)
        belief_integer(self.action_epoch)
        if (
            not isinstance(self.source_context, BeliefContext)
            or self.prior_snapshot_id != self.source_context.snapshot_id
            or self.action_epoch != self.source_context.action_epoch
        ):
            raise ValueError("belief action reference source context mismatch")
        if self.execution_status not in ("executed", "dispatch_unknown"):
            raise ValueError("belief prediction requires an execution receipt status")


# {
#   責務: [BeliefLikelihoodBatch: 同じ観測から得た複数Recognizer/Outcomeを一つの尤度factorで渡す]
#   フィールド: [context/freshness: 実Frameに結び付く観測範囲と元時刻,
#     evidence_refs: 観測/結果の最大16参照。相関証拠を独立票として再乗算しない,
#     correlation_id: 同じframe/静止画/融合窓の再利用を識別するproducer側の相関ID,
#     state_likelihoods: typed state digestごとの観測尤度/同じ測度の密度。1超も可、None/欠落は未評価,
#     calibration_id: 尤度モデルの外部校正版。Noneなら比較scoreとしてだけ扱う,
#     unknown_likelihood: 未表現world全体の尤度。不明を0や1と捏造しない]
# }
@dataclass(frozen=True, slots=True)
class BeliefLikelihoodBatch:
    context: BeliefContext
    freshness: BeliefFreshness
    evidence_refs: tuple[BeliefEvidenceRef, ...]
    correlation_id: str
    state_likelihoods: tuple[tuple[str, float | None], ...]
    calibration_id: str | None = None
    unknown_likelihood: float | None = None

    # {
    #   責務: [__post_init__: 尤度の形・有限性と観測出所を検証する]
    #   処理: [同一contextの実観測を最低一つ要求し、自由文/画像/任意callbackを保持しない]
    #   引数: [self: Recognizer/Fusion adapterから渡すjoint likelihood vector]
    #   戻り値: [なし]
    #   エラー: [ValueError: scope/相関/尤度/校正の契約外]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.context, BeliefContext) or not isinstance(
            self.freshness, BeliefFreshness
        ):
            raise ValueError("belief likelihood requires typed observation metadata")
        belief_id(self.correlation_id)
        if self.calibration_id is not None:
            belief_id(self.calibration_id)
        if (
            type(self.evidence_refs) is not tuple
            or not 1 <= len(self.evidence_refs) <= 16
        ):
            raise ValueError("belief likelihood evidence exceeds contract")
        seen = set()
        observed = False
        for item in self.evidence_refs:
            if (
                not isinstance(item, BeliefEvidenceRef)
                or item.context != self.context
                or item.kind
                not in (BeliefEvidenceKind.OBSERVATION, BeliefEvidenceKind.OUTCOME)
                or item.captured_at_ns > self.freshness.observed_at_ns
                or item.evidence_id in seen
            ):
                raise ValueError(
                    "belief likelihood evidence scope or identity mismatch"
                )
            seen.add(item.evidence_id)
            observed |= item.kind == BeliefEvidenceKind.OBSERVATION
        if not observed:
            raise ValueError("posterior likelihood requires actual observed evidence")
        if (
            type(self.state_likelihoods) is not tuple
            or len(self.state_likelihoods) > 256
        ):
            raise ValueError("belief likelihood vector exceeds contract")
        digests = set()
        for row in self.state_likelihoods:
            if type(row) is not tuple or len(row) != 2:
                raise ValueError("belief likelihood rows must be immutable pairs")
            digest, likelihood = row
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(value not in "0123456789abcdef" for value in digest)
                or digest in digests
            ):
                raise ValueError(
                    "belief likelihood state digest is invalid or duplicated"
                )
            digests.add(digest)
            if likelihood is not None:
                belief_number(likelihood)
        if self.unknown_likelihood is not None:
            belief_number(self.unknown_likelihood)


# {
#   責務: [BeliefEstimatorPolicy: 一更新の遷移展開と相関履歴を有限に制限する]
#   フィールド: [max_generated_states: 同時保持する予測world数,
#     max_transition_outcomes: 一つのモデルが返せるchance分岐上限,
#     max_expansion_bytes: pendingと保持worldのcanonical payload合計上限。Python heapの実測とは別,
#     max_seen_correlations: 再票加算を防ぐ履歴の上限。満杯なら黙ってevictせず更新を拒否,
#     max_update_ns: 同じclock originで確認する計算deadline]
# }
@dataclass(frozen=True, slots=True)
class BeliefEstimatorPolicy:
    max_generated_states: int = 256
    max_transition_outcomes: int = 256
    max_expansion_bytes: int = 2 * 1024 * 1024
    max_seen_correlations: int = 1024
    max_update_ns: int = 100_000_000

    # {
    #   責務: [__post_init__: メモリとdeadlineの上限宣言を検証する]
    #   処理: [bool/無限値/ゼロ/過大値を拒否する]
    #   引数: [self: estimatorの固定予算]
    #   戻り値: [なし]
    #   エラー: [ValueError: 予算値が契約外]
    # }
    def __post_init__(self) -> None:
        belief_integer(self.max_generated_states, 1, 256)
        belief_integer(self.max_transition_outcomes, 1, 4096)
        belief_integer(self.max_expansion_bytes, 1, 16 * 1024 * 1024)
        belief_integer(self.max_seen_correlations, 1, 65536)
        belief_integer(self.max_update_ns, 1, 60_000_000_000)


# {
#   責務: [checked_likelihood: chance確率を暗黙変換せず有限0..1として扱う]
#   処理: [bool・無限値・範囲外を拒否する]
#   引数: [value: モデルが返した確率]
#   戻り値: [float: 検証済み確率]
#   エラー: [ValueError: 不正な確率]
# }
def checked_likelihood(value: float) -> float:
    """Model probabilityを暗黙変換せず有限0..1として扱う。"""
    if type(value) not in (int, float) or not 0 <= value <= 1 or not isfinite(value):
        raise ValueError("transition probability is outside contract")
    return float(value)
