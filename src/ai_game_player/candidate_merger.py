from ai_game_player.models import ActionCandidate


class CandidateMerger:
    def __init__(self, proximity: int = 20, iou_threshold: float = .5) -> None:
        if not 0 <= iou_threshold <= 1:
            raise ValueError("IoU threshold must be between 0 and 1")
        self.proximity = proximity
        self.iou_threshold = iou_threshold

    def merge(
        self,
        automation: list[ActionCandidate],
        ocr: list[ActionCandidate],
        image: list[ActionCandidate] | None = None,
    ) -> list[ActionCandidate]:
        result = list(automation)
        for candidates in (ocr, image or []):
            for candidate in candidates:
                if not any(self._same(existing, candidate) for existing in result):
                    result.append(candidate)
        return result

    def _same(self, left: ActionCandidate, right: ActionCandidate) -> bool:
        if left.action_id == right.action_id:
            return True
        if left.bbox is not None and right.bbox is not None and self._iou(left.bbox, right.bbox) >= self.iou_threshold:
            return True
        if left.x is None or left.y is None or right.x is None or right.y is None:
            return False
        return abs(left.x - right.x) <= self.proximity and abs(left.y - right.y) <= self.proximity

    def _iou(self, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        left_x, left_y, left_width, left_height = left
        right_x, right_y, right_width, right_height = right
        overlap_width = max(0, min(left_x + left_width, right_x + right_width) - max(left_x, right_x))
        overlap_height = max(0, min(left_y + left_height, right_y + right_height) - max(left_y, right_y))
        overlap = overlap_width * overlap_height
        return overlap / (left_width * left_height + right_width * right_height - overlap)