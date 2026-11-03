from dataclasses import dataclass
from enum import Enum
from math import isfinite


RECOVERY_PLAN_SCHEMA = "recovery.plan/v1"
RECOVERY_ATTEMPT_SCHEMA = "recovery.attempt/v1"
MAXIMUM_RECOVERY_DEADLINE_SECONDS = 60.0


# {
#   責務: [
#     RecoveryAction: Recovery Controllerが呼出側へ要求する次の行動を表す
#   ]
#   フィールド: [
#     REOBSERVE: 新しいFrameを取得して状態を観測し直す
#     REDECIDE: 新しいFrameに対して既存Decision/Verifier/Safety経路で判断し直す
#     ABSTAIN: Recoveryとしては何もしない。通常経路の判断は既存Safetyに任せる
#     REQUEST_STOP: RunControllerへ安全停止を要求する
#   ]
# }
class RecoveryAction(str, Enum):
    REOBSERVE = "reobserve"
    REDECIDE = "redecide"
    ABSTAIN = "abstain"
    REQUEST_STOP = "request_stop"


# {
#   責務: [
#     RecoveryState: Session内のRecovery episodeの進行段階を表す
#   ]
#   フィールド: [
#     IDLE: 進行中のRecovery episodeが無い
#     REOBSERVE: 再観測の結果待ち
#     REDECIDE: 再判断の結果待ち
#     RESOLVED: 直前のepisodeが解消し、cooldown中である可能性がある
#     STOP_REQUESTED: 停止要求済み。再armまで新しいRecoveryを開始しない
#   ]
# }
class RecoveryState(str, Enum):
    IDLE = "idle"
    REOBSERVE = "reobserve"
    REDECIDE = "redecide"
    RESOLVED = "resolved"
    STOP_REQUESTED = "stop_requested"


# {
#   責務: [
#     RecoveryReason: RecoveryPlanを選んだ理由を型付きで記録する
#   ]
#   フィールド: [
#     WARNING_REOBSERVE / FRESH_FRAME_REDECIDE / STALE_FRAME_REOBSERVE: Recoveryを進める理由
#     RESOLVED: 新しいFrameで異常が解消した
#     STOP_LATCHED / STALE_EPOCH / TARGET_LOST: 最優先で拒否した理由
#     DUPLICATE_EVENT / COOLDOWN / PLAN_PENDING / UNEXPECTED_ACK: 進行無しで記録した理由
#     BUDGET_EXHAUSTED / DEADLINE_EXCEEDED: episodeを失敗として終了した理由
#     CRITICAL_SELF_CHECK / SCENE_FAILURES_EXCEEDED / JOURNAL_FAILED / STOP_ALREADY_REQUESTED: 停止要求の理由
#   ]
# }
class RecoveryReason(str, Enum):
    WARNING_REOBSERVE = "warning_reobserve"
    FRESH_FRAME_REDECIDE = "fresh_frame_redecide"
    STALE_FRAME_REOBSERVE = "stale_frame_reobserve"
    RESOLVED = "resolved"
    STOP_LATCHED = "stop_latched"
    STALE_EPOCH = "stale_epoch"
    TARGET_LOST = "target_lost"
    DUPLICATE_EVENT = "duplicate_event"
    COOLDOWN = "cooldown"
    PLAN_PENDING = "plan_pending"
    UNEXPECTED_ACK = "unexpected_ack"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CRITICAL_SELF_CHECK = "critical_self_check"
    SCENE_FAILURES_EXCEEDED = "scene_failures_exceeded"
    JOURNAL_FAILED = "journal_failed"
    STOP_ALREADY_REQUESTED = "stop_already_requested"


