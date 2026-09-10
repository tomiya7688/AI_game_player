from ai_game_player.models import ScreenObservation
from ai_game_player.perceptual_hasher import PerceptualHasher


class ScreenSimilarity:
    """Classifies two observations from their perceptual hashes when available."""

    def __init__(self, hasher: PerceptualHasher | None = None) -> None:
        self.hasher = hasher or PerceptualHasher()

    def compare(self, previous: ScreenObservation, current: ScreenObservation) -> dict[str, float | str]:
        if (previous.screen_id, previous.width, previous.height) != (current.screen_id, current.width, current.height):
            return self._result(0.0)
        previous_hash = previous.features.get("perceptual_hash")
        current_hash = current.features.get("perceptual_hash")
        if isinstance(previous_hash, str) and isinstance(current_hash, str):
            similarity = self.hasher.similarity(previous_hash, current_hash)
        else:
            similarity = self._fallback_similarity(previous, current)
        return self._result(similarity)

    def _fallback_similarity(self, previous: ScreenObservation, current: ScreenObservation) -> float:
        previous_signature = previous.features.get("signature")
        current_signature = current.features.get("signature")
        if isinstance(previous_signature, str) and isinstance(current_signature, str):
            return float(previous_signature == current_signature)
        return float(previous.ocr_text == current.ocr_text)

    def _result(self, similarity: float) -> dict[str, float | str]:
        if similarity == 1.0:
            category = "identical"
        elif similarity >= 0.95:
            category = "nearly_same"
        elif similarity >= 0.65:
            category = "partially_changed"
        else:
            category = "changed"
        return {"similarity": similarity, "difference_ratio": 1.0 - similarity, "category": category}