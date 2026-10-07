from collections import deque
from dataclasses import dataclass
from hashlib import sha1
from math import isfinite
from typing import Any, Protocol

from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame


@dataclass(frozen=True)
class OcrResult:
    text: str
    bbox: tuple[int, int, int, int]
    confidence: float
    source: str
    model: str
    preprocessing: str = "default"
    frame_id: str = ""
    kind: str = "click"
    dangerous: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("OCR text must not be empty")
        if len(self.bbox) != 4 or self.bbox[2] <= 0 or self.bbox[3] <= 0:
            raise ValueError("OCR bbox must be (x, y, positive_width, positive_height)")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("OCR confidence must be between 0 and 1")
        if not self.source.strip():
            raise ValueError("OCR source must not be empty")
        if not self.model.strip():
            raise ValueError("OCR model must not be empty")
        if not self.preprocessing.strip():
            raise ValueError("OCR preprocessing must not be empty")

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        source: str,
        model: str,
        preprocessing: str = "default",
        frame_id: str = "",
    ) -> "OcrResult":
        x = int(value.get("x", 0))
        y = int(value.get("y", 0))
        width = int(value.get("width", 0))
        height = int(value.get("height", 0))
        return cls(
            str(value.get("text", "")).strip(),
            (x, y, width, height),
            float(value.get("confidence", 0.0)),
            str(value.get("source", source)),
            str(value.get("model", model)),
            str(value.get("preprocessing", preprocessing)),
            str(value.get("frame_id", frame_id)),
            str(value.get("kind", "click")),
            bool(value.get("dangerous", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class OcrProvider(Protocol):
    name: str
    model: str
    preprocessing: str

    def recognize(self, frame: ScreenFrame, frame_id: str = "") -> list[OcrResult]:
        ...


class RecognizerOcrProvider:
    """Adapts the existing dict-based OCR recognizers to the provider contract."""

    def __init__(self, recognizer: Any, name: str, model: str, preprocessing: str = "default") -> None:
        self.recognizer = recognizer
        self.name = name
        self.model = model
        self.preprocessing = preprocessing

    def recognize(self, frame: ScreenFrame, frame_id: str = "") -> list[OcrResult]:
        values = self.recognizer.recognize(frame)
        return [
            OcrResult.from_dict(
                value,
                source=self.name,
                model=self.model,
                preprocessing=self.preprocessing,
                frame_id=frame_id,
            )
            for value in values
            if isinstance(value, dict) and str(value.get("text", "")).strip()
        ]


@dataclass(frozen=True)
class OcrProviderConfig:
    provider: OcrProvider
    weight: float = 1.0
    enabled: bool = True
    fallback: bool = False

    def __post_init__(self) -> None:
        if not isfinite(self.weight) or not 0 <= self.weight <= 1:
            raise ValueError("OCR provider weight must be between 0 and 1")


@dataclass(frozen=True)
class OcrProviderStatus:
    provider: str
    model: str
    state: str
    fallback: bool
    result_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class OcrBatch:
    frame_id: str
    results: tuple[OcrResult, ...]
    elements: tuple[DetectedElement, ...]
    statuses: tuple[OcrProviderStatus, ...]
    fallback_used: bool

    def to_candidate_dicts(self) -> list[dict[str, Any]]:
        values = []
        for element in self.elements:
            x, y, width, height = element.bbox
            values.append(
                {
                    "action_id": element.element_id,
                    "text": element.text or "",
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "confidence": element.confidence,
                    "source": element.source,
                    "kind": element.kind,
                    "dangerous": element.dangerous,
                }
            )
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "results": [value.to_dict() for value in self.results],
            "elements": [value.to_dict() for value in self.elements],
            "statuses": [value.to_dict() for value in self.statuses],
            "fallback_used": self.fallback_used,
        }


class OcrFusionPipeline:
    """Runs interchangeable OCR providers and fuses agreeing spatial/text evidence."""

    def __init__(
        self,
        providers: list[OcrProviderConfig],
        fallback_threshold: float = 0.55,
        iou_threshold: float = 0.4,
        temporal_window: int = 3,
    ) -> None:
        if not 0 <= fallback_threshold <= 1:
            raise ValueError("fallback threshold must be between 0 and 1")
        if not 0 <= iou_threshold <= 1:
            raise ValueError("IoU threshold must be between 0 and 1")
        if temporal_window < 1:
            raise ValueError("temporal window must be positive")
        self.providers = list(providers)
        self.fallback_threshold = fallback_threshold
        self.iou_threshold = iou_threshold
        self._history: deque[tuple[OcrResult, ...]] = deque(maxlen=temporal_window - 1 if temporal_window > 1 else 0)
        self._frame_index = 0
        self._weights = {
            (config.provider.name, config.provider.model, config.provider.preprocessing): config.weight
            for config in self.providers
        }

    def recognize(self, frame: ScreenFrame, frame_id: str = "") -> list[dict[str, Any]]:
        return self.recognize_batch(frame, frame_id).to_candidate_dicts()

    def recognize_batch(self, frame: ScreenFrame, frame_id: str = "") -> OcrBatch:
        self._frame_index += 1
        resolved_frame_id = frame_id or f"frame-{self._frame_index}"
        primary = [config for config in self.providers if not config.fallback]
        fallback = [config for config in self.providers if config.fallback]
        primary_results, statuses = self._run(primary, frame, resolved_frame_id)
        elements = self._fuse(primary_results)
        fallback_used = bool(fallback) and self._needs_fallback(elements)
        all_results = list(primary_results)
        if fallback_used:
            fallback_results, fallback_statuses = self._run(fallback, frame, resolved_frame_id)
            all_results.extend(fallback_results)
            statuses.extend(fallback_statuses)
            elements = self._fuse(all_results)
        else:
            statuses.extend(self._not_run_statuses(fallback))
        if self._history.maxlen:
            self._history.append(tuple(all_results))
        return OcrBatch(resolved_frame_id, tuple(all_results), tuple(elements), tuple(statuses), fallback_used)

    def _run(
        self,
        configs: list[OcrProviderConfig],
        frame: ScreenFrame,
        frame_id: str,
    ) -> tuple[list[OcrResult], list[OcrProviderStatus]]:
        results: list[OcrResult] = []
        statuses: list[OcrProviderStatus] = []
        for config in configs:
            provider = config.provider
            if not config.enabled:
                statuses.append(OcrProviderStatus(provider.name, provider.model, "disabled", config.fallback))
                continue
            try:
                provider_results = provider.recognize(frame, frame_id)
                results.extend(provider_results)
                statuses.append(
                    OcrProviderStatus(provider.name, provider.model, "success", config.fallback, len(provider_results))
                )
            except Exception as exc:  # Provider failure must not take down independent OCR providers.
                statuses.append(OcrProviderStatus(provider.name, provider.model, "error", config.fallback, error=str(exc)))
        return results, statuses

    def _not_run_statuses(self, configs: list[OcrProviderConfig]) -> list[OcrProviderStatus]:
        return [
            OcrProviderStatus(
                config.provider.name,
                config.provider.model,
                "disabled" if not config.enabled else "skipped",
                config.fallback,
            )
            for config in configs
        ]

    def _needs_fallback(self, elements: list[DetectedElement]) -> bool:
        return not elements or max(element.confidence for element in elements) < self.fallback_threshold

    def _fuse(self, results: list[OcrResult]) -> list[DetectedElement]:
        groups: list[list[OcrResult]] = []
        for result in results:
            group = next((value for value in groups if self._same(value[0], result)), None)
            if group is None:
                groups.append([result])
            else:
                group.append(result)
        return [self._element(group) for group in groups]

    def _element(self, group: list[OcrResult]) -> DetectedElement:
        evidence: dict[tuple[str, str, str], OcrResult] = {}
        for result in group:
            key = (result.source, result.model, result.preprocessing)
            if key not in evidence or result.confidence > evidence[key].confidence:
                evidence[key] = result
        values = list(evidence.values())
        representative = max(values, key=lambda result: result.confidence * self._weight(result))
        remaining = 1.0
        for result in values:
            remaining *= 1.0 - result.confidence * self._weight(result)
        confidence = 1.0 - remaining
        confidence = min(1.0, confidence + 0.04 * self._temporal_support(representative))
        sources = "+".join(sorted({result.source for result in values}))
        normalized = self._normalize(representative.text)
        x, y, width, height = representative.bbox
        identity = f"{normalized}|{x // 8}|{y // 8}|{width // 8}|{height // 8}"
        element_id = "ocr-" + sha1(identity.encode("utf-8")).hexdigest()[:10]
        return DetectedElement(
            element_id,
            "text",
            representative.bbox,
            f"ocr_fusion:{sources}",
            round(confidence, 6),
            representative.text,
            representative.kind,
            representative.dangerous,
        )

    def _weight(self, result: OcrResult) -> float:
        return self._weights.get((result.source, result.model, result.preprocessing), 1.0)

    def _temporal_support(self, result: OcrResult) -> int:
        support = 0
        for frame_results in self._history:
            if any(self._same(previous, result) for previous in frame_results):
                support += 1
        return support

    def _same(self, left: OcrResult, right: OcrResult) -> bool:
        return self._normalize(left.text) == self._normalize(right.text) and self._iou(left.bbox, right.bbox) >= self.iou_threshold

    def _normalize(self, text: str) -> str:
        return " ".join(text.casefold().split())

    def _iou(self, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        left_x, left_y, left_width, left_height = left
        right_x, right_y, right_width, right_height = right
        overlap_width = max(0, min(left_x + left_width, right_x + right_width) - max(left_x, right_x))
        overlap_height = max(0, min(left_y + left_height, right_y + right_height) - max(left_y, right_y))
        overlap = overlap_width * overlap_height
        union = left_width * left_height + right_width * right_height - overlap
        return overlap / union if union else 0.0


@dataclass(frozen=True)
class OcrDecisionImpact:
    recognition_error: bool
    decision_error: bool
    execution_error: bool
    impact: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class OcrDecisionImpactEvaluator:
    """Separates OCR mismatch from downstream wrong-decision/wrong-execution impact."""

    def assess(
        self,
        reference_text: str,
        observed_text: str,
        expected_action_id: str | None = None,
        decision_action_id: str | None = None,
        execution_action_id: str | None = None,
    ) -> OcrDecisionImpact:
        recognition_error = self._normalize(reference_text) != self._normalize(observed_text)
        decision_error = expected_action_id is not None and decision_action_id != expected_action_id
        execution_error = expected_action_id is not None and execution_action_id is not None and execution_action_id != expected_action_id
        if execution_error:
            impact = "execution_error"
        elif decision_error:
            impact = "decision_error"
        elif recognition_error:
            impact = "recognition_only"
        else:
            impact = "none"
        return OcrDecisionImpact(recognition_error, decision_error, execution_error, impact)

    def _normalize(self, text: str) -> str:
        return " ".join(text.casefold().split())