# {
#   責務: [
#     RecoveryAttemptStatus: 呼出側が実行したRecovery attemptの結果を表す
#   ]
#   フィールド: [
#     RESOLVED: 新しいFrameで異常が解消した
#     UNRESOLVED: attemptは完了したが異常が残った
#     FAILED: attemptを完了できなかった（合法候補なし、観測失敗など）
#   ]
# }
class RecoveryAttemptStatus(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


# {
#   責務: [
#     RecoveryPolicy: Recovery episodeの回数・時間・cooldown・連続失敗の上限を保持する
#   ]
#   フィールド: [
#     max_reobserve: 1 episodeで要求する再観測の最大回数
#     max_redecide: 1 episodeで要求する再判断の最大回数
#     deadline_seconds: episode開始から打ち切るまでの秒数（monotonic clock）
#     cooldown_steps: 解消・失敗後に新しいepisodeを開始しないSelf-Check step数
#     max_scene_failures: 同一Sceneで停止要求へ昇格する連続episode失敗数
#   ]
# }
@dataclass(frozen=True)
class RecoveryPolicy:
    max_reobserve: int = 2
    max_redecide: int = 1
    deadline_seconds: float = 3.0
    cooldown_steps: int = 2
    max_scene_failures: int = 2

    # {
    #   責務: [
    #     __post_init__: Recovery上限が有限で、無限反復を許さない値か検証する
    #   ]
    #   処理: [
    #     1: 回数上限が0以上の整数で、boolでないことを確認する
    #     2: 連続失敗上限が1以上であることを確認する
    #     3: 期限が有限・正で上限秒数以下であることを確認する
    #   ]
    #   引数: [
    #     self: 検証対象の上限
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     上限が範囲外の場合はValueErrorを送出する
    #   ]
    # }
    def __post_init__(self) -> None:
        counts = (
            self.max_reobserve,
            self.max_redecide,
            self.cooldown_steps,
            self.max_scene_failures,
        )
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("recovery counts must be non-negative integers")
        if self.max_scene_failures < 1:
            raise ValueError("max scene failures must be at least one")
        if (
            type(self.deadline_seconds) not in (int, float)
            or not isfinite(self.deadline_seconds)
            or not 0 < self.deadline_seconds <= MAXIMUM_RECOVERY_DEADLINE_SECONDS
        ):
            raise ValueError("recovery deadline must be finite, positive and bounded")


# {
#   責務: [
#     RecoveryBudget: RecoveryPlan時点で残っている回数と時間を表す
#   ]
#   フィールド: [
#     reobserve: 残りの再観測回数
#     redecide: 残りの再判断回数
#     seconds: episode期限までの残り秒数。episodeが無い場合は0
#   ]
# }
@dataclass(frozen=True)
class RecoveryBudget:
    reobserve: int
    redecide: int
    seconds: float


# {
#   責務: [
#     RecoveryPlan: Self-Checkイベントまたはattempt結果から決めた次の行動を記録可能な形で表す
#   ]
#   フィールド: [
#     action: 呼出側へ要求する行動
#     reason: 行動を選んだ型付き理由
#     state: 行動決定後のController状態
#     event_ref: 対象SelfCheckEventの識別子（category@step_count）。無い場合はNone
#     remaining: 決定後に残る回数と時間
#     deadline: episode期限のmonotonic時刻。episodeが無い場合はNone
#     action_epoch: 計画を有効とする行動世代（RunControllerの再arm世代）
#     scene_id: 対象Scene
#     snapshot_id: 行動の基準Snapshot。REDECIDEでは新しく観測したSnapshot
#     attempt: episode内のattempt番号。行動を要求しない場合は0
#     stop_reason: REQUEST_STOPの場合の停止理由文
#   ]
#   処理: [
#     to_dict: recovery.plan/v1形式へ変換する
#   ]
# }
@dataclass(frozen=True)
class RecoveryPlan:
    action: RecoveryAction
    reason: RecoveryReason
    state: RecoveryState
    event_ref: str | None
    remaining: RecoveryBudget
    deadline: float | None
    action_epoch: int
    scene_id: str | None
    snapshot_id: str | None
    attempt: int = 0
    stop_reason: str | None = None

    # {
    #   責務: [
    #     correlation_id: 同じepisodeのplan・attempt・結果をJournalで結び付けるIDを返す
    #   ]
    #   処理: [
    #     1: 行動世代・Scene・event参照から決定論的なIDを作る
    #   ]
    #   引数: [
    #     self: 対象のplan
    #   ]
    #   戻り値: [
    #     str: recovery:<epoch>:<scene>:<event_ref>形式のID
    #   ]
    # }
    @property
    def correlation_id(self) -> str:
        return f"recovery:{self.action_epoch}:{self.scene_id or '-'}:{self.event_ref or '-'}"

    # {
    #   責務: [
    #     to_dict: planをEvent Journalへ保存するversioned辞書へ変換する
    #   ]
    #   処理: [
    #     1: Enum値と残予算をJSON互換値に変換する
    #   ]
    #   引数: [
    #     self: 変換対象のplan
    #   ]
    #   戻り値: [
    #     dict[str, object]: recovery.plan/v1の辞書
    #   ]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "schema": RECOVERY_PLAN_SCHEMA,
            "action": self.action.value,
            "reason": self.reason.value,
            "state": self.state.value,
            "event_ref": self.event_ref,
            "remaining": {
                "reobserve": self.remaining.reobserve,
                "redecide": self.remaining.redecide,
                "seconds": self.remaining.seconds,
            },
            "deadline": self.deadline,
            "action_epoch": self.action_epoch,
            "scene_id": self.scene_id,
            "snapshot_id": self.snapshot_id,
            "attempt": self.attempt,
            "stop_reason": self.stop_reason,
        }


