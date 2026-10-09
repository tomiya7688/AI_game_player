import threading
from collections.abc import Callable


# {
#   責務: [ExecutionCancelled: 停止または再開で失効した実行を表す]
#   フィールド: [reason: キャンセル理由]
# }
class ExecutionCancelled(RuntimeError):
    # {
    #   責務: [__init__: キャンセル理由を例外へ保持する]
    #   処理: [reasonを属性と例外メッセージへ設定する]
    #   引数: [reason: 実行を継続できない理由]
    #   戻り値: []
    # }
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# {
#   責務: [RunController: GUIと実行パイプライン間でスレッド安全な停止状態を共有する]
#   フィールド: [running: 実行許可, rearm_token: 再開世代, stop_reason: 最新の停止理由]
#   処理: [開始・停止を同期し、古い世代の推論結果を拒否する]
# }
class RunController:
    """Pipeline execution state shared by the GUI and orchestration layer."""

    # {
    #   責務: [__init__: 実行可能状態と同期用ロックを初期化する]
    #   処理: [初期世代・状態・停止理由を設定する]
    #   引数: []
    #   戻り値: []
    # }
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = True
        self._rearm_token = 0
        self._stop_reason: str | None = None

    # {
    #   責務: [is_running: 現在の実行許可状態を返す]
    #   処理: [ロック内で状態を読み取る]
    #   引数: []
    #   戻り値: [bool: 実行許可状態]
    # }
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # {
    #   責務: [rearm_token: 明示的な再開世代を返す]
    #   処理: [ロック内で世代番号を読み取る]
    #   引数: []
    #   戻り値: [int: 再開世代]
    # }
    @property
    def rearm_token(self) -> int:
        """Monotonic token incremented only by an explicit start/re-arm action."""
        with self._lock:
            return self._rearm_token

    # {
    #   責務: [stop_reason: 最新の停止理由を返す]
    #   処理: [ロック内で理由を読み取る]
    #   引数: []
    #   戻り値: [str | None: 停止理由または停止理由なし]
    # }
    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    # {
    #   責務: [start: 実行を再開して旧推論結果と区別する]
    #   処理: [緊急停止を解除し、世代を進めて停止理由を保持する]
    #   引数: []
    #   戻り値: []
    # }
    def start(self) -> None:
        from ai_game_player.safety_guard import rearm_default_emergency_stop

        rearm_default_emergency_stop()
        with self._lock:
            self._rearm_token += 1
            self._running = True

    # {
    #   責務: [stop: 実行を停止し、停止理由を保持する]
    #   処理: [ロック内で停止状態と理由を設定する]
    #   引数: [reason: 停止を要求した要因]
    #   戻り値: []
    # }
    def stop(self, reason: str = "停止要求") -> None:
        with self._lock:
            self._running = False
            self._stop_reason = reason

    # {
    #   責務: [_validate_running_locked: 呼出側がロックを保持した状態で実行世代を検証する]
    #   処理: [停止状態と期待世代を照合し、不一致なら停止理由付き例外を送出する]
    #   引数: [expected_rearm_token: 対象処理が開始時に記録した再開世代]
    #   戻り値: []
    #   エラー: [ExecutionCancelled: 停止済みまたは旧世代]
    # }
    def _validate_running_locked(self, expected_rearm_token: int | None) -> None:
        if not self._running:
            raise ExecutionCancelled(self._stop_reason or "実行は停止されています")
        if expected_rearm_token is not None and expected_rearm_token != self._rearm_token:
            reason = self._stop_reason or "停止理由は記録されていません"
            raise ExecutionCancelled(f"再開前に開始した推論結果を破棄しました。停止理由: {reason}")

    # {
    #   責務: [run_if_current: 現在有効な実行世代の短いcommit処理を停止と直列化する]
    #   処理: [実行世代を検証し、RunControllerのlockを保持したままcommit callbackを実行する]
    #   引数: [expected_rearm_token: commit対象の開始世代, commit: 準備済み状態を公開する処理]
    #   戻り値: []
    #   エラー: [ExecutionCancelled: 停止済みまたは旧世代, Exception: commit callbackで発生した例外]
    # }
    def run_if_current(
        self,
        expected_rearm_token: int | None,
        commit: Callable[[], None],
    ) -> None:
        with self._lock:
            self._validate_running_locked(expected_rearm_token)
            commit()

    # {
    #   責務: [ensure_running: 現在の実行状態と推論開始時の世代を検証する]
    #   処理: [停止済みまたは世代失効なら理由付きキャンセルを送出する]
    #   引数: [expected_rearm_token: 実行開始時の再開世代]
    #   戻り値: []
    #   エラー: [ExecutionCancelled: 停止済みまたは旧世代]
    # }
    def ensure_running(self, expected_rearm_token: int | None = None) -> None:
        with self._lock:
            self._validate_running_locked(expected_rearm_token)
