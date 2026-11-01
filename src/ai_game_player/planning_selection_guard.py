"""SELECTを入力権限に変えず、新しい実画面へ照合する一手のguard。"""

from dataclasses import dataclass
from typing import NoReturn

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.observation_freshness import ObservationFreshness
from ai_game_player.planner_observation import PlannerObservationAdapter
from ai_game_player.planning_outcome import PlanningOutcome


# {
#   責務: [PlanningSelectionGuard: 元planと選択候補を同じRun世代・実画面へ束縛する]
#   フィールド: [context/belief_version: 元仮説, run_generation/expires_at_ns: 停止/失効境界,
#     semantic_digest: 観測由来state identity, candidate: 評価済みの実体,
#     capture: 元Frame参照, screen_id/signature: #628 receiptと一致すべき元判断参照]
# }
@dataclass(frozen=True, slots=True)
class PlanningSelectionGuard:
    context: BeliefContext
    belief_version: int
    run_generation: int
    expires_at_ns: int
    semantic_digest: str
    candidate: ActionCandidate
    capture: ObservationFreshness
    screen_id: str
    signature: str

    # {
    #   責務: [check_authority: input直前にもRun世代・失効時刻を照合する]
    #   処理: [SELECTを期限後・停止再開後へ持ち越さない]
    #   引数: [generation: 現Run世代, now_ns: 同origin時計] 戻り値: [なし]
    #   エラー: [DecisionWithheld: 古い/失効したplan]
    # }
    def check_authority(self, generation: int, now_ns: int) -> None:
        if generation != self.run_generation or now_ns >= self.expires_at_ns:
            raise DecisionWithheld(
                DecisionDisposition(
                    PlanningOutcome.ABSTAIN,
                    ("planning_selection_expired_or_stale",),
                    "belief_planner",
                    self.context.snapshot_id,
                )
            )

    # {
    #   責務: [validate_fresh: 新Frameの意味状態・候補実体と元計画を入力前に一致確認する]
    #   処理: [source/turn/state/座標/署名を照合し、画像が動いた同Epochの古いplanも拒否する]
    #   引数: [observation/candidates: 新取得の実画面と再評価候補, adapter: 同じtrusted sensor,
    #     generation/now_ns: 現authority] 戻り値: [ObservationFreshness: 照合した新Frame]
    #   エラー: [DecisionWithheld: 不一致/取得根拠不明]
    # }
    def validate_fresh(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        adapter: PlannerObservationAdapter,
        generation: int,
        now_ns: int,
    ) -> ObservationFreshness:
        self.check_authority(generation, now_ns)
        try:
            fresh = ObservationFreshness.from_observation(observation)
        except ValueError:
            self._reject("planning_fresh_reference_unavailable")
        if not fresh.is_newer_than(self.capture):
            self._reject("planning_fresh_reference_not_new")
        state = adapter.semantic_state(observation, self.context.action_epoch)
        if state != (self.semantic_digest, self.context.turn):
            self._reject("planning_semantic_state_changed", reobserve=True)
        matching = [
            candidate
            for candidate in candidates
            if candidate.action_id == self.candidate.action_id
        ]
        if len(matching) != 1 or matching[0] != self.candidate:
            self._reject("planning_candidate_changed", reobserve=True)
        if (
            observation.screen_id != self.screen_id
            or str(observation.features.get("signature", "")) != self.signature
        ):
            self._reject("planning_screen_signature_changed", reobserve=True)
        return fresh

    # {
    #   責務: [_reject: 不一致を非操作結果として伝え、fake actionへ変換しない]
    #   処理: [source/authority不明は保留、同scope内の実変化は有限再観測を要求する]
    #   引数: [reason: 固定code, reobserve: 同scope再計画可能か] 戻り値: [なし]
    #   エラー: [DecisionWithheld: 必ず入力前に脱出する]
    # }
    def _reject(self, reason: str, *, reobserve: bool = False) -> NoReturn:
        raise DecisionWithheld(
            DecisionDisposition(
                PlanningOutcome.OBSERVE_MORE if reobserve else PlanningOutcome.ABSTAIN,
                (reason,),
                "belief_planner",
                self.context.snapshot_id,
            )
        )
