from collections import Counter

from ai_game_player.models import ActionCandidate, ScreenObservation


class ActionEvaluator:
    SUPPORTED = frozenset({"click", "double_click", "key", "wait"})

    # {
    #   責務: [
    #     explain: 候補ごとの許可・拒否理由を監査可能な形式で返す
    #   ]
    #   処理: [
    #     1: 同じ操作IDを持つ候補を特定して、IDの曖昧さを検出する
    #     2: 各候補の安全条件を評価して、理由と信頼度を記録する
    #   ]
    #   引数: [
    #     observation: 候補の座標を検証する画面観測
    #     candidates: 評価する操作候補
    #   ]
    #   戻り値: [
    #     report: 各候補の許可状態、理由、信頼度
    #   ]
    #   補足: [
    #     操作IDが重複する候補は、元の候補を特定できないため全件拒否する
    #   ]
    # }
    def explain(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> list[dict[str, object]]:
        """Return an auditable acceptance/rejection result for every candidate."""
        result = []
        action_id_counts = Counter(candidate.action_id for candidate in candidates)
        duplicate_action_ids = {
            action_id for action_id, count in action_id_counts.items() if count > 1
        }
        # 同じIDを先に拒否集合へ入れ、先頭候補の結果に依存しないようにする。
        seen: set[str] = set(duplicate_action_ids)
        for candidate in candidates:
            reason = self._rejection_reason(observation, candidate, seen)
            accepted = reason is None
            result.append({"action_id": candidate.action_id, "accepted": accepted, "reason": reason or "accepted", "confidence": candidate.confidence})
            if accepted:
                seen.add(candidate.action_id)
        return result

    def evaluate(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> list[ActionCandidate]:
        report = self.explain(observation, candidates)
        return [candidate for candidate, entry in zip(candidates, report) if entry["accepted"]]

    def _rejection_reason(self, observation: ScreenObservation, candidate: ActionCandidate, seen: set[str]) -> str | None:
        if candidate.action_id in seen:
            return "duplicate_action_id"
        if candidate.kind not in self.SUPPORTED:
            return "unsupported_kind"
        if candidate.dangerous:
            return "dangerous_action"
        if candidate.confidence < 0.5:
            return "low_confidence"
        if candidate.kind in {"click", "double_click"} and (candidate.x is None or candidate.y is None):
            return "missing_coordinates"
        if candidate.kind in {"click", "double_click"} and not (0 <= candidate.x < observation.width and 0 <= candidate.y < observation.height):
            return "outside_screen"
        return None
