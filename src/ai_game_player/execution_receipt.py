"""planned判断と実入力の事実を分離する、世代付き実行受領記録。"""

from dataclasses import asdict, dataclass

from ai_game_player.outcome_models import OutcomeEvent


EXECUTION_STATUSES = frozenset(
    {"executed", "dispatch_unknown", "not_executed", "not_dispatched", "dry_run"}
)


# {
#   責務: [ExecutionReceipt: 実行境界から得た送信事実を元のsnapshot・操作・世代へ固定する]
#   フィールド: [snapshot_id/action_id: 元の判断と候補, action_generation: 実行開始時の停止・再開世代,
#     screen_id/state_signature: 入力直前観測との照合情報, execution_status: 送信済み・結果不明・未送信・dry-run,
#     input_dispatch_started: Trueは送信境界到達、Falseは未到達、Noneはbackendが証明できない場合,
#     eligible_for_learning: 有効世代の正常送信だけを学習対象にする指定, reason: 固定の監査理由]
# }
@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    snapshot_id: str
    action_id: str
    action_generation: int
    screen_id: str
    state_signature: str
    execution_status: str
    input_dispatch_started: bool | None
    eligible_for_learning: bool = True
    reason: str = ""

    # {
    #   責務: [__post_init__: 不正な世代や矛盾する送信記録を拒否する]
    #   処理: [IDと状態を検査し、未送信や結果不明を学習成功として扱わない]
    #   引数: [self: 構築中の受領記録]
    #   戻り値: [なし]
    #   エラー: [ValueError: ID・世代・status・送信状態が不正]
    # }
    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.snapshot_id, self.action_id, self.screen_id)
        ):
            raise ValueError(
                "execution receipt requires bound snapshot/action/screen IDs"
            )
        if type(self.action_generation) is not int or self.action_generation < 0:
            raise ValueError("execution receipt requires a non-negative generation")
        if not isinstance(self.state_signature, str) or not isinstance(
            self.reason, str
        ):
            raise ValueError("invalid execution receipt references")
        if (
            self.execution_status not in EXECUTION_STATUSES
            or type(self.eligible_for_learning) is not bool
        ):
            raise ValueError("invalid execution receipt status")
        if (
            self.input_dispatch_started is not None
            and type(self.input_dispatch_started) is not bool
        ):
            raise ValueError("invalid dispatch evidence")
        if (
            self.execution_status == "executed"
            and self.input_dispatch_started is not True
        ):
            raise ValueError("executed receipt requires dispatch evidence")
        if (
            self.execution_status in {"not_executed", "not_dispatched", "dry_run"}
            and self.input_dispatch_started is not False
        ):
            raise ValueError("non-dispatched receipt must not claim input")
        if self.execution_status != "executed":
            object.__setattr__(self, "eligible_for_learning", False)

    # {
    #   責務: [to_dict: 実行事実をtrace/Journalへ保存する固定構造に変換する]
    #   処理: [元の観測画素や自由な設定を含めず、受領記録をコピーする]
    #   引数: [self: 世代に結び付いた受領記録]
    #   戻り値: [dict: JSON化できる監査用記録]
    # }
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# {
#   責務: [ExecutionObservation: 実操作の受領記録と、同世代で公開した後続観測結果を一対に固定する]
#   フィールド: [receipt: snapshot・操作・世代を確定する受領記録,
#     outcome: 既存検出器の結果。画面変化だけでは因果的な成功を保証しない,
#     after_screen_id/after_signature: 結果を検出した画面の参照]
# }
@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    receipt: ExecutionReceipt
    outcome: "OutcomeEvent"
    after_screen_id: str
    after_signature: str

    # {
    #   責務: [__post_init__: 異なる操作や結果不明dispatchの結果を混在させない]
    #   処理: [Outcomeの操作IDとreceiptを照合し、不明dispatchの成功化を拒否する]
    #   引数: [self: 構築中の観測結果ペア]
    #   戻り値: [なし]
    #   エラー: [ValueError: 操作または送信状態との矛盾]
    # }
    def __post_init__(self) -> None:
        if self.receipt.action_id != self.outcome.action_id:
            raise ValueError("execution observation action does not match receipt")
        if (
            self.receipt.execution_status == "dispatch_unknown"
            and self.outcome.status != "unknown"
        ):
            raise ValueError("unknown dispatch must not become a confirmed outcome")

    # {
    #   責務: [to_dict: 実行参照と結果を分離したprojection用データを返す]
    #   処理: [受領記録と既存Outcomeの表現をそれぞれコピーする]
    #   引数: [self: generation固定済みの観測結果]
    #   戻り値: [dict: 学習側が因果証拠を別途検証するための構造化データ]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "receipt": self.receipt.to_dict(),
            "outcome": self.outcome.to_dict(),
            "after_screen_id": self.after_screen_id,
            "after_signature": self.after_signature,
        }
