import threading
from collections import deque
from dataclasses import dataclass
from math import isfinite

from ai_game_player.recovery_models import (
    RecoveryAction,
    RecoveryAttemptResult,
    RecoveryAttemptStatus,
    RecoveryBudget,
    RecoveryPlan,
    RecoveryPolicy,
    RecoveryReason,
    RecoveryState,
)
from ai_game_player.self_check_models import SelfCheckEvent, SelfCheckSeverity


SEEN_EVENT_LIMIT = 64


# {
#   責務: [
#     _RecoveryEpisode: 1つのSelf-Check異常に対するRecoveryの進行をController内部で保持する
#   ]
#   フィールド: [
#     event_ref: 対象イベントの識別子
#     step_count: 対象イベントのSelf-Check step数
#     start_snapshot_id: episode開始時のSnapshot
#     deadline: episodeを打ち切るmonotonic時刻
#     reobserve_used / redecide_used: 発行済みの再観測・再判断回数
#     fresh_snapshot_id: 再観測で得た新しいSnapshot。未取得ならNone
#     redecided_snapshot_ids: 再判断に使用済みのSnapshot集合
#     pending: 結果待ちのplan。無ければNone
#   ]
# }
@dataclass
class _RecoveryEpisode:
    event_ref: str
    step_count: int
    start_snapshot_id: str
    deadline: float
    reobserve_used: int = 0
    redecide_used: int = 0
    fresh_snapshot_id: str | None = None
    redecided_snapshot_ids: frozenset[str] = frozenset()
    pending: RecoveryPlan | None = None


