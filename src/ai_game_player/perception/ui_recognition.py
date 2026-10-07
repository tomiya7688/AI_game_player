import json
from dataclasses import dataclass
from hashlib import sha1
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame


class UiDetectorProvider(Protocol):
    name: str

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        ...


class DetectorProviderAdapter:
    """Adapts existing detectors such as BrightRegionDetector to a UI provider."""

    def __init__(self, name: str, detector: Any) -> None:
        self.name = name
        self.detector = detector

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        return list(self.detector.detect(frame))


@dataclass(frozen=True)
class UiProviderConfig:
    provider: UiDetectorProvider
    weight: float = 1.0
    enabled: bool = True
    generic: bool = True

    def __post_init__(self) -> None:
        if not isfinite(self.weight) or not 0 <= self.weight <= 1:
            raise ValueError("UI provider weight must be between 0 and 1")


@dataclass(frozen=True)
class UiProviderStatus:
    provider: str
    state: str
    generic: bool
    result_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class UiDetectionBatch:
    elements: tuple[DetectedElement, ...]
    statuses: tuple[UiProviderStatus, ...]
    generic_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "elements": [element.to_dict() for element in self.elements],
            "statuses": [status.to_dict() for status in self.statuses],
            "generic_used": self.generic_used,
        }


class UiDetectionPipeline:
    """Runs lightweight known-UI providers first and generic providers only when needed."""

    def __init__(self, providers: list[UiProviderConfig], known_threshold: float = 0.8, iou_threshold: float = 0.5) -> None:
        if not 0 <= known_threshold <= 1:
            raise ValueError("known threshold must be between 0 and 1")
        if not 0 <= iou_threshold <= 1:
            raise ValueError("IoU threshold must be between 0 and 1")
        self.providers = list(providers)
        self.known_threshold = known_threshold
        self.iou_threshold = iou_threshold

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        return list(self.detect_batch(frame).elements)

    def detect_batch(self, frame: ScreenFrame) -> UiDetectionBatch:
        lightweight = [config for config in self.providers if not config.generic]
        generic = [config for config in self.providers if config.generic]
        evidence, statuses = self._run(lightweight, frame)
        lightweight_elements = self._fuse(evidence)
        generic_used = not lightweight_elements or max(element.confidence for element in lightweight_elements) < self.known_threshold
        if generic_used:
            generic_evidence, generic_statuses = self._run(generic, frame)
            evidence.extend(generic_evidence)
            statuses.extend(generic_statuses)
        else:
            statuses.extend(self._skipped(generic))
        return UiDetectionBatch(tuple(self._fuse(evidence)), tuple(statuses), generic_used)

    def _run(self, configs: list[UiProviderConfig], frame: ScreenFrame) -> tuple[list[tuple[DetectedElement, float]], list[UiProviderStatus]]:
        evidence: list[tuple[DetectedElement, float]] = []
        statuses: list[UiProviderStatus] = []
        for config in configs:
            if not config.enabled:
                statuses.append(UiProviderStatus(config.provider.name, "disabled", config.generic))
                continue
            try:
                elements = config.provider.detect(frame)
                evidence.extend((element, config.weight) for element in elements)
                statuses.append(UiProviderStatus(config.provider.name, "success", config.generic, len(elements)))
            except Exception as exc:  # Providers are independent failure domains.
                statuses.append(UiProviderStatus(config.provider.name, "error", config.generic, error=str(exc)))
        return evidence, statuses

    def _skipped(self, configs: list[UiProviderConfig]) -> list[UiProviderStatus]:
        return [
            UiProviderStatus(config.provider.name, "disabled" if not config.enabled else "skipped", config.generic)
            for config in configs
        ]

    def _fuse(self, evidence: list[tuple[DetectedElement, float]]) -> list[DetectedElement]:
        groups: list[list[tuple[DetectedElement, float]]] = []
        for item in evidence:
            group = next((group for group in groups if self._same(group[0][0], item[0])), None)
            if group is None:
                groups.append([item])
            else:
                group.append(item)
        return [self._element(group) for group in groups]

    def _element(self, group: list[tuple[DetectedElement, float]]) -> DetectedElement:
        representative, _ = max(group, key=lambda item: item[0].confidence * item[1])
        remaining = 1.0
        for element, weight in group:
            remaining *= 1.0 - element.confidence * weight
        confidence = round(1.0 - remaining, 6)
        sources = "+".join(sorted({element.source for element, _ in group}))
        left, top, width, height = representative.bbox
        identity = f"{representative.element_type}|{self._normalize(representative.text)}|{left}|{top}|{width}|{height}"
        return DetectedElement(
            "ui-" + sha1(identity.encode("utf-8")).hexdigest()[:10],
            representative.element_type,
            representative.bbox,
            f"ui_fusion:{sources}",
            confidence,
            representative.text,
            representative.kind,
            any(element.dangerous for element, _ in group),
        )

    def _same(self, left: DetectedElement, right: DetectedElement) -> bool:
        if left.element_type != right.element_type or self._iou(left.bbox, right.bbox) < self.iou_threshold:
            return False
        left_text = self._normalize(left.text)
        right_text = self._normalize(right.text)
        return not left_text or not right_text or left_text == right_text

    def _normalize(self, text: str | None) -> str:
        return " ".join(text.casefold().split()) if text else ""

    def _iou(self, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        overlap_width = max(0, min(lx + lw, rx + rw) - max(lx, rx))
        overlap_height = max(0, min(ly + lh, ry + rh) - max(ly, ry))
        overlap = overlap_width * overlap_height
        union = lw * lh + rw * rh - overlap
        return overlap / union if union else 0.0


@dataclass(frozen=True)
class UiRecognitionSample:
    sample_id: str
    game_id: str
    visual_id: str
    element_type: str
    bbox_norm: tuple[float, float, float, float]
    source: str
    confidence: float
    sample_kind: str
    text: str | None = None
    interaction_id: str | None = None
    transition_id: str | None = None

    def __post_init__(self) -> None:
        if self.sample_kind not in {"positive", "hard_negative"}:
            raise ValueError("sample kind must be positive or hard_negative")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UiRecognitionSample":
        return cls(
            str(value["sample_id"]),
            str(value["game_id"]),
            str(value["visual_id"]),
            str(value["element_type"]),
            tuple(float(part) for part in value["bbox_norm"]),
            str(value["source"]),
            float(value["confidence"]),
            str(value["sample_kind"]),
            str(value["text"]) if value.get("text") is not None else None,
            str(value["interaction_id"]) if value.get("interaction_id") is not None else None,
            str(value["transition_id"]) if value.get("transition_id") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["bbox_norm"] = list(self.bbox_norm)
        return value


@dataclass(frozen=True)
class UiPrototype:
    prototype_id: str
    visual_id: str
    element_type: str
    bbox_norm: tuple[float, float, float, float]
    source_games: tuple[str, ...]
    confidence: float
    text: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UiPrototype":
        return cls(
            str(value["prototype_id"]),
            str(value["visual_id"]),
            str(value["element_type"]),
            tuple(float(part) for part in value["bbox_norm"]),
            tuple(str(game) for game in value["source_games"]),
            float(value["confidence"]),
            str(value["text"]) if value.get("text") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        value = self.__dict__.copy()
        value["bbox_norm"] = list(self.bbox_norm)
        value["source_games"] = list(self.source_games)
        return value


class UiRecognitionMemory:
    """Persists game-specific samples separately from validated cross-game prototypes."""

    def __init__(self, path: Path, fingerprint_grid: int = 4) -> None:
        if fingerprint_grid < 2:
            raise ValueError("fingerprint grid must be at least 2")
        self.path = path
        self.fingerprint_grid = fingerprint_grid

    def record(
        self,
        game_id: str,
        frame: ScreenFrame,
        element: DetectedElement,
        *,
        sample_kind: str = "positive",
        interaction_id: str | None = None,
        transition_id: str | None = None,
    ) -> UiRecognitionSample:
        sample = UiRecognitionSample(
            uuid4().hex,
            game_id,
            self._fingerprint(frame, element.bbox),
            element.element_type,
            self._normalize_bbox(frame, element.bbox),
            element.source,
            element.confidence,
            sample_kind,
            element.text,
            interaction_id,
            transition_id,
        )
        data = self._read()
        data["samples"].append(sample.to_dict())
        self._write(data)
        return sample

    def samples(self, game_id: str | None = None) -> list[UiRecognitionSample]:
        values = [UiRecognitionSample.from_dict(value) for value in self._read()["samples"]]
        return [value for value in values if game_id is None or value.game_id == game_id]

    def prototypes(self) -> list[UiPrototype]:
        return [UiPrototype.from_dict(value) for value in self._read()["prototypes"]]

    def detect_game(self, frame: ScreenFrame, game_id: str, similarity_threshold: float = 0.88) -> list[DetectedElement]:
        return self._detect_samples(frame, [sample for sample in self.samples(game_id) if sample.sample_kind == "positive"], similarity_threshold, f"ui_memory:{game_id}")

    def promote_cross_game(self, min_games: int = 2) -> list[UiPrototype]:
        if min_games < 2:
            raise ValueError("cross-game prototype requires at least two games")
        groups: dict[tuple[str, str], list[UiRecognitionSample]] = {}
        for sample in self.samples():
            if sample.sample_kind == "positive":
                groups.setdefault((sample.visual_id, sample.element_type), []).append(sample)
        promoted: list[UiPrototype] = []
        for (visual_id, element_type), samples in groups.items():
            games = tuple(sorted({sample.game_id for sample in samples}))
            if len(games) < min_games:
                continue
            bbox = tuple(sum(sample.bbox_norm[index] for sample in samples) / len(samples) for index in range(4))
            confidence = sum(sample.confidence for sample in samples) / len(samples)
            text = next((sample.text for sample in samples if sample.text), None)
            identity = f"{visual_id}|{element_type}|{'|'.join(games)}"
            promoted.append(UiPrototype("prototype-" + sha1(identity.encode("utf-8")).hexdigest()[:10], visual_id, element_type, bbox, games, round(confidence, 6), text))
        data = self._read()
        data["prototypes"] = [prototype.to_dict() for prototype in promoted]
        self._write(data)
        return promoted

    def detect_prototypes(self, frame: ScreenFrame, similarity_threshold: float = 0.92) -> list[DetectedElement]:
        result: list[DetectedElement] = []
        for prototype in self.prototypes():
            bbox = self._denormalize_bbox(frame, prototype.bbox_norm)
            similarity = self._similarity(prototype.visual_id, self._fingerprint(frame, bbox))
            if similarity < similarity_threshold:
                continue
            result.append(
                DetectedElement(
                    prototype.prototype_id,
                    prototype.element_type,
                    bbox,
                    "ui_cross_game_prototype",
                    round(min(1.0, prototype.confidence * similarity), 6),
                    prototype.text,
                )
            )
        return result

    def _detect_samples(self, frame: ScreenFrame, samples: list[UiRecognitionSample], threshold: float, source: str) -> list[DetectedElement]:
        result: list[DetectedElement] = []
        for sample in samples:
            bbox = self._denormalize_bbox(frame, sample.bbox_norm)
            similarity = self._similarity(sample.visual_id, self._fingerprint(frame, bbox))
            if similarity < threshold:
                continue
            result.append(
                DetectedElement(
                    "known-" + sample.sample_id[:10],
                    sample.element_type,
                    bbox,
                    source,
                    round(min(1.0, sample.confidence * similarity), 6),
                    sample.text,
                )
            )
        return result

    def _fingerprint(self, frame: ScreenFrame, bbox: tuple[int, int, int, int]) -> str:
        left, top, width, height = self._clamp_bbox(frame, bbox)
        values: list[str] = []
        for grid_y in range(self.fingerprint_grid):
            y = min(frame.height - 1, top + ((2 * grid_y + 1) * height) // (2 * self.fingerprint_grid))
            for grid_x in range(self.fingerprint_grid):
                x = min(frame.width - 1, left + ((2 * grid_x + 1) * width) // (2 * self.fingerprint_grid))
                offset = (y * frame.width + x) * 4
                blue, green, red = frame.bgra[offset : offset + 3]
                brightness = (red * 299 + green * 587 + blue * 114) // 1000
                values.append(format(min(15, brightness // 16), "x"))
        return "".join(values)

    def _similarity(self, left: str, right: str) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        distance = sum(abs(int(a, 16) - int(b, 16)) for a, b in zip(left, right))
        return 1.0 - distance / (15 * len(left))

    def _normalize_bbox(self, frame: ScreenFrame, bbox: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
        left, top, width, height = self._clamp_bbox(frame, bbox)
        return (left / frame.width, top / frame.height, width / frame.width, height / frame.height)

    def _denormalize_bbox(self, frame: ScreenFrame, bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        left = round(bbox[0] * frame.width)
        top = round(bbox[1] * frame.height)
        width = max(1, round(bbox[2] * frame.width))
        height = max(1, round(bbox[3] * frame.height))
        return self._clamp_bbox(frame, (left, top, width, height))

    def _clamp_bbox(self, frame: ScreenFrame, bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        left, top, width, height = bbox
        left = min(max(0, left), frame.width - 1)
        top = min(max(0, top), frame.height - 1)
        width = max(1, min(width, frame.width - left))
        height = max(1, min(height, frame.height - top))
        return left, top, width, height

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"samples": [], "prototypes": []}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("samples"), list) or not isinstance(value.get("prototypes"), list):
            raise ValueError("UI recognition memory must contain samples and prototypes arrays")
        return value

    def _write(self, value: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class KnownUiDetector:
    name = "known_ui"

    def __init__(self, memory: UiRecognitionMemory, game_id: str, similarity_threshold: float = 0.88) -> None:
        self.memory = memory
        self.game_id = game_id
        self.similarity_threshold = similarity_threshold

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        return self.memory.detect_game(frame, self.game_id, self.similarity_threshold)


class CrossGamePrototypeDetector:
    name = "cross_game_prototype"

    def __init__(self, memory: UiRecognitionMemory, similarity_threshold: float = 0.92) -> None:
        self.memory = memory
        self.similarity_threshold = similarity_threshold

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        return self.memory.detect_prototypes(frame, self.similarity_threshold)


@dataclass(frozen=True)
class UiTransferMetrics:
    reference_count: int
    detected_count: int
    true_positive: int
    false_positive: int
    missed: int
    precision: float
    recall: float


class UiTransferEvaluator:
    def __init__(self, iou_threshold: float = 0.5) -> None:
        self.iou_threshold = iou_threshold

    def evaluate(self, reference: list[DetectedElement], detected: list[DetectedElement]) -> UiTransferMetrics:
        matched: set[int] = set()
        true_positive = 0
        for expected in reference:
            index = next((index for index, actual in enumerate(detected) if index not in matched and self._same(expected, actual)), None)
            if index is not None:
                matched.add(index)
                true_positive += 1
        false_positive = len(detected) - true_positive
        missed = len(reference) - true_positive
        precision = true_positive / len(detected) if detected else (1.0 if not reference else 0.0)
        recall = true_positive / len(reference) if reference else 1.0
        return UiTransferMetrics(len(reference), len(detected), true_positive, false_positive, missed, precision, recall)

    def _same(self, left: DetectedElement, right: DetectedElement) -> bool:
        if left.element_type != right.element_type:
            return False
        if left.text and right.text and " ".join(left.text.casefold().split()) != " ".join(right.text.casefold().split()):
            return False
        return self._iou(left.bbox, right.bbox) >= self.iou_threshold

    def _iou(self, left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        overlap_width = max(0, min(lx + lw, rx + rw) - max(lx, rx))
        overlap_height = max(0, min(ly + lh, ry + rh) - max(ly, ry))
        overlap = overlap_width * overlap_height
        union = lw * lh + rw * rh - overlap
        return overlap / union if union else 0.0


@dataclass(frozen=True)
class UiDetectionImpact:
    recall: float
    decision_error: bool
    execution_error: bool


class UiDetectionImpactEvaluator:
    def __init__(self, transfer_evaluator: UiTransferEvaluator | None = None) -> None:
        self.transfer_evaluator = transfer_evaluator or UiTransferEvaluator()

    def assess(
        self,
        reference: list[DetectedElement],
        detected: list[DetectedElement],
        expected_action_id: str | None = None,
        decision_action_id: str | None = None,
        execution_action_id: str | None = None,
    ) -> UiDetectionImpact:
        metrics = self.transfer_evaluator.evaluate(reference, detected)
        decision_error = expected_action_id is not None and decision_action_id != expected_action_id
        execution_error = expected_action_id is not None and execution_action_id is not None and execution_action_id != expected_action_id
        return UiDetectionImpact(metrics.recall, decision_error, execution_error)
