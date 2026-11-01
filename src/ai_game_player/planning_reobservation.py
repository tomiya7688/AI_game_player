"""Frame再取得で復活しない、意思決定episodeの再観測予算と実行ack境界。"""

from dataclasses import dataclass
from threading import Lock

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_likelihood import BeliefActionRef
from ai_game_player.belief_validation import belief_id, belief_integer
from ai_game_player.planning_risk import planning_value


# {
#   責務: [PlanningObservationBudget: 一つのGoal episodeに使える追加観測を制限する]
#   フィールド: [max_requests: 最大要求回数, max_total_cost: normalized value単位の累積上限,
#     max_elapsed_ns: episode開始からのmonotonic時間上限。新Frameで延長しない]
# }
@dataclass(frozen=True, slots=True)
class PlanningObservationBudget:
    max_requests: int = 2
    max_total_cost: float = 0.4
    max_elapsed_ns: int = 3_000_000_000

    # {
    #   責務: [__post_init__: 有限の再観測予算を検証する]
    #   処理: [回数0は再観測無効、時間0は即期限とし、負数/非有限値を拒否する]
    #   引数: [self: 予算]
    #   戻り値: [なし]
    #   エラー: [ValueError: 予算違反]
    # }
    def __post_init__(self) -> None:
        belief_integer(self.max_requests, 0, 32)
        planning_value(self.max_total_cost, 0.0, 64.0)
        belief_integer(self.max_elapsed_ns, 0, 60_000_000_000)


