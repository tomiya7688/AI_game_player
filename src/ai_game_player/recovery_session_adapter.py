from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ai_game_player.recovery_controller import RecoveryController
from ai_game_player.recovery_models import (
    RecoveryAction,
    RecoveryAttemptResult,
    RecoveryPlan,
    RecoveryReason,
)
from ai_game_player.self_check_models import SelfCheckEvent


RECOVERY_PLAN_EVENT_TYPE = "recovery.plan"
RECOVERY_ATTEMPT_EVENT_TYPE = "recovery.attempt"


# {
#   責務: [
#     RecoveryJournal: Recovery記録に必要なEvent Journalのappend契約を表す
#   ]
#   フィールド: [
#     なし: EventJournal.appendと同じ形の呼出しだけを要求するProtocol
#   ]
# }
class RecoveryJournal(Protocol):
    # {
    #   責務: [
    #     append: 1件のRecovery eventを記録する
    #   ]
    #   処理: [
    #     1: 実装側がevent_typeとpayloadを永続化する
    #   ]
    #   引数: [
    #     event_type: event種別, status: 行動または結果, snapshot_id: 対象Snapshot, correlation_id: episode識別子, payload: versioned辞書
    #   ]
    #   戻り値: [
    #     object: 実装側の記録結果
    #   ]
    #   エラー: [
    #     記録できない場合は実装側の例外を送出する
    #   ]
    # }
    def append(
        self,
        event_type: str,
        *,
        status: str | None = None,
        snapshot_id: str | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> object: ...


# {
#   責務: [
#     RecoverySessionAdapter: RecoveryPlanをEvent Journalへ記録し、停止要求だけをSessionへ渡すAdapter
#   ]
#   フィールド: [
#     controller: Recovery状態機械
#     _journal: recovery.plan/recovery.attemptの記録先
#     _request_stop: 停止理由文を受け取りSessionを停止する関数（例: GameSessionController.stop）
#   ]
#   処理: [
#     REOBSERVE/REDECIDEは呼出側へ返すだけで実入力を送らない。実入力は既存のDecisionPipeline/Verifier/Safety経路が担う
#   ]
# }
class RecoverySessionAdapter:
    # {
    #   責務: [
    #     __init__: Controller・記録先・停止要求先を保持する
    #   ]
    #   処理: [
    #     1: 依存を保持する
    #   ]
    #   引数: [
    #     controller: Recovery状態機械
    #     journal: EventJournal互換の記録先
    #     request_stop: 停止理由文を受け取る停止要求関数
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def __init__(
        self,
        controller: RecoveryController,
        journal: RecoveryJournal,
        request_stop: Callable[[str], None],
    ) -> None:
        self.controller = controller
        self._journal = journal
        self._request_stop = request_stop

    # {
    #   責務: [
    #     handle_event: Self-CheckイベントをRecoveryPlanへ変換し、記録と停止要求を行う
    #   ]
    #   処理: [
    #     1: Controllerでplanを決める（Controllerのlock外でJournalと停止要求を呼ぶ）
    #     2: planを記録し、記録に失敗した場合は停止要求へ昇格する
    #     3: REQUEST_STOPなら既存Stopが無い場合だけ停止を要求する
    #   ]
    #   引数: [
    #     event: SelfCheckMonitorが返したイベント
    #     snapshot_id: イベント検出時点のSnapshot
    #     action_generation: RunControllerの再arm世代
    #     now_monotonic: monotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #     target_lost: 操作対象を失っていればTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 呼出側が従う行動
    #   ]
    #   エラー: [
    #     Controllerへの入力が無効な場合はValueErrorを送出する
    #   ]
    # }
    def handle_event(
        self,
        event: SelfCheckEvent,
        *,
        snapshot_id: str,
        action_generation: int,
        now_monotonic: float,
        stop_requested: bool,
        target_lost: bool = False,
    ) -> RecoveryPlan:
        plan = self.controller.on_event(
            event,
            snapshot_id=snapshot_id,
            action_generation=action_generation,
            now_monotonic=now_monotonic,
            stop_requested=stop_requested,
            target_lost=target_lost,
        )
        return self._commit(plan, stop_requested)

    # {
    #   責務: [
    #     handle_result: Recovery attemptの結果を記録し、次のplanを決める
    #   ]
    #   処理: [
    #     1: attempt結果を記録し、記録に失敗した場合は停止要求へ昇格する
    #     2: Controllerで次のplanを決めて記録し、停止要求を渡す
    #   ]
    #   引数: [
    #     result: 呼出側が実行したattemptの結果
    #     correlation_id: 対象planのcorrelation_id
    #     now_monotonic: monotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 呼出側が従う次の行動
    #   ]
    #   エラー: [
    #     時刻が無効な場合はValueErrorを送出する
    #   ]
    # }
    def handle_result(
        self,
        result: RecoveryAttemptResult,
        *,
        correlation_id: str,
        now_monotonic: float,
        stop_requested: bool = False,
    ) -> RecoveryPlan:
        try:
            self._journal.append(
                RECOVERY_ATTEMPT_EVENT_TYPE,
                status=result.status.value,
                snapshot_id=result.snapshot_id,
                correlation_id=correlation_id,
                payload=result.to_dict(),
            )
        except Exception:  # noqa: BLE001 - 記録できないRecoveryは継続せず停止要求へ昇格する
            return self._fail_closed(result.snapshot_id, stop_requested)
        plan = self.controller.ack(
            result, now_monotonic=now_monotonic, stop_requested=stop_requested
        )
        return self._commit(plan, stop_requested)

    # {
    #   責務: [
    #     handle_poll: 結果が返らないattemptを期限で打ち切り、記録と停止要求を行う
    #   ]
    #   処理: [
    #     1: Controllerのpollで期限切れを判定し、planがあれば記録して停止要求を渡す
    #   ]
    #   引数: [
    #     now_monotonic: monotonic時刻（秒）
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan | None: 打ち切った場合のplan。変化が無ければNone
    #   ]
    # }
    def handle_poll(
        self, *, now_monotonic: float, stop_requested: bool = False
    ) -> RecoveryPlan | None:
        plan = self.controller.poll(
            now_monotonic=now_monotonic, stop_requested=stop_requested
        )
        return None if plan is None else self._commit(plan, stop_requested)

    # {
    #   責務: [
    #     _commit: planを記録し、REQUEST_STOPなら停止を要求する
    #   ]
    #   処理: [
    #     1: recovery.planを記録し、失敗したら停止要求へ昇格する
    #     2: REQUEST_STOPで既存Stopが無い場合だけ停止要求関数を呼ぶ
    #   ]
    #   引数: [
    #     plan: 記録するplan
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: 記録に成功したplan、または停止要求plan
    #   ]
    # }
    def _commit(self, plan: RecoveryPlan, stop_requested: bool) -> RecoveryPlan:
        if not self._try_record_plan(plan):
            return self._fail_closed(plan.snapshot_id, stop_requested)
        self._dispatch_stop(plan, stop_requested)
        return plan

    # {
    #   責務: [
    #     _fail_closed: 記録失敗時にRecoveryを停止要求へ昇格する
    #   ]
    #   処理: [
    #     1: ControllerをJOURNAL_FAILEDで停止要求状態にする
    #     2: 停止要求planの記録を1回だけ試み、成否に関係なく停止要求を渡す
    #   ]
    #   引数: [
    #     snapshot_id: 記録対象のSnapshot
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     RecoveryPlan: JOURNAL_FAILEDのREQUEST_STOP
    #   ]
    # }
    def _fail_closed(
        self, snapshot_id: str | None, stop_requested: bool
    ) -> RecoveryPlan:
        plan = self.controller.request_stop(RecoveryReason.JOURNAL_FAILED, snapshot_id)
        # 記録先が壊れていても停止要求は必ず渡すため、2回目の記録結果では分岐しない。
        self._try_record_plan(plan)
        self._dispatch_stop(plan, stop_requested)
        return plan

    # {
    #   責務: [
    #     _try_record_plan: planをrecovery.planとしてJournalへ保存し、成否を返す
    #   ]
    #   処理: [
    #     1: action・Snapshot・correlation_id・payloadを付けてappendする
    #     2: 記録例外は停止要求へ昇格させるためFalseへ変換する
    #   ]
    #   引数: [
    #     plan: 保存するplan
    #   ]
    #   戻り値: [
    #     bool: 記録できた場合はTrue
    #   ]
    # }
    def _try_record_plan(self, plan: RecoveryPlan) -> bool:
        try:
            self._journal.append(
                RECOVERY_PLAN_EVENT_TYPE,
                status=plan.action.value,
                snapshot_id=plan.snapshot_id,
                correlation_id=plan.correlation_id,
                payload=plan.to_dict(),
            )
        except Exception:  # noqa: BLE001 - 記録できないRecoveryは継続せず停止要求へ昇格する
            return False
        return True

    # {
    #   責務: [
    #     _dispatch_stop: REQUEST_STOPを既存Stopの置換無しでSessionへ渡す
    #   ]
    #   処理: [
    #     1: REQUEST_STOPかつ既存Stopが無い場合だけ停止理由文を渡す
    #   ]
    #   引数: [
    #     plan: 判定するplan
    #     stop_requested: 既存Stopが要求済みならTrue
    #   ]
    #   戻り値: [
    #     なし
    #   ]
    # }
    def _dispatch_stop(self, plan: RecoveryPlan, stop_requested: bool) -> None:
        if plan.action is RecoveryAction.REQUEST_STOP and not stop_requested:
            self._request_stop(plan.stop_reason or "recovery:stop")
