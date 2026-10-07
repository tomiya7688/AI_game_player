from collections.abc import Sequence

from ai_game_player.models import ActionCandidate, DetectedElement


CandidateInput = ActionCandidate | DetectedElement
CandidateView = tuple[ActionCandidate, str, str | None]


class CandidateMerger:
    def __init__(self, proximity: int = 20, iou_threshold: float = .5) -> None:
        if not 0 <= iou_threshold <= 1:
            raise ValueError("IoU threshold must be between 0 and 1")
        self.proximity = proximity
        self.iou_threshold = iou_threshold

    def merge(
        self,
        automation: list[ActionCandidate],
        ocr: Sequence[CandidateInput],
        image: Sequence[CandidateInput] | None = None,
    ) -> list[ActionCandidate]:
        result: list[CandidateView] = [self._view(candidate, "automation") for candidate in automation]
        for group, candidates in (("ocr", ocr), ("image", image or [])):
            for value in candidates:
                incoming = self._view(value, group)
                duplicate = next((index for index, existing in enumerate(result) if self._same(existing, incoming)), None)
                if duplicate is None:
                    result.append(incoming)
                elif self._prefer(incoming, result[duplicate]):
                    result[duplicate] = incoming
        return [candidate for candidate, _, _ in result]

    def _view(self, value: CandidateInput, group: str) -> CandidateView:
        if isinstance(value, DetectedElement):
            left, top, width, height = value.bbox
            candidate = ActionCandidate(
                value.element_id,
                value.kind,
                value.text or value.element_type,
                left + width // 2,
                top + height // 2,
                value.confidence,
                value.dangerous,
                value.bbox,
            )
            return candidate, group, value.text
        text = None if group == "image" else value.label
        return value, group, text

    def _same(self, left: CandidateView, right: CandidateView) -> bool:
        left_candidate, left_group, left_text = left
        right_candidate, right_group, right_text = right
        if left_candidate.action_id == right_candidate.action_id:
            return True
        normalized_left = self._normalize_text(left_text)
        normalized_right = self._normalize_text(right_text)
        if left_candidate.bbox is not None and right_candidate.bbox is not None:
            if self._iou(left_candidate.bbox, right_candidate.bbox) >= self.iou_threshold:
                if normalized_left and normalized_right:
                    return normalized_left == normalized_right
                return True
        if left_candidate.x is None or left_candidate.y is None or right_candidate.x is None or right_candidate.y is None:
            return False
        if abs(left_candidate.x - right_candidate.x) > self.proximity or abs(left_candidate.y - right_candidate.y) > self.proximity:
            return False
        if normalized_left and normalized_right:
            return normalized_left == normalized_right
        return "image" in {left_group, right_group}

    def _prefer(self, incoming: CandidateView, existing: CandidateView) -> bool:
        incoming_candidate, incoming_group, _ = incoming
        existing_candidate, existing_group, _ = existing
        incoming_priority = self._priority(incoming_group)
        existing_priority = self._priority(existing_group)
        if incoming_priority != existing_priority:
            return incoming_priority > existing_priority
        return incoming_candidate.confidence > existing_candidate.confidence

    def _priority(self, group: str) -> int:
        return {"automation": 3, "ocr": 2, "image": 1}.get(group, 0)

    def _normalize_text(self, text: str | None) -> str:
        return " ".join(text.casefold().split()) if text else ""

    def _iou(self, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        left_x, left_y, left_width, left_height = left
        right_x, right_y, right_width, right_height = right
        overlap_width = max(0, min(left_x + left_width, right_x + right_width) - max(left_x, right_x))
        overlap_height = max(0, min(left_y + left_height, right_y + right_height) - max(left_y, right_y))
        overlap = overlap_width * overlap_height
        return overlap / (left_width * left_height + right_width * right_height - overlap)