"""入力・観測・校正の出所を保持した、有限候補のBayesian filtering。"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
import math
import threading
import time
from typing import Generic

from ai_game_player.belief_candidate_store import BeliefCandidateStore, BeliefStoreError
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.belief_likelihood import (
    BeliefActionRef,
    BeliefEstimatorPolicy,
    BeliefLikelihoodBatch,
)
from ai_game_player.belief_prediction import expand_prediction
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_validation import belief_id, belief_integer
from ai_game_player.transition_model import TransitionModel
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    StateT,
    WorldStateHypothesis,
)


# {
#   責務: [BeliefEstimateResult: 更新した仮説か、採用しない理由を既存判断へ返す]
#   フィールド: [snapshot: commit成功時だけの不変集合, reason: 固定code,
#     elapsed_ns: このclockでの更新時間, transition_branches: 完了した展開の検査済み分岐数,
#     unknown_transition_branches: 容量/不正遷移で不保持の分岐数。未完了の集計はNone]
# }
@dataclass(frozen=True, slots=True)
class BeliefEstimateResult(Generic[StateT]):
    snapshot: BeliefSnapshot[StateT] | None
    reason: str
    elapsed_ns: int
    transition_branches: int | None = None
    unknown_transition_branches: int | None = None

    # {
    #   責務: [reobserve_requested: 更新不能/低coverageを明示して呼出側のabstainを可能にする]
    #   処理: [snapshot不在またはStoreの再観測条件を返し、Actionは決めない]
    #   引数: [self: 更新結果]
    #   戻り値: [bool: 再観測を検討すべき場合True]
    # }
    @property
    def reobserve_requested(self) -> bool:
        return self.snapshot is None or self.snapshot.reobserve_requested


# {
#   責務: [BayesianBeliefEstimator: predictと新観測の一回の尤度更新を分離する]
#   フィールド: [store: typed/bounded集合, policy: 遷移と相関履歴予算,
#     _clock: Storeと同じmonotonic origin, _lock: estimator内の更新直列化,
#     _scope/_correlations/_frames/_receipts: 証拠/実行参照の有限履歴。容量超過で黙って忘却しない]
# }
class BayesianBeliefEstimator(Generic[StateT]):
    # {
    #   責務: [__init__: 型付きStoreと有限予算を共有する]
    #   処理: [worker/IO/入力を開始せず、初期化待ちのfilterを作る]
    #   引数: [store: このfilterが更新するStore, policy: 予算, clock_ns: 同originの時計]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        store: BeliefCandidateStore[StateT],
        *,
        policy: BeliefEstimatorPolicy | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            not isinstance(store, BeliefCandidateStore)
            or not callable(clock_ns)
            or policy is not None
            and not isinstance(policy, BeliefEstimatorPolicy)
        ):
            raise ValueError("belief estimator requires typed store, policy and clock")
        self.store = store
        self.policy = policy or BeliefEstimatorPolicy()
        self._clock = clock_ns
        self._lock = threading.RLock()
        self._scope: tuple[object, ...] | None = None
        self._correlations: set[str] = set()
        self._frames: set[str] = set()
        self._receipts: set[str] = set()

    # {
    #   責務: [initialize: 遷移モデル無しでも観測仮説から開始する]
    #   処理: [既存Storeの検証/TopK/unknown保持へ委譲し、元観測を再利用しないよう登録する]
    #   引数: [context: 同じ実観測, candidates: Recognizer由来の不変仮説, unknown_mass: 未表現質量]
    #   戻り値: [BeliefEstimateResult: 成功集合または採用しない理由]
    # }
    def initialize(
        self,
        context: BeliefContext,
        candidates: Iterable[WorldStateHypothesis[StateT]],
        *,
        unknown_mass: float = 0.0,
    ) -> BeliefEstimateResult[StateT]:
        with self._lock:
            started = self._now()
            if not isinstance(context, BeliefContext):
                return self._failure("invalid_observation_context", started)
            if self._scope is not None and self._scope != context.run_scope:
                return self._failure("scope_reset_required", started)
            try:
                values = tuple(
                    islice(candidates, self.store.policy.max_input_candidates + 1)
                )
                if len(values) > self.store.policy.max_input_candidates or any(
                    not isinstance(item, WorldStateHypothesis)
                    or item.origin != HypothesisOrigin.OBSERVATION_HYPOTHESIS
                    for item in values
                ):
                    return self._failure("invalid_initial_observation", started)
                correlations = {
                    e.correlation_id for item in values for e in item.evidence_refs
                }
                if (
                    len(self._correlations | correlations)
                    > self.policy.max_seen_correlations
                    or len(self._frames | set(context.frame_ids))
                    > 16 * self.policy.max_seen_correlations
                ):
                    return self._failure("correlation_history_capacity", started)
                snapshot = self.store.publish(
                    context,
                    values,
                    unknown_mass=unknown_mass,
                    update_reason="observation_initialized",
                    deadline_ns=started + self.policy.max_update_ns,
                )
            except TimeoutError:
                return self._failure("initialization_deadline", started)
            except BeliefStoreError as error:
                return self._failure(
                    "initialization_deadline"
                    if str(error) == "update_timeout"
                    else "invalid_initial_observation",
                    started,
                )
            except (ValueError, TypeError, OverflowError):
                return self._failure("invalid_initial_observation", started)
            except Exception:
                return self._failure("observation_source_failed", started)
            self._scope = context.run_scope
            self._remember(context, correlations)
            return self._success(snapshot, "initialized", started)

    # {
    #   責務: [reset: Scene/Session/source変更を明示し過去の票を引き継がない]
    #   処理: [Storeをunknownへ置換してから、相関履歴を新scopeへ初期化する]
    #   引数: [context: 新しい実行scope]
    #   戻り値: [BeliefSnapshot: 空候補・unknown質量1]
    # }
    def reset(self, context: BeliefContext) -> BeliefSnapshot[StateT]:
        with self._lock:
            snapshot = self.store.reset(context)
            if self._scope != context.run_scope:
                self._correlations.clear()
                self._frames.clear()
                self._receipts.clear()
            self._scope = context.run_scope
            return snapshot

    # {
    #   責務: [predict: 実行receiptに対応する状態分布をchance遷移で予測する]
    #   処理: [未観測worldへ確率を伝播し、同worldの排他経路を合算する。
    #     分岐/状態容量超過や不正遷移の質量はunknownへ残し、実観測とは呼ばない]
    #   引数: [prior: Storeの現在版, action: trusted adapterの実行参照,
    #     model: 任意登録のpure model。Noneなら未接続, model_id/model_version: 出所]
    #   戻り値: [BeliefEstimateResult: 不変predictionまたはfallback]
    # }
    def predict(
        self,
        prior: BeliefSnapshot[StateT],
        action: BeliefActionRef,
        model: TransitionModel[StateT] | None,
        *,
        model_id: str,
        model_version: str,
    ) -> BeliefEstimateResult[StateT]:
        with self._lock:
            started = self._now()
            deadline = started + self.policy.max_update_ns
            invalid = self._check_prior(prior, started)
            if invalid:
                return self._failure(invalid, started)
            if model is None:
                return self._failure("transition_unavailable", started)
            if (
                not isinstance(action, BeliefActionRef)
                or action.execution_status != "executed"
                or action.action_epoch != prior.context.action_epoch
                or action.prior_snapshot_id != prior.context.snapshot_id
                or action.source_context != prior.context
            ):
                return self._failure("action_receipt_mismatch_or_unknown", started)
            if action.receipt_ref in self._receipts:
                return self._failure("action_receipt_already_predicted", started)
            if len(self._receipts) >= self.policy.max_seen_correlations:
                return self._failure("receipt_history_capacity", started)
            try:
                belief_id(model_id)
                belief_id(model_version)
                candidates, branches, dropped = expand_prediction(
                    prior,
                    action,
                    model,
                    model_id,
                    model_version,
                    self.policy,
                    self.store.policy.max_input_candidates,
                    self.store.policy.max_state_bytes,
                    lambda: self._check_deadline(deadline),
                )
                self._check_deadline(deadline)
                snapshot = self.store.publish(
                    prior.context,
                    candidates,
                    unknown_mass=prior.residual_unknown_mass,
                    update_reason="action_prediction",
                    deadline_ns=deadline,
                    expected_version=prior.hypothesis_set_version,
                )
            except TimeoutError:
                return self._failure("prediction_deadline", started)
            except BeliefStoreError as error:
                return self._failure(
                    "prediction_deadline"
                    if str(error) == "update_timeout"
                    else "invalid_transition_or_stale_prior",
                    started,
                )
            except (ValueError, TypeError, OverflowError):
                return self._failure("invalid_transition_or_stale_prior", started)
            except Exception:
                return self._failure("transition_failed", started)
            self._receipts.add(action.receipt_ref)
            return self._success(snapshot, "predicted", started, branches, dropped)

    # {
    #   責務: [observe: 一つのjoint尤度factorでposteriorを計算する]
    #   処理: [同frame/相関IDの再投入を拒否し、確率が定義できる時だけBayes正規化する。
    #     未校正/欠落尤度/未知world尤度不明では比較順位とunknown1へfallbackする]
    #   引数: [prior: 現在のpredictionまたは観測仮説, batch: 実Frameのjoint likelihood]
    #   戻り値: [BeliefEstimateResult: posterior仮説。全矛盾時は空unknown]
    # }
    def observe(
        self,
        prior: BeliefSnapshot[StateT],
        batch: BeliefLikelihoodBatch,
    ) -> BeliefEstimateResult[StateT]:
        with self._lock:
            started = self._now()
            deadline = started + self.policy.max_update_ns
            invalid = self._check_prior(prior, started)
            if invalid:
                return self._failure(invalid, started)
            if not isinstance(batch, BeliefLikelihoodBatch):
                return self._failure("invalid_likelihood_batch", started)
            invalid = self._check_observation(prior, batch, started)
            if invalid:
                return self._failure(invalid, started)
            try:
                candidates, unknown, reason = self._posterior(prior, batch, deadline)
                self._check_deadline(deadline)
                snapshot = self.store.publish(
                    batch.context,
                    candidates,
                    unknown_mass=unknown,
                    update_reason=reason,
                    deadline_ns=deadline,
                    expected_version=prior.hypothesis_set_version,
                )
            except TimeoutError:
                return self._failure("posterior_deadline", started)
            except BeliefStoreError as error:
                return self._failure(
                    "posterior_deadline"
                    if str(error) == "update_timeout"
                    else "invalid_posterior_or_stale_prior",
                    started,
                )
            except (ValueError, TypeError, OverflowError):
                return self._failure("invalid_posterior_or_stale_prior", started)
            self._remember(
                batch.context,
                (
                    batch.correlation_id,
                    *(item.correlation_id for item in batch.evidence_refs),
                ),
            )
            return self._success(snapshot, reason, started)

    # {
    #   責務: [_check_prior: 別世代/失効/他writerの版から更新しない]
    #   処理: [Storeの現在参照と型・時計・有効期間を確認する]
    #   引数: [prior: 入力版, now: 同じclockの時刻]
    #   戻り値: [str|None: 拒否理由または利用可能]
    # }
    def _check_prior(self, prior: BeliefSnapshot[StateT], now: int) -> str | None:
        if not isinstance(prior, BeliefSnapshot):
            return "invalid_prior"
        if self._scope != prior.context.run_scope:
            return "scope_reset_required"
        if not prior.created_at_ns <= now < prior.expires_at_ns:
            return "stale_prior"
        if self.store.read(prior.context) is not prior:
            return "stale_prior_version"
        return None

    # {
    #   責務: [_check_observation: 観測scope/鮮度/相関履歴の入力条件を確認する]
    #   処理: [過去frame、旧Action世代、相関再投入、履歴上限を拒否する]
    #   引数: [prior: 元分布, batch: 新観測, now: 元時計の時刻]
    #   戻り値: [str|None: 固定拒否理由]
    # }
    def _check_observation(
        self,
        prior: BeliefSnapshot[StateT],
        batch: BeliefLikelihoodBatch,
        now: int,
    ) -> str | None:
        context = batch.context
        if (
            context.run_scope != prior.context.run_scope
            or context.action_epoch != prior.context.action_epoch
            or context.turn < prior.context.turn
            or context.frame_sequence <= prior.context.frame_sequence
            or context.snapshot_id == prior.context.snapshot_id
        ):
            return "observation_scope_or_order_mismatch"
        if not batch.freshness.observed_at_ns <= now < batch.freshness.expires_at_ns:
            return "stale_or_future_observation"
        correlations = {
            batch.correlation_id,
            *(item.correlation_id for item in batch.evidence_refs),
        }
        if self._frames.intersection(
            context.frame_ids
        ) or self._correlations.intersection(correlations):
            return "correlated_observation_already_used"
        if (
            len(self._correlations | correlations) > self.policy.max_seen_correlations
            or len(self._frames | set(context.frame_ids))
            > 16 * self.policy.max_seen_correlations
        ):
            return "correlation_history_capacity"
        return None

    # {
    #   責務: [_posterior: probabilityとscoreを別々の規則で一度だけ更新する]
    #   処理: [log空間で積を計算しunderflowを避け、unknown尤度も分母へ保持する]
    #   引数: [prior: 元仮説, batch: 一つのjoint likelihood, deadline: 計算期限]
    #   戻り値: [候補列, unknown質量, 更新reason]
    # }
    def _posterior(
        self,
        prior: BeliefSnapshot[StateT],
        batch: BeliefLikelihoodBatch,
        deadline: int,
    ) -> tuple[tuple[WorldStateHypothesis[StateT], ...], float, str]:
        values = dict(batch.state_likelihoods)
        calibrated = (
            prior.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
            and batch.calibration_id is not None
            and all(
                values.get(item.state_ref.digest) is not None
                for item in prior.candidates
            )
            and (
                prior.residual_unknown_mass == 0 or batch.unknown_likelihood is not None
            )
        )
        logs = []
        for item in prior.candidates:
            self._check_deadline(deadline)
            likelihood = values.get(item.state_ref.digest)
            # Missing evidence is neutral in a ranking, never a contradiction.
            likelihood = 1.0 if likelihood is None else likelihood
            logs.append(
                math.log(item.weight.value) + math.log(likelihood)
                if item.weight.value > 0 and likelihood > 0
                else -math.inf
            )
        unknown_log = -math.inf
        if calibrated and prior.residual_unknown_mass > 0 and batch.unknown_likelihood:
            unknown_log = math.log(prior.residual_unknown_mass) + math.log(
                batch.unknown_likelihood
            )
        maximum = max((*logs, unknown_log), default=-math.inf)
        if maximum == -math.inf:
            return (), 1.0, "all_hypotheses_contradicted"
        scaled = [
            math.exp(value - maximum) if value != -math.inf else 0.0 for value in logs
        ]
        unknown_scaled = (
            math.exp(unknown_log - maximum) if unknown_log != -math.inf else 0.0
        )
        denominator = math.fsum((*scaled, unknown_scaled)) if calibrated else 1.0
        kind = (
            BeliefWeightKind.CALIBRATED_PROBABILITY
            if calibrated
            else BeliefWeightKind.RELATIVE_SCORE
        )
        calibration = (
            "bayes."
            + sha256(
                (
                    str(prior.hypothesis_set_version) + ":" + str(batch.calibration_id)
                ).encode()
            ).hexdigest()
            if calibrated
            else None
        )
        candidates = tuple(
            WorldStateHypothesis(
                "posterior." + item.state_ref.digest,
                item.state_ref,
                batch.context,
                BeliefWeight(kind, weight / denominator, calibration),
                HypothesisOrigin.POSTERIOR_HYPOTHESIS,
                batch.evidence_refs,
                batch.freshness,
                prior_action_ref=item.prior_action_ref,
                predicted_outcome_ref=item.predicted_outcome_ref
                or "prior." + str(prior.hypothesis_set_version),
            )
            for item, weight in zip(prior.candidates, scaled)
            if weight > 0
        )
        return (
            candidates,
            unknown_scaled / denominator if calibrated else 1.0,
            "bayesian_posterior" if calibrated else "uncalibrated_posterior_ranking",
        )

    # {
    #   責務: [_remember: 採用済み観測の相関だけを有限履歴へ追加する]
    #   処理: [予測は観測履歴へ追加せず、明示resetまで再票加算を防ぐ]
    #   引数: [context: 採用済みframe, correlations: 同じ入力の相関参照]
    #   戻り値: [なし]
    # }
    def _remember(self, context: BeliefContext, correlations: Iterable[str]) -> None:
        self._frames.update(context.frame_ids)
        self._correlations.update(correlations)

    # {
    #   責務: [_now: 同じmonotonic originの整数時刻を読む]
    #   処理: [clockの型/非負性を検証する]
    #   引数: [なし]
    #   戻り値: [int: ns]
    # }
    def _now(self) -> int:
        return belief_integer(self._clock())

    # {
    #   責務: [_check_deadline: ノード境界で期限切れを拒否する]
    #   処理: [未完了の遅いcallback結果を公開しない。任意callbackを強制終了する機構ではない]
    #   引数: [deadline: 同じ時計の期限ns]
    #   戻り値: [なし]
    #   エラー: [TimeoutError: 計算期限切れ]
    # }
    def _check_deadline(self, deadline: int) -> None:
        if self._now() >= deadline:
            raise TimeoutError("belief update deadline")

    # {
    #   責務: [_failure: 旧Storeを変更せず不採用理由を返す]
    #   処理: [自由文例外を残さず固定理由と計測時間を保持する]
    #   引数: [reason: code, started: 開始時刻]
    #   戻り値: [BeliefEstimateResult: snapshotなし]
    # }
    def _failure(self, reason: str, started: int) -> BeliefEstimateResult[StateT]:
        return BeliefEstimateResult(None, reason, max(0, self._now() - started))

    # {
    #   責務: [_success: commit済み集合と更新計測を返す]
    #   処理: [Storeの不変Snapshotと固定理由だけを公開する]
    #   引数: [snapshot: 採用版, reason: code, started: 開始時刻, branches/dropped: 展開統計]
    #   戻り値: [BeliefEstimateResult: 成功集合]
    # }
    def _success(
        self,
        snapshot: BeliefSnapshot[StateT],
        reason: str,
        started: int,
        branches: int = 0,
        dropped: int = 0,
    ) -> BeliefEstimateResult[StateT]:
        return BeliefEstimateResult(
            snapshot, reason, max(0, self._now() - started), branches, dropped
        )