# {
#   責務: [PlanningDecisionEpisode: 再観測予算をGoalと実行scopeへ固定し、実行ackだけで更新する]
#   フィールド: [context/goal_id: 所有する意思決定episode, budget: 要求/費用/時間上限,
#     _requests/_cost/_started: 新snapshotでも保持する消費, _pending: 最後に選択したcontext/action,
#     _acknowledged_frame: 既に実行した判断frameの再採用拒否, _lock: 短い状態操作の直列化]
# }
class PlanningDecisionEpisode:
    # {
    #   責務: [__init__: trusted runtime ownerがGoal用の一つのepisodeを開始する]
    #   処理: [IOや入力を発行せずscopeと開始時刻を固定する。Frame毎の再生成は禁止する利用契約]
    #   引数: [context: 初期scope, goal_id: 固定Goal参照, now_ns: 同clock originの時刻, budget: 有限予算]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        context: BeliefContext,
        goal_id: str,
        now_ns: int,
        budget: PlanningObservationBudget | None = None,
    ) -> None:
        if type(context) is not BeliefContext:
            raise ValueError("planning episode requires typed context")
        belief_id(goal_id)
        belief_integer(now_ns)
        self.context = context
        self.goal_id = goal_id
        self.budget = budget or PlanningObservationBudget()
        if type(self.budget) is not PlanningObservationBudget:
            raise ValueError("planning episode requires typed observation budget")
        self._requests = 0
        self._cost = 0.0
        self._started = now_ns
        self._last_time = now_ns
        self._pending: tuple[BeliefContext, str] | None = None
        self._acknowledged_frame = -1
        self._lock = Lock()

    # {
    #   責務: [check_context: 同scope/Goalと前進するFrameだけをepisodeへ受け入れる]
    #   処理: [新Frameで消費をresetせず、時計後退/旧世代/同Frame別snapshot/既実行Frameを拒否する]
    #   引数: [context: 現在の実観測, goal_id: 今回のGoal, now_ns: 同origin時刻]
    #   戻り値: [bool: 計画評価を許可する場合True]
    # }
    def check_context(self, context: BeliefContext, goal_id: str, now_ns: int) -> bool:
        with self._lock:
            if (
                context.run_scope != self.context.run_scope
                or context.action_epoch != self.context.action_epoch
                or goal_id != self.goal_id
                or now_ns < self._last_time
                or context.frame_sequence <= self._acknowledged_frame
                or context.frame_sequence < self.context.frame_sequence
                or context.turn < self.context.turn
                or (
                    context.frame_sequence == self.context.frame_sequence
                    and context != self.context
                )
            ):
                return False
            self._last_time = now_ns
            self.context = context
            return True

    # {
    #   責務: [request_observation: 予算内の再観測要求を一回消費する]
    #   処理: [同時要求でも回数/累積費用/episode時間を超えない。消費をFrame更新で戻さない]
    #   引数: [context: 評価済みの現context, cost: 同価値単位の非負費用, now_ns: 現在時刻]
    #   戻り値: [bool: 消費して要求できる場合True]
    # }
    def request_observation(
        self, context: BeliefContext, cost: float, now_ns: int
    ) -> bool:
        cost = planning_value(cost, 0.0, 2.0)
        with self._lock:
            if (
                context != self.context
                or now_ns < self._last_time
                or self._requests >= self.budget.max_requests
                or self._cost + cost > self.budget.max_total_cost
                or now_ns - self._started >= self.budget.max_elapsed_ns
            ):
                return False
            self._requests += 1
            self._cost += cost
            self._last_time = now_ns
            self._pending = None
            return True

    # {
    #   責務: [record_selection: 実行ackで照合する直近の提案を記憶する]
    #   処理: [現在contextだけを受理し、実行したという事実は生成しない]
    #   引数: [context: 判断元, action_id: 許可候補から選択したID]
    #   戻り値: [bool: 同じepisodeへ登録できた場合True]
    # }
    def record_selection(self, context: BeliefContext, action_id: str) -> bool:
        belief_id(action_id)
        with self._lock:
            if context != self.context:
                return False
            self._pending = (context, action_id)
            return True

    # {
    #   責務: [acknowledge_execution: 実行元が確認した同じ提案のackだけで次episodeの予算を開始する]
    #   処理: [既存BeliefActionRefを再利用し、送信不明/旧操作/再送は拒否する。参照型だけを実行証明とはしない]
    #   引数: [action: trusted runtimeがExecutionReceiptから投影した参照, now_ns: 同origin時刻]
    #   戻り値: [bool: 一回だけackを受理した場合True]
    # }
    def acknowledge_execution(self, action: BeliefActionRef, now_ns: int) -> bool:
        belief_integer(now_ns)
        with self._lock:
            if (
                type(action) is not BeliefActionRef
                or action.execution_status != "executed"
                or self._pending != (action.source_context, action.action_id)
                or now_ns < self._last_time
            ):
                return False
            self._acknowledged_frame = action.source_context.frame_sequence
            self._requests = 0
            self._cost = 0.0
            self._started = now_ns
            self._last_time = now_ns
            self._pending = None
            return True

    # {
    #   責務: [reset_scope: 明示された別実行scope/世代への移行時だけ予算を開始し直す]
    #   処理: [同じscopeの新Frame/Goal名変更ではresetしない]
    #   引数: [context: 別scopeの初期観測, goal_id: 次Goal参照, now_ns: 新clock origin時刻]
    #   戻り値: [なし]
    #   エラー: [ValueError: 同scopeによる予算reset]
    # }
    def reset_scope(self, context: BeliefContext, goal_id: str, now_ns: int) -> None:
        belief_id(goal_id)
        belief_integer(now_ns)
        with self._lock:
            same_source = (
                context.game_id,
                context.scene_id,
                context.session_id,
                context.source_id,
            ) == (
                self.context.game_id,
                self.context.scene_id,
                self.context.session_id,
                self.context.source_id,
            )
            if (
                same_source
                and context.source_generation < self.context.source_generation
            ):
                raise ValueError(
                    "an older source generation cannot reset a planning episode"
                )
            if (
                context.run_scope == self.context.run_scope
                and context.action_epoch <= self.context.action_epoch
            ):
                raise ValueError(
                    "new frames, goals or older epochs cannot reset an observation episode"
                )
            self.context = context
            self.goal_id = goal_id
            self._requests = 0
            self._cost = 0.0
            self._started = now_ns
            self._last_time = now_ns
            self._pending = None
            self._acknowledged_frame = -1

    # {
    #   責務: [usage: 残予算の監査用に消費を不変値で返す]
    #   処理: [内部状態の可変参照を公開しない]
    #   引数: [self: episode]
    #   戻り値: [tuple: 再観測要求回数と同価値単位の累積費用]
    # }
    @property
    def usage(self) -> tuple[int, float]:
        with self._lock:
            return self._requests, self._cost