# {
#   責務: [
#     RecoveryAttemptResult: 呼出側がRecoveryPlanを実行した結果をControllerへ返す
#   ]
#   フィールド: [
#     action: 実行したplanの行動
#     action_epoch: 実行したplanの行動世代
#     attempt: 実行したplanのattempt番号
#     snapshot_id: attempt完了時点で観測したSnapshot。新Frameが無ければplanと同じ値
#     status: attemptの結果
#     detail: 失敗理由など記録用の補足
#   ]
#   処理: [
#     to_dict: recovery.attempt/v1形式へ変換する
#   ]
# }
@dataclass(frozen=True)
class RecoveryAttemptResult:
    action: RecoveryAction
    action_epoch: int
    attempt: int
    snapshot_id: str
    status: RecoveryAttemptStatus
    detail: str = ""

    # {
    #   責務: [
    #     __post_init__: Controllerへ返す結果が実行可能な行動とSnapshotを指すか検証する
    #   ]
    #   処理: [
    #     1: 行動がREOBSERVEまたはREDECIDEであることを確認する
    #     2: 世代・attempt番号・Snapshot識別子の範囲を確認する
    #   ]
    #   引数: [
    #     self: 検証対象の結果
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    #   エラー: [
    #     結果が無効な場合はValueErrorを送出する
    #   ]
    # }
    def __post_init__(self) -> None:
        if self.action not in (RecoveryAction.REOBSERVE, RecoveryAction.REDECIDE):
            raise ValueError("only reobserve and redecide attempts can be acknowledged")
        if type(self.action_epoch) is not int or self.action_epoch < 0:
            raise ValueError("action epoch must be a non-negative integer")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if not self.snapshot_id.strip():
            raise ValueError("snapshot id must not be empty")

    # {
    #   責務: [
    #     to_dict: attempt結果をEvent Journalへ保存するversioned辞書へ変換する
    #   ]
    #   処理: [
    #     1: Enum値をJSON互換値に変換する
    #   ]
    #   引数: [
    #     self: 変換対象の結果
    #   ]
    #   戻り値: [
    #     dict[str, object]: recovery.attempt/v1の辞書
    #   ]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "schema": RECOVERY_ATTEMPT_SCHEMA,
            "action": self.action.value,
            "action_epoch": self.action_epoch,
            "attempt": self.attempt,
            "snapshot_id": self.snapshot_id,
            "status": self.status.value,
            "detail": self.detail,
        }
