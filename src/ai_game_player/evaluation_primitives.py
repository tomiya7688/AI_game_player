import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from ai_game_player.outcome import OutcomeAssessment


PRIMITIVES = (
    "progress",
    "survival",
    "resources",
    "novelty",
    "repetition",
    "irreversible_loss",
)

DEFAULT_WEIGHTS: dict[str, float] = {
    "progress": 1.0,
    "survival": 0.8,
    "resources": 0.5,
    "novelty": 0.3,
    "repetition": 0.4,
    "irreversible_loss": 0.9,
}


@dataclass(frozen=True)
class EvaluationAxis:
    value: float
    confidence: float
    evidence: str

    def __post_init__(self) -> None:
        if not isfinite(self.value) or not -1.0 <= self.value <= 1.0:
            raise ValueError("evaluation axis value must be between -1 and 1")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evaluation axis confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, float | str]:
        return {"value": self.value, "confidence": self.confidence, "evidence": self.evidence}


@dataclass(frozen=True)
class EvaluationResult:
    axes: dict[str, EvaluationAxis]
    weights: dict[str, float]
    score: float
    confidence: float

    def __post_init__(self) -> None:
        if tuple(self.axes) != PRIMITIVES:
            raise ValueError("evaluation result must contain the six standard primitives in order")
        if set(self.weights) != set(PRIMITIVES):
            raise ValueError("evaluation weights must contain the six standard primitives")
        if not isfinite(self.score) or not -1.0 <= self.score <= 1.0:
            raise ValueError("evaluation score must be between -1 and 1")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evaluation confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "axes": {name: axis.to_dict() for name, axis in self.axes.items()},
            "weights": self.weights.copy(),
            "score": self.score,
            "confidence": self.confidence,
        }

    def to_decision_context(self) -> dict[str, object]:
        """Return the compact, structured representation consumed by Decision Context."""
        return {
            "evaluation_vector": {name: axis.value for name, axis in self.axes.items()},
            "evaluation_confidence": {name: axis.confidence for name, axis in self.axes.items()},
            "evaluation_score": self.score,
            "evaluation_score_confidence": self.confidence,
        }


class PrimitiveEvaluator:
    """Deterministically maps generic Outcome/state signals to reusable evaluation axes."""

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        self.weights = self._weights(weights)

    def evaluate(
        self,
        outcome: OutcomeAssessment,
        signals: Mapping[str, float] | None = None,
        weights: Mapping[str, float] | None = None,
    ) -> EvaluationResult:
        values = self._signals(signals)
        active_weights = self._weights(weights, self.weights)
        axes = {
            "progress": self._progress(outcome, values),
            "survival": self._survival(outcome, values),
            "resources": self._signal_axis(values, "resource_delta", "resources", signed=True),
            "novelty": self._signal_axis(values, "novelty", "novelty"),
            "repetition": self._penalty_axis(values, "repetition"),
            "irreversible_loss": self._penalty_axis(values, "irreversible_loss"),
        }
        weight_total = sum(active_weights.values())
        if weight_total == 0:
            score = 0.0
            confidence = 0.0
        else:
            score = sum(axes[name].value * active_weights[name] for name in PRIMITIVES) / weight_total
            confidence = sum(axes[name].confidence * active_weights[name] for name in PRIMITIVES) / weight_total
        return EvaluationResult(axes, active_weights, round(score, 6), round(confidence, 6))

    def _progress(self, outcome: OutcomeAssessment, signals: dict[str, float]) -> EvaluationAxis:
        if outcome.status == "success":
            return EvaluationAxis(1.0, outcome.confidence, outcome.reason)
        if outcome.status == "failure":
            return EvaluationAxis(-1.0, outcome.confidence, outcome.reason)
        if "progress_delta" in signals:
            return self._signal_axis(signals, "progress_delta", "progress", signed=True)
        return EvaluationAxis(0.0, min(outcome.confidence, 0.5), "no generic progress signal")

    def _survival(self, outcome: OutcomeAssessment, signals: dict[str, float]) -> EvaluationAxis:
        if outcome.status == "failure":
            return EvaluationAxis(-1.0, outcome.confidence, outcome.reason)
        if outcome.status == "success":
            return EvaluationAxis(1.0, outcome.confidence, outcome.reason)
        if "survival" in signals:
            return self._signal_axis(signals, "survival", "survival", signed=True)
        return EvaluationAxis(0.5, min(outcome.confidence, 0.5), "outcome remains ongoing")

    def _signal_axis(self, signals: dict[str, float], key: str, axis: str, signed: bool = False) -> EvaluationAxis:
        if key not in signals:
            return EvaluationAxis(0.0, 0.0, f"no {key} signal")
        value = signals[key]
        if not signed:
            value = max(0.0, value)
        confidence = signals.get(f"{axis}_confidence", signals.get(f"{key}_confidence", 1.0))
        return EvaluationAxis(self._clamp(value), self._confidence(confidence), f"generic signal: {key}")

    def _penalty_axis(self, signals: dict[str, float], key: str) -> EvaluationAxis:
        if key not in signals:
            return EvaluationAxis(0.0, 0.0, f"no {key} signal")
        magnitude = max(0.0, signals[key])
        confidence = signals.get(f"{key}_confidence", 1.0)
        return EvaluationAxis(-self._clamp(magnitude), self._confidence(confidence), f"generic penalty signal: {key}")

    def _signals(self, signals: Mapping[str, float] | None) -> dict[str, float]:
        result: dict[str, float] = {}
        for key, raw in (signals or {}).items():
            value = float(raw)
            if not isfinite(value):
                raise ValueError(f"signal must be finite: {key}")
            result[str(key)] = value
        return result

    def _weights(self, override: Mapping[str, float] | None, base: Mapping[str, float] | None = None) -> dict[str, float]:
        result = dict(base or DEFAULT_WEIGHTS)
        for key, raw in (override or {}).items():
            if key not in PRIMITIVES:
                raise ValueError(f"unknown evaluation primitive: {key}")
            value = float(raw)
            if not isfinite(value) or value < 0:
                raise ValueError(f"weight must be a finite non-negative number: {key}")
            result[key] = value
        return result

    @staticmethod
    def _clamp(value: float) -> float:
        return max(-1.0, min(1.0, value))

    @staticmethod
    def _confidence(value: float) -> float:
        if not isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("signal confidence must be between 0 and 1")
        return value


class EvaluationLog:
    """Small append-only JSON log for auditable primitive scores."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, result: EvaluationResult, metadata: Mapping[str, object] | None = None) -> None:
        entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries, list):
            raise ValueError("evaluation log must contain an array")
        entries.append({"evaluation": result.to_dict(), "metadata": dict(metadata or {})})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