# {
#   責務: [
#     RecoveryController: SelfCheckEventを有限回の再観測・再判断・停止要求へ変換するSession単位の状態機械
#   ]
#   フィールド: [
#     policy: 回数・時間・cooldown・連続失敗の上限
#     _state: 現在のRecovery段階
#     _episode: 進行中のepisode。無ければNone
#     _action_epoch: 最後に受け付けた行動世代
#     _scene_id / _scene_failures: 現在Sceneと同一Sceneの連続episode失敗数
#     _cooldown_until_step: このstep数未満のイベントでは新episodeを開始しない
#     _stop_reason: 自ら停止要求した場合の理由
#     _seen_events: 同じstep数の重複イベント判定用の有限履歴
#     _latest_step: 受け付けた最大のSelf-Check step数。これより古いイベントは履歴から外れても再処理しない
#     _last_now: 最後に受け取ったmonotonic時刻
#   ]
#   処理: [
#     on_event / ack / poll / request_stop / reset で状態を更新し、実入力・モデル推論・callbackを呼ばずにplanを返す
#   ]
# }
class RecoveryController:
    # {
    #   責務: [
    #     __init__: Recovery上限と初期状態を設定する
    #   ]
    #   処理: [
    #     1: policyを保持し、状態をIDLEに初期化する
    #   ]
    #   引数: [
    #     policy: Recovery上限。省略時は既定値
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def __init__(self, policy: RecoveryPolicy | None = None) -> None:
        self.policy = policy or RecoveryPolicy()
        self._lock = threading.Lock()
        self._action_epoch = 0
        self._reset_locked()

    # {
    #   責務: [
    #     state: 現在のRecovery段階を返す
    #   ]
    #   処理: [
    #     1: lock内で状態を読む
    #   ]
    #   引数: [
    #     なし
    #   ]
    #   戻り値: [
    #     RecoveryState: 現在の段階
    #   ]
    # }
    @property
    def state(self) -> RecoveryState:
        with self._lock:
            return self._state

    # {
    #   責務: [
    #     on_event: Self-Checkイベントから次のRecovery行動を決める
    #   ]
    #   処理: [
    #     1: 旧世代・既存Stop・自身の停止要求・Target喪失を最優先で拒否する
    #     2: 既処理より古いstep・重複イベント・Scene変更・CRITICAL・cooldown・結果待ちを判定する
    #     3: 期限内なら再観測→再判断の順で次のplanを発行し、予算切れはepisode失敗にする
    #   ]
    #   引数: [
    #     event: SelfCheckMonitorが返したイベント
    #     snapshot_id: イベント検出時点のSnapshot識別子
    #     action_generation: RunControllerの再arm世代
    #     now_monotonic: 呼出側が注入するmonotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #     target_lost: 操作対象を失っていればTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 次の行動と型付き理由
    #   ]
    #   エラー: [
    #     時刻・世代・Snapshotが無効な場合はValueErrorを送出する
    #   ]
    # }
    def on_event(
        self,
        event: SelfCheckEvent,
        *,
        snapshot_id: str,
        action_generation: int,
        now_monotonic: float,
        stop_requested: bool,
        target_lost: bool = False,
    ) -> RecoveryPlan:
        _require_snapshot(snapshot_id)
        with self._lock:
            self._accept_time_locked(now_monotonic)
            refusal = self._refuse_locked(
                action_generation, stop_requested, target_lost, snapshot_id
            )
            if refusal is not None:
                return refusal
            event_key = (event.scene_id, event.category.value, event.step_count)
            if event.step_count < self._latest_step or event_key in self._seen_events:
                return self._plan_locked(
                    RecoveryAction.ABSTAIN, RecoveryReason.DUPLICATE_EVENT, snapshot_id
                )
            self._seen_events.append(event_key)
            self._latest_step = event.step_count
            if event.scene_id != self._scene_id:
                self._enter_scene_locked(event.scene_id)
            if event.severity is SelfCheckSeverity.CRITICAL:
                self._episode = None
                return self._stop_locked(
                    RecoveryReason.CRITICAL_SELF_CHECK, snapshot_id, event.reason
                )
            episode = self._episode
            if episode is None:
                if event.step_count < self._cooldown_until_step:
                    return self._plan_locked(
                        RecoveryAction.ABSTAIN, RecoveryReason.COOLDOWN, snapshot_id
                    )
                episode = _RecoveryEpisode(
                    event_ref=f"{event.category.value}@{event.step_count}",
                    step_count=event.step_count,
                    start_snapshot_id=snapshot_id,
                    deadline=now_monotonic + self.policy.deadline_seconds,
                )
                self._episode = episode
            elif episode.pending is not None and now_monotonic < episode.deadline:
                return self._plan_locked(
                    RecoveryAction.ABSTAIN, RecoveryReason.PLAN_PENDING, snapshot_id
                )
            episode.pending = None
            return self._advance_locked(now_monotonic, snapshot_id)

    # {
    #   責務: [
    #     ack: 呼出側が実行したRecovery attemptの結果を受け取り、次の行動を決める
    #   ]
    #   処理: [
    #     1: 旧世代・未発行の新世代・既存Stop・結果待ちでないattemptを拒否する（結果では再armしない）
    #     2: 新しいFrameを得た再観測だけを再観測済みとして記録する
    #     3: 開始時とplan基準の両方と異なるFrameで解消すればRESOLVED、そうでなければ予算内で次のplanを発行する
    #   ]
    #   引数: [
    #     result: 実行したattemptの結果
    #     now_monotonic: 呼出側が注入するmonotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 次の行動と型付き理由
    #   ]
    #   エラー: [
    #     時刻が無効な場合はValueErrorを送出する
    #   ]
    # }
    def ack(
        self,
        result: RecoveryAttemptResult,
        *,
        now_monotonic: float,
        stop_requested: bool = False,
    ) -> RecoveryPlan:
        with self._lock:
            self._accept_time_locked(now_monotonic)
            # 再armはSelf-Checkイベント側の世代でだけ受け付ける。結果の世代で停止latchを解除しない。
            if result.action_epoch > self._action_epoch:
                return self._plan_locked(
                    RecoveryAction.ABSTAIN,
                    RecoveryReason.UNEXPECTED_ACK,
                    result.snapshot_id,
                )
            refusal = self._refuse_locked(
                result.action_epoch, stop_requested, False, result.snapshot_id
            )
            if refusal is not None:
                return refusal
            episode = self._episode
            pending = None if episode is None else episode.pending
            if (
                episode is None
                or pending is None
                or pending.action is not result.action
                or pending.attempt != result.attempt
                or pending.action_epoch != result.action_epoch
            ):
                return self._plan_locked(
                    RecoveryAction.ABSTAIN,
                    RecoveryReason.UNEXPECTED_ACK,
                    result.snapshot_id,
                )
            episode.pending = None
            new_frame = result.snapshot_id != pending.snapshot_id
            if result.action is RecoveryAction.REOBSERVE and new_frame:
                episode.fresh_snapshot_id = result.snapshot_id
            if (
                result.action is RecoveryAction.REDECIDE
                and pending.snapshot_id is not None
            ):
                episode.redecided_snapshot_ids = episode.redecided_snapshot_ids | {
                    pending.snapshot_id
                }
            resolved_on_new_frame = (
                result.status is RecoveryAttemptStatus.RESOLVED
                and result.snapshot_id != episode.start_snapshot_id
                and result.snapshot_id != pending.snapshot_id
            )
            if resolved_on_new_frame:
                return self._resolve_locked(result.snapshot_id)
            return self._advance_locked(now_monotonic, result.snapshot_id)

    # {
    #   責務: [
    #     poll: 結果が返らないattemptを期限で打ち切る
    #   ]
    #   処理: [
    #     1: 既存Stopなら停止latchを記録する
    #     2: 結果待ちepisodeが期限切れならepisode失敗として処理する
    #   ]
    #   引数: [
    #     now_monotonic: 呼出側が注入するmonotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan | None: 打ち切った場合のplan。変化が無ければNone
    #   ]
    #   エラー: [
    #     時刻が無効な場合はValueErrorを送出する
    #   ]
    # }
    def poll(
        self, *, now_monotonic: float, stop_requested: bool = False
    ) -> RecoveryPlan | None:
        with self._lock:
            self._accept_time_locked(now_monotonic)
            episode = self._episode
            if stop_requested:
                if self._state is RecoveryState.STOP_REQUESTED and episode is None:
                    return None
                return self._latch_external_stop_locked(
                    None if episode is None else episode.start_snapshot_id
                )
            if (
                episode is None
                or episode.pending is None
                or now_monotonic < episode.deadline
            ):
                return None
            episode.pending = None
            return self._fail_episode_locked(
                RecoveryReason.DEADLINE_EXCEEDED, episode.start_snapshot_id
            )

    # {
    #   責務: [
    #     request_stop: 記録失敗などRecovery外部の理由で停止要求へ昇格する
    #   ]
    #   処理: [
    #     1: episodeを破棄し、再armまで停止要求を保持する
    #   ]
    #   引数: [
    #     reason: 停止要求の型付き理由
    #     snapshot_id: 記録対象のSnapshot。不明ならNone
    #   ]
    #   戻り値: [
    #     RecoveryPlan: REQUEST_STOPのplan
    #   ]
    # }
    def request_stop(
        self, reason: RecoveryReason, snapshot_id: str | None = None
    ) -> RecoveryPlan:
        with self._lock:
            self._episode = None
            return self._stop_locked(reason, snapshot_id, None)

    # {
    #   責務: [
    #     reset: Session開始・終了時にRecovery状態を初期化する
    #   ]
    #   処理: [
    #     1: 行動世代以外の状態を初期値に戻す
    #   ]
    #   引数: [
    #     なし
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    # {
    #   責務: [
    #     _reset_locked: 呼出側がlockを保持した状態で行動世代以外のRecovery状態を初期化する
    #   ]
    #   処理: [
    #     1: 状態・episode・Scene・cooldown・停止理由・重複履歴・最終時刻を初期値へ戻す
    #   ]
    #   引数: [
    #     なし
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def _reset_locked(self) -> None:
        self._state = RecoveryState.IDLE
        self._episode: _RecoveryEpisode | None = None
        self._scene_id: str | None = None
        self._scene_failures = 0
        self._cooldown_until_step = 0
        self._stop_reason: str | None = None
        self._seen_events: deque[tuple[str, str, int]] = deque(maxlen=SEEN_EVENT_LIMIT)
        self._last_now: float | None = None
        self._latest_step = 0

    # {
    #   責務: [
    #     _require_episode_locked: 進行中episodeを前提とする内部処理でepisodeを取得する
    #   ]
    #   処理: [
    #     1: episodeが無ければ内部不整合として例外を送出する
    #   ]
    #   引数: [
    #     なし
    #   ]
    #   戻り値: [
    #     _RecoveryEpisode: 進行中episode
    #   ]
    #   エラー: [
    #     episodeが無い場合はRuntimeErrorを送出する
    #   ]
    # }
    def _require_episode_locked(self) -> _RecoveryEpisode:
        if self._episode is None:
            raise RuntimeError("recovery episode is not active")
        return self._episode

    # {
    #   責務: [
    #     _accept_time_locked: 注入時刻が有限で逆行していないことを確認する
    #   ]
    #   処理: [
    #     1: 非有限・逆行時刻を拒否し、最後の時刻を更新する
    #   ]
    #   引数: [
    #     now_monotonic: monotonic時刻（秒）
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     時刻が無効な場合はValueErrorを送出する
    #   ]
    # }
    def _accept_time_locked(self, now_monotonic: float) -> None:
        if type(now_monotonic) not in (int, float) or not isfinite(now_monotonic):
            raise ValueError("recovery clock must be finite")
        if self._last_now is not None and now_monotonic < self._last_now:
            raise ValueError("recovery clock must not go backwards")
        self._last_now = float(now_monotonic)

    # {
    #   責務: [
    #     _refuse_locked: 旧世代・既存Stop・自身の停止要求・Target喪失を最優先で拒否する
    #   ]
    #   処理: [
    #     1: 旧世代はABSTAIN、新世代は再armとして状態を初期化する
    #     2: 既存Stopはlatchして入力を伴わないABSTAINを返す
    #     3: 自身が停止要求済みなら同じ停止要求を返し、Target喪失はepisodeを破棄する
    #   ]
    #   引数: [
    #     action_generation: 呼出側の行動世代
    #     stop_requested: 既存Stopが要求済みならTrue
    #     target_lost: 操作対象を失っていればTrue
    #     snapshot_id: 記録対象のSnapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan | None: 拒否した場合のplan。続行可能ならNone
    #   ]
    #   エラー: [
    #     世代が負または整数でない場合はValueErrorを送出する
    #   ]
    # }
    def _refuse_locked(
        self,
        action_generation: int,
        stop_requested: bool,
        target_lost: bool,
        snapshot_id: str,
    ) -> RecoveryPlan | None:
        if type(action_generation) is not int or action_generation < 0:
            raise ValueError("action generation must be a non-negative integer")
        if action_generation < self._action_epoch:
            return self._plan_locked(
                RecoveryAction.ABSTAIN, RecoveryReason.STALE_EPOCH, snapshot_id
            )
        if action_generation > self._action_epoch:
            last_now = self._last_now
            self._reset_locked()
            self._last_now = last_now
            self._action_epoch = action_generation
        if stop_requested:
            return self._latch_external_stop_locked(snapshot_id)
        if self._state is RecoveryState.STOP_REQUESTED:
            if self._stop_reason is None:
                return self._plan_locked(
                    RecoveryAction.ABSTAIN, RecoveryReason.STOP_LATCHED, snapshot_id
                )
            return self._plan_locked(
                RecoveryAction.REQUEST_STOP,
                RecoveryReason.STOP_ALREADY_REQUESTED,
                snapshot_id,
                stop_reason=self._stop_reason,
            )
        if target_lost:
            self._episode = None
            self._state = RecoveryState.IDLE
            return self._plan_locked(
                RecoveryAction.ABSTAIN, RecoveryReason.TARGET_LOST, snapshot_id
            )
        return None

    # {
    #   責務: [
    #     _latch_external_stop_locked: 既存Stopを置き換えずに記録し、再armまでRecoveryを止める
    #   ]
    #   処理: [
    #     1: episodeを破棄してSTOP_REQUESTEDへ移る。自身の停止理由は持たない
    #   ]
    #   引数: [
    #     snapshot_id: 記録対象のSnapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan: STOP_LATCHEDのABSTAIN
    #   ]
    # }
    def _latch_external_stop_locked(self, snapshot_id: str | None) -> RecoveryPlan:
        self._episode = None
        self._state = RecoveryState.STOP_REQUESTED
        self._stop_reason = None
        return self._plan_locked(
            RecoveryAction.ABSTAIN, RecoveryReason.STOP_LATCHED, snapshot_id
        )

    # {
    #   責務: [
    #     _enter_scene_locked: Scene変更時に前Sceneのepisode・失敗数・cooldownを破棄する
    #   ]
    #   処理: [
    #     1: Scene識別子を更新し、Scene単位の状態を初期化する
    #   ]
    #   引数: [
    #     scene_id: 新しいScene
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def _enter_scene_locked(self, scene_id: str) -> None:
        self._scene_id = scene_id
        self._scene_failures = 0
        self._cooldown_until_step = 0
        self._episode = None
        self._state = RecoveryState.IDLE

    # {
    #   責務: [
    #     _advance_locked: 期限と残予算に従って再観測・再判断・失敗のいずれかを選ぶ
    #   ]
    #   処理: [
    #     1: 期限切れならepisode失敗にする
    #     2: 未使用の新Frameがあり再判断予算が残れば、そのFrameでREDECIDEを発行する
    #     3: そうでなく再観測予算が残ればREOBSERVEを発行し、どちらも無ければ予算切れにする
    #   ]
    #   引数: [
    #     now_monotonic: monotonic時刻（秒）
    #     snapshot_id: 現在観測しているSnapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 次の行動
    #   ]
    # }
    def _advance_locked(self, now_monotonic: float, snapshot_id: str) -> RecoveryPlan:
        episode = self._require_episode_locked()
        if now_monotonic >= episode.deadline:
            return self._fail_episode_locked(
                RecoveryReason.DEADLINE_EXCEEDED, snapshot_id
            )
        fresh = episode.fresh_snapshot_id
        if (
            fresh is not None
            and fresh not in episode.redecided_snapshot_ids
            and episode.redecide_used < self.policy.max_redecide
        ):
            episode.redecide_used += 1
            return self._issue_locked(
                RecoveryAction.REDECIDE, RecoveryReason.FRESH_FRAME_REDECIDE, fresh
            )
        if episode.reobserve_used < self.policy.max_reobserve:
            episode.reobserve_used += 1
            reason = (
                RecoveryReason.WARNING_REOBSERVE
                if episode.reobserve_used == 1
                else RecoveryReason.STALE_FRAME_REOBSERVE
            )
            return self._issue_locked(RecoveryAction.REOBSERVE, reason, snapshot_id)
        return self._fail_episode_locked(RecoveryReason.BUDGET_EXHAUSTED, snapshot_id)

    # {
    #   責務: [
    #     _issue_locked: 結果待ちのREOBSERVE/REDECIDE planを発行する
    #   ]
    #   処理: [
    #     1: attempt番号を付けたplanを作り、episodeの結果待ちとして保持する
    #   ]
    #   引数: [
    #     action: 発行する行動
    #     reason: 行動の理由
    #     snapshot_id: 行動の基準Snapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 発行したplan
    #   ]
    # }
    def _issue_locked(
        self, action: RecoveryAction, reason: RecoveryReason, snapshot_id: str
    ) -> RecoveryPlan:
        episode = self._require_episode_locked()
        self._state = (
            RecoveryState.REOBSERVE
            if action is RecoveryAction.REOBSERVE
            else RecoveryState.REDECIDE
        )
        attempt = episode.reobserve_used + episode.redecide_used
        plan = self._plan_locked(action, reason, snapshot_id, attempt=attempt)
        episode.pending = plan
        return plan

    # {
    #   責務: [
    #     _resolve_locked: 新Frameで異常が解消したepisodeを終了する
    #   ]
    #   処理: [
    #     1: 連続失敗数を0に戻し、cooldownを設定してRESOLVEDへ移る
    #   ]
    #   引数: [
    #     snapshot_id: 解消を確認したSnapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan: RESOLVEDのABSTAIN
    #   ]
    # }
    def _resolve_locked(self, snapshot_id: str) -> RecoveryPlan:
        episode = self._require_episode_locked()
        plan_event_ref = episode.event_ref
        self._scene_failures = 0
        self._cooldown_until_step = episode.step_count + self.policy.cooldown_steps
        self._episode = None
        self._state = RecoveryState.RESOLVED
        return self._plan_locked(
            RecoveryAction.ABSTAIN,
            RecoveryReason.RESOLVED,
            snapshot_id,
            event_ref=plan_event_ref,
        )

    # {
    #   責務: [
    #     _fail_episode_locked: 予算切れ・期限切れのepisodeを失敗として終了する
    #   ]
    #   処理: [
    #     1: 同一Sceneの連続失敗数を増やし、cooldownを設定する
    #     2: 連続失敗数が上限に達すれば停止要求、そうでなければABSTAINを返す
    #   ]
    #   引数: [
    #     reason: 失敗理由
    #     snapshot_id: 記録対象のSnapshot
    #   ]
    #   戻り値: [
    #     RecoveryPlan: ABSTAINまたはREQUEST_STOP
    #   ]
    # }
    def _fail_episode_locked(
        self, reason: RecoveryReason, snapshot_id: str | None
    ) -> RecoveryPlan:
        episode = self._require_episode_locked()
        event_ref = episode.event_ref
        self._scene_failures += 1
        self._cooldown_until_step = episode.step_count + self.policy.cooldown_steps
        self._episode = None
        if self._scene_failures >= self.policy.max_scene_failures:
            return self._stop_locked(
                RecoveryReason.SCENE_FAILURES_EXCEEDED,
                snapshot_id,
                reason.value,
                event_ref=event_ref,
            )
        self._state = RecoveryState.IDLE
        return self._plan_locked(
            RecoveryAction.ABSTAIN, reason, snapshot_id, event_ref=event_ref
        )

    # {
    #   責務: [
    #     _stop_locked: 再armまで保持する停止要求planを作る
    #   ]
    #   処理: [
    #     1: 停止理由文を作ってSTOP_REQUESTEDへ移る
    #   ]
    #   引数: [
    #     reason: 停止要求の型付き理由
    #     snapshot_id: 記録対象のSnapshot
    #     detail: 停止理由文へ付ける補足。無ければNone
    #     event_ref: 対象イベント。省略時は進行中episodeの値
    #   ]
    #   戻り値: [
    #     RecoveryPlan: REQUEST_STOPのplan
    #   ]
    # }
    def _stop_locked(
        self,
        reason: RecoveryReason,
        snapshot_id: str | None,
        detail: str | None,
        *,
        event_ref: str | None = None,
    ) -> RecoveryPlan:
        stop_reason = f"recovery:{reason.value}" + (f": {detail}" if detail else "")
        self._state = RecoveryState.STOP_REQUESTED
        self._stop_reason = stop_reason
        return self._plan_locked(
            RecoveryAction.REQUEST_STOP,
            reason,
            snapshot_id,
            stop_reason=stop_reason,
            event_ref=event_ref,
        )

    # {
    #   責務: [
    #     _plan_locked: 現在状態と残予算からRecoveryPlanを組み立てる
    #   ]
    #   処理: [
    #     1: 進行中episodeの残回数・残時間・期限を計算する
    #     2: 行動・理由・世代・Sceneを含むplanを返す
    #   ]
    #   引数: [
    #     action: 要求する行動
    #     reason: 行動の理由
    #     snapshot_id: 行動の基準Snapshot
    #     attempt: attempt番号。行動を要求しない場合は0
    #     stop_reason: REQUEST_STOPの理由文
    #     event_ref: 対象イベント。省略時は進行中episodeの値
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 組み立てたplan
    #   ]
    # }
    def _plan_locked(
        self,
        action: RecoveryAction,
        reason: RecoveryReason,
        snapshot_id: str | None,
        *,
        attempt: int = 0,
        stop_reason: str | None = None,
        event_ref: str | None = None,
    ) -> RecoveryPlan:
        episode = self._episode
        if episode is None:
            remaining = RecoveryBudget(0, 0, 0.0)
            deadline = None
        else:
            now = self._last_now if self._last_now is not None else episode.deadline
            remaining = RecoveryBudget(
                self.policy.max_reobserve - episode.reobserve_used,
                self.policy.max_redecide - episode.redecide_used,
                max(0.0, episode.deadline - now),
            )
            deadline = episode.deadline
            event_ref = event_ref or episode.event_ref
        return RecoveryPlan(
            action=action,
            reason=reason,
            state=self._state,
            event_ref=event_ref,
            remaining=remaining,
            deadline=deadline,
            action_epoch=self._action_epoch,
            scene_id=self._scene_id,
            snapshot_id=snapshot_id,
            attempt=attempt,
            stop_reason=stop_reason,
        )


# {
#   責務: [
#     _require_snapshot: Snapshot識別子が空でない文字列か検証する
#   ]
#   処理: [
#     1: 型と空白を確認する
#   ]
#   引数: [
#     snapshot_id: 検証するSnapshot識別子
#   ]
#   戻り値: [
#     なし
#   ]
#   エラー: [
#     無効な場合はValueErrorを送出する
#   ]
# }
def _require_snapshot(snapshot_id: str) -> None:
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("snapshot id must not be empty")
