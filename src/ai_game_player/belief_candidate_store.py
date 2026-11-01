"""分布を置換し、重複・相関・Top-K・byte予算で確信を増やさない候補Store。"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import math
import threading
import time
from typing import Generic

from ai_game_player.belief_contract import BeliefContext, BeliefWeightKind
from ai_game_player.belief_snapshot import BeliefDiagnostics, BeliefSnapshot
from ai_game_player.belief_validation import belief_id, belief_integer, belief_number
from ai_game_player.world_state_hypothesis import StateT, WorldStateHypothesis


# {
#   責務: [BeliefStoreError: 旧Snapshotを変更せず更新を拒否した理由を固定codeで伝える]
#   フィールド: [なし: ValueError本文は入力の自由文を含まないreason code]
# }
class BeliefStoreError(ValueError):
    pass


# {
#   責務: [BeliefStorePolicy: 入力・保存・鮮度・実行時間の上限を宣言する]
#   フィールド: [max_candidates: 保存するK, max_input_candidates: 一更新で読む上限,
#     max_snapshot_bytes/max_state_bytes: canonical保存/単体state上限,
#     max_age_ns/max_frame_span_ns: 証拠TTL/複数Frameの時間幅,
#     max_update_ns: clock_id内で測る更新deadline]
# }
@dataclass(frozen=True, slots=True)
class BeliefStorePolicy:
    max_candidates: int = 32
    max_input_candidates: int = 256
    max_snapshot_bytes: int = 262144
    max_state_bytes: int = 16384
    max_age_ns: int = 1_000_000_000
    max_frame_span_ns: int = 250_000_000
    max_update_ns: int = 100_000_000

    # {
    #   責務: [__post_init__: 資源budgetを有限の区間へ限定する]
    #   処理: [K/input/byte/TTL/deadlineを検査しboolや逆転した上限を拒否する]
    #   引数: [self: 予算宣言]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正な予算]
    # }
    def __post_init__(self) -> None:
        belief_integer(self.max_candidates, 1, 256)
        belief_integer(self.max_input_candidates, self.max_candidates, 4096)
        belief_integer(self.max_snapshot_bytes, 4096, 16 * 1024 * 1024)
        belief_integer(self.max_state_bytes, 1, 65536)
        belief_integer(self.max_age_ns, 1, 3_600_000_000_000)
        belief_integer(self.max_frame_span_ns, 0, self.max_age_ns)
        belief_integer(self.max_update_ns, 1, 60_000_000_000)


# {
#   責務: [BeliefCandidateStore: 一つのversioned候補集合だけを有界に保持する]
#   フィールド: [state_type: Storeが保持する不変game state型, clock_id/_clock: monotonic時計origin,
#     policy: K/byte/入力/TTL/時間の予算, _snapshot: 最新集合のみ。履歴を蓄積しない,
#     _lock: 計算外で短い版CASと参照取得に使うlock]
# }
class BeliefCandidateStore(Generic[StateT]):
    # {
    #   責務: [__init__: 型・時計・資源予算を束ねる]
    #   処理: [worker、入力、学習、Journal IOを開始せず空Storeを構成する]
    #   引数: [state_type: 型付き状態class, clock_id: producerと共有する時計ID,
    #     policy: 有界予算, clock_ns: 同じoriginのmonotonic ns関数]
    #   戻り値: [なし]
    #   エラー: [ValueError: 型/時計/予算が不正]
    # }
    def __init__(
        self,
        state_type: type[StateT],
        *,
        clock_id: str,
        policy: BeliefStorePolicy | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            not isinstance(state_type, type)
            or not callable(clock_ns)
            or (policy is not None and not isinstance(policy, BeliefStorePolicy))
        ):
            raise ValueError("belief store requires a state type, clock and policy")
        self.state_type = state_type
        self.clock_id = belief_id(clock_id)
        self.policy = policy or BeliefStorePolicy()
        self._clock = clock_ns
        self._snapshot: BeliefSnapshot[StateT] | None = None
        self._lock = threading.Lock()

    # {
    #   責務: [read: 同じcontextかつ未失効のSnapshotだけを返す]
    #   処理: [別Frame/世代/時計とTTL切れをNoneとして明示し古い候補を再利用しない]
    #   引数: [context: 呼出側が要求する現在の観測scope]
    #   戻り値: [BeliefSnapshot|None: 不変集合または利用不能]
    # }
    def read(self, context: BeliefContext) -> BeliefSnapshot[StateT] | None:
        now = belief_integer(self._clock())
        with self._lock:
            snapshot = self._snapshot
        if (
            snapshot is None
            or context != snapshot.context
            or not snapshot.created_at_ns <= now < snapshot.expires_at_ns
        ):
            return None
        return snapshot

    # {
    #   責務: [reset: scene/session/source/clock境界の候補を明示的に破棄する]
    #   処理: [同じStore時計のcontextでempty unknownへ置換し、以前の候補履歴を残さない]
    #   引数: [context: 新しい実行scope]
    #   戻り値: [BeliefSnapshot: 候補なし・unknown質量1の新しい版]
    #   エラー: [BeliefStoreError: 異なるclock origin]
    # }
    def reset(self, context: BeliefContext) -> BeliefSnapshot[StateT]:
        self._check_context(context)
        now = belief_integer(self._clock())
        with self._lock:
            version = (
                1
                if self._snapshot is None
                else self._snapshot.hypothesis_set_version + 1
            )
            snapshot = BeliefSnapshot(
                version,
                context,
                (),
                BeliefWeightKind.UNKNOWN,
                1.0,
                now,
                now + self.policy.max_age_ns,
                "scope_reset",
                BeliefDiagnostics(),
            )
            if len(snapshot.to_bytes()) > self.policy.max_snapshot_bytes:
                raise BeliefStoreError("snapshot_metadata_exceeds_byte_budget")
            self._snapshot = snapshot
            return snapshot

    # {
    #   責務: [publish: 一つの入力分布を検証し有界Snapshotへ原子的に置換する]
    #   処理: [同ID再送を除去し、同worldは最大重みだけ保持して捨てた質量をunknownへ残す。
    #     旧Snapshotの票は加算せず、合法な排他経路の確率合算は#564が入力前に行う]
    #   引数: [context: 分布全体のscope, candidates: 最大input件数の候補列,
    #     unknown_mass: 入力で明示した確率unknownの下限, update_reason: 固定更新理由,
    #     deadline_ns: 同じclock originの任意期限, expected_version: 計算元priorの版。空Storeは0]
    #   戻り値: [BeliefSnapshot: Top-K/byte/TTL後も残余を保持した集合]
    #   エラー: [BeliefStoreError/ValueError: 混在・不正・過去・deadline・競合。前版は変更しない]
    # }
    def publish(
        self,
        context: BeliefContext,
        candidates: Iterable[WorldStateHypothesis[StateT]],
        *,
        unknown_mass: float = 0.0,
        update_reason: str = "observation_candidates",
        deadline_ns: int | None = None,
        expected_version: int | None = None,
    ) -> BeliefSnapshot[StateT]:
        self._check_context(context)
        unknown_mass = belief_number(unknown_mass, 1.0)
        belief_id(update_reason)
        started = belief_integer(self._clock())
        deadline = started + self.policy.max_update_ns
        if deadline_ns is not None:
            deadline = min(deadline, belief_integer(deadline_ns))
        with self._lock:
            previous = self._snapshot
        if expected_version is not None:
            belief_integer(expected_version)
            actual_version = 0 if previous is None else previous.hypothesis_set_version
            if actual_version != expected_version:
                raise BeliefStoreError("stale_expected_version")
        self._check_advance(previous, context)
        if previous is not None and started < previous.created_at_ns:
            raise BeliefStoreError("clock_regressed")
        unique, input_count, duplicate_ids = self._collect(
            context, candidates, deadline
        )
        kinds = {
            (
                item.weight.kind,
                item.weight.calibration_id,
                item.origin,
                item.state_ref.state_schema_id,
            )
            for item in unique.values()
        }
        if len(kinds) > 1:
            raise BeliefStoreError("mixed_weight_origin_or_state_schema")
        kind = next(iter(kinds))[0] if kinds else BeliefWeightKind.UNKNOWN
        if (
            kind == BeliefWeightKind.CALIBRATED_PROBABILITY
            and math.fsum(item.weight.value for item in unique.values()) + unknown_mass
            > 1.0 + 1e-12
        ):
            raise BeliefStoreError("input_probability_mass_exceeds_one")
        worlds: dict[str, WorldStateHypothesis[StateT]] = {}
        duplicate_worlds = 0
        evidence_registry = {}
        for item in unique.values():
            self._check_deadline(deadline)
            for evidence in item.evidence_refs:
                if (
                    evidence.evidence_id in evidence_registry
                    and evidence_registry[evidence.evidence_id] != evidence
                ):
                    raise BeliefStoreError("conflicting_evidence_identity")
                evidence_registry[evidence.evidence_id] = evidence
            existing = worlds.get(item.state_ref.digest)
            if existing is not None:
                duplicate_worlds += 1
                if (-existing.weight.value, existing.hypothesis_id) <= (
                    -item.weight.value,
                    item.hypothesis_id,
                ):
                    continue
            worlds[item.state_ref.digest] = item
        now = belief_integer(self._clock())
        fresh = []
        for item in worlds.values():
            if item.freshness.observed_at_ns > now or any(
                evidence.captured_at_ns > now for evidence in item.evidence_refs
            ):
                raise BeliefStoreError("future_evidence_timestamp")
            if self._is_fresh(item, now):
                fresh.append(item)
        fresh.sort(
            key=lambda item: (
                -item.weight.value,
                item.state_ref.digest,
                item.hypothesis_id,
            )
        )
        retained = tuple(item for item in fresh if item.weight.value > 0)[
            : self.policy.max_candidates
        ]
        version = 1 if previous is None else previous.hypothesis_set_version + 1
        while True:
            self._check_deadline(deadline)
            now = belief_integer(self._clock())
            if any(not self._is_fresh(item, now) for item in retained):
                retained = tuple(item for item in retained if self._is_fresh(item, now))
            snapshot = self._make_snapshot(
                version,
                context,
                retained,
                kind,
                now,
                started,
                update_reason,
                input_count,
                duplicate_ids,
                duplicate_worlds,
                len(unique),
            )
            encoded_size = len(snapshot.to_bytes())
            elapsed = belief_integer(self._clock()) - started
            snapshot = replace(
                snapshot,
                diagnostics=replace(snapshot.diagnostics, update_latency_ns=elapsed),
            )
            if (
                max(encoded_size, len(snapshot.to_bytes()))
                <= self.policy.max_snapshot_bytes
            ):
                break
            if not retained:
                raise BeliefStoreError("snapshot_metadata_exceeds_byte_budget")
            retained = retained[:-1]
        with self._lock:
            self._check_deadline(deadline)
            if self._snapshot is not previous:
                raise BeliefStoreError("concurrent_snapshot_update")
            self._snapshot = snapshot
        return snapshot

    # {
    #   責務: [_collect: 有限入力を検証し同じhypothesis IDの厳密再送だけを除去する]
    #   処理: [上限+1件で打ち切り、同ID異内容や型/scope/単体byte違反を拒否する]
    #   引数: [context: 期待scope, candidates: 入力列, deadline: 更新期限]
    #   戻り値: [tuple: ID別候補、入力数、再送数]
    #   エラー: [BeliefStoreError: 入力上限・矛盾・deadline]
    # }
    def _collect(
        self,
        context: BeliefContext,
        candidates: Iterable[WorldStateHypothesis[StateT]],
        deadline: int,
    ) -> tuple[dict[str, WorldStateHypothesis[StateT]], int, int]:
        result = {}
        count = duplicates = 0
        for count, item in enumerate(candidates, 1):
            self._check_deadline(deadline)
            if count > self.policy.max_input_candidates:
                raise BeliefStoreError("input_candidate_limit")
            if not isinstance(item, WorldStateHypothesis) or item.context != context:
                raise BeliefStoreError("candidate_context_mismatch")
            if item.state_ref.state_type is not self.state_type:
                raise BeliefStoreError("candidate_state_type_mismatch")
            if len(item.state_ref.canonical_bytes) > self.policy.max_state_bytes:
                raise BeliefStoreError("candidate_state_byte_limit")
            if item.hypothesis_id in result:
                if result[item.hypothesis_id] != item:
                    raise BeliefStoreError("conflicting_hypothesis_identity")
                duplicates += 1
            else:
                result[item.hypothesis_id] = item
        return result, count, duplicates

    # {
    #   責務: [_make_snapshot: 切詰め後の質量を正規化で消さずSnapshotへ固定する]
    #   処理: [確率の残余は1-known、score/emptyはunknown1。証拠は相関group単位で数える]
    #   引数: [version/context/retained/kind: 候補契約, now/started: 同じ時計の更新時刻,
    #     reason: 更新理由, input_count/duplicate_ids/duplicate_worlds/peak: 有界診断]
    #   戻り値: [BeliefSnapshot: byte予算検査前の候補集合]
    # }
    def _make_snapshot(
        self,
        version: int,
        context: BeliefContext,
        retained: tuple[WorldStateHypothesis[StateT], ...],
        kind: BeliefWeightKind,
        now: int,
        started: int,
        reason: str,
        input_count: int,
        duplicate_ids: int,
        duplicate_worlds: int,
        peak: int,
    ) -> BeliefSnapshot[StateT]:
        actual_kind = kind if retained else BeliefWeightKind.UNKNOWN
        unknown = (
            max(0.0, 1.0 - math.fsum(item.weight.value for item in retained))
            if actual_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
            else 1.0
        )
        expires = min(
            [now + self.policy.max_age_ns]
            + [item.freshness.expires_at_ns for item in retained]
            + [
                min(evidence.captured_at_ns for evidence in item.evidence_refs)
                + self.policy.max_age_ns
                for item in retained
            ]
        )
        refs = {
            evidence.evidence_id: evidence
            for item in retained
            for evidence in item.evidence_refs
        }
        groups = {
            (evidence.context.snapshot_id, evidence.correlation_id)
            for evidence in refs.values()
        }
        diagnostics = BeliefDiagnostics(
            input_count,
            peak,
            duplicate_ids,
            duplicate_worlds,
            input_count - duplicate_ids - duplicate_worlds - len(retained),
            len(groups),
            len(refs),
            now - started,
        )
        return BeliefSnapshot(
            version,
            context,
            retained,
            actual_kind,
            unknown,
            now,
            expires,
            reason,
            diagnostics,
        )

    # {
    #   責務: [_is_fresh: 候補と全証拠の元時刻から鮮度を評価する]
    #   処理: [候補の期限だけを更新して古い証拠を新鮮に見せることを防ぐ]
    #   引数: [item: 候補, now: 同じclockの現在ns]
    #   戻り値: [bool: TTL・証拠age・frame spanを満たす場合True]
    # }
    def _is_fresh(self, item: WorldStateHypothesis[StateT], now: int) -> bool:
        times = [evidence.captured_at_ns for evidence in item.evidence_refs]
        return (
            item.freshness.observed_at_ns <= now < item.freshness.expires_at_ns
            and item.freshness.expires_at_ns - item.freshness.observed_at_ns
            <= self.policy.max_age_ns
            and now - min(times) < self.policy.max_age_ns
            and max(times) - min(times) <= self.policy.max_frame_span_ns
        )

    # {
    #   責務: [_check_context: Storeとproducerのclock originを照合する]
    #   処理: [別processのmonotonic数値を同じ時刻として比較しない]
    #   引数: [context: 更新scope]
    #   戻り値: [なし]
    #   エラー: [BeliefStoreError: 型またはclock mismatch]
    # }
    def _check_context(self, context: BeliefContext) -> None:
        if not isinstance(context, BeliefContext) or context.clock_id != self.clock_id:
            raise BeliefStoreError("clock_origin_mismatch")

    # {
    #   責務: [_check_advance: 同じrun内でも過去Frame/手番/実行世代を再利用しない]
    #   処理: [run境界にはresetを要求し、同じFrame番号に違うSnapshotを付ける入力を拒否する]
    #   引数: [previous: 前版, context: 新しい分布scope]
    #   戻り値: [なし]
    #   エラー: [BeliefStoreError: reset不足または過去/矛盾したscope]
    # }
    def _check_advance(
        self, previous: BeliefSnapshot[StateT] | None, context: BeliefContext
    ) -> None:
        if previous is None:
            return
        old = previous.context
        if old.run_scope != context.run_scope:
            raise BeliefStoreError("scope_reset_required")
        if (
            context.turn < old.turn
            or context.action_epoch < old.action_epoch
            or context.frame_sequence < old.frame_sequence
        ):
            raise BeliefStoreError("stale_context")
        if context.frame_sequence == old.frame_sequence and context != old:
            raise BeliefStoreError("snapshot_frame_mismatch")
        if (
            context.frame_sequence > old.frame_sequence
            and context.snapshot_id == old.snapshot_id
        ):
            raise BeliefStoreError("snapshot_id_reused_for_new_frame")

    # {
    #   責務: [_check_deadline: 有界更新の期限を同じ時計で検査する]
    #   処理: [長い処理結果を期限後に公開しない]
    #   引数: [deadline: clock_id内の期限ns]
    #   戻り値: [なし]
    #   エラー: [BeliefStoreError: timeout]
    # }
    def _check_deadline(self, deadline: int) -> None:
        if belief_integer(self._clock()) >= deadline:
            raise BeliefStoreError("update_timeout")
