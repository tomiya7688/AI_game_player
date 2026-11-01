"""任意2048 Sensor: 既存の画面Recognizer出力だけを未校正仮説へ変換する。"""

from collections.abc import Callable
import time
from uuid import uuid4

from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_candidate_store import (
    BeliefCandidateStore,
    BeliefStorePolicy,
)
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.decision_context import DecisionContext
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.models import ScreenObservation
from ai_game_player.observation_freshness import ObservationFreshness
from ai_game_player.planner_observation import PlannerObservationInput
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)


# {
#   責務: [Reference2048PlanningObservation: 既存画面認識を任意の純粋modelへ接続する]
#   フィールド: [clock_id/session_id: 同時計/実行scope, ttl_ns: 実験観測の有効期限,
#     store: bounded immutable仮説集合。DOM/乱数/正解stateを保持しない]
# }
class Reference2048PlanningObservation:
    # {
    #   責務: [__init__: 明示reference profileの時計とTTLを固定する]
    #   処理: [既定4秒の仮説TTLは物理Safety leaseを変更しない。未校正確率は常にunknown1]
    #   引数: [clock_id: Plannerと同じorigin, session_id: 任意実行ID, ttl_ns: 最大10秒の観測期限,
    #     clock_ns: monotonic時計] 戻り値: [なし]
    # }
    def __init__(
        self,
        *,
        clock_id: str,
        session_id: str | None = None,
        ttl_ns: int = 4_000_000_000,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(ttl_ns) is not int or not 1 <= ttl_ns <= 10_000_000_000:
            raise ValueError("reference planning TTL must be bounded")
        self.clock_id, self.session_id, self.ttl_ns = (
            clock_id,
            session_id or uuid4().hex,
            ttl_ns,
        )
        self.store = BeliefCandidateStore(
            Game2048State,
            clock_id=clock_id,
            policy=BeliefStorePolicy(max_age_ns=ttl_ns),
            clock_ns=clock_ns,
        )
        self._run_scope: tuple[object, ...] | None = None

    # {
    #   責務: [prepare: 同Frameの認識盤面から一つの未校正worldを構成する]
    #   処理: [recognition confidenceをworld確率へ変換せず、証拠相関IDをFrameへ固定する]
    #   引数: [context/observation: 実観測, action_epoch/run_generation: 実操作/停止世代, now_ns: 同時計]
    #   戻り値: [PlannerObservationInputまたはNone: 画面adapter不在だけ能力無し]
    #   エラー: [DecisionWithheld: 認識不足/失効。Baselineで無視しない]
    # }
    def prepare(
        self,
        context: DecisionContext,
        observation: ScreenObservation,
        action_epoch: int,
        run_generation: int,
        now_ns: int,
    ) -> PlannerObservationInput | None:
        if "game_2048" not in observation.features:
            return None
        state = self._state(observation)
        if state is None:
            raise DecisionWithheld(
                DecisionDisposition(
                    PlanningOutcome.ABSTAIN,
                    ("reference_board_unavailable",),
                    "belief_planner",
                    context.snapshot_id,
                )
            )
        capture = ObservationFreshness.from_observation(observation)
        if not capture.captured_at_ns <= now_ns < capture.captured_at_ns + self.ttl_ns:
            raise DecisionWithheld(
                DecisionDisposition(
                    PlanningOutcome.ABSTAIN,
                    ("reference_capture_expired",),
                    "belief_planner",
                    context.snapshot_id,
                )
            )
        source = BeliefContext(
            "2048",
            "board",
            f"{self.session_id}.run{run_generation}",
            context.snapshot_id,
            (capture.frame_id,),
            capture.captured_at_ns,
            action_epoch,
            action_epoch,
            capture.source_id,
            capture.source_generation,
            self.clock_id,
        )
        if self._run_scope is not None and self._run_scope != source.run_scope:
            self.store.reset(source)
        self._run_scope = source.run_scope
        evidence = BeliefEvidenceRef(
            "screen." + context.snapshot_id,
            capture.frame_id,
            BeliefEvidenceKind.OBSERVATION,
            "reference_screen_recognizer",
            "v1",
            source,
            capture.captured_at_ns,
            capture.frame_id,
        )
        state_ref = TypedStateRef("2048", "2048-state.v1", Game2048State, state)
        hypothesis = WorldStateHypothesis(
            "recognized_board",
            state_ref,
            source,
            BeliefWeight(BeliefWeightKind.RELATIVE_SCORE, 1),
            HypothesisOrigin.OBSERVATION_HYPOTHESIS,
            (evidence,),
            BeliefFreshness(
                capture.captured_at_ns, capture.captured_at_ns + self.ttl_ns
            ),
        )
        snapshot = self.store.publish(source, (hypothesis,), unknown_mass=1)
        return PlannerObservationInput(
            snapshot, "reference_2048.v1", "reference_play", state_ref.digest
        )

    # {
    #   責務: [semantic_state: 新Frameの認識値を純粋digestとして照合する]
    #   処理: [Storeや候補版を更新せずboard/観測score/終了flagだけを参照する]
    #   引数: [observation: 新実観測, action_epoch: 実操作連番] 戻り値: [digest/turnまたはNone]
    # }
    def semantic_state(
        self, observation: ScreenObservation, action_epoch: int
    ) -> tuple[str, int] | None:
        state = self._state(observation)
        if state is None:
            return None
        return TypedStateRef(
            "2048", "2048-state.v1", Game2048State, state
        ).digest, action_epoch

    # {
    #   責務: [current_belief: Storeの最新版を元scopeへ照合する]
    #   処理: [readだけを行い、再認識/更新を開始しない]
    #   引数: [context: 元判断scope] 戻り値: [BeliefSnapshotまたはNone]
    # }
    def current_belief(
        self, context: BeliefContext
    ) -> BeliefSnapshot[Game2048State] | None:
        return self.store.read(context)

    # {
    #   責務: [_state: 既存Recognizerの結果だけをtyped stateへ検証する]
    #   処理: [欠落/低支持/不正値は未知。raw DOMや画面外Oracleを使用しない]
    #   引数: [observation: 同Frame解析結果] 戻り値: [Game2048StateまたはNone]
    # }
    @staticmethod
    def _state(observation: ScreenObservation) -> Game2048State | None:
        raw = observation.features.get("game_2048")
        if not isinstance(raw, dict):
            return None
        confidence = raw.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0.65 <= confidence <= 1
            or type(raw.get("game_over")) is not bool
        ):
            return None
        board = raw.get("board")
        if not isinstance(board, (list, tuple)) or any(
            not isinstance(row, (list, tuple)) for row in board
        ):
            return None
        try:
            state = Game2048State(
                tuple(tuple(row) for row in board),
                score=raw.get("score"),
                over=raw["game_over"],
            )
            if not state.over and not any(tile for row in state.cells for tile in row):
                return None
            return state
        except (TypeError, ValueError):
            return None
