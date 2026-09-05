from ai_game_player.models import ActionCandidate, ScreenObservation


class ActionEvaluator:
    SUPPORTED = frozenset({"click", "double_click", "key", "wait"})

    def explain(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> list[dict[str, object]]:
        """Return an auditable acceptance/rejection result for every candidate."""
        result = []
        seen: set[str] = set()
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