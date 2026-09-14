import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


@dataclass(frozen=True)
class EvaluatorEvidence:
    evaluator: str
    score: float
    confidence: float
    reliability: float
    evidence: str

    def __post_init__(self) -> None:
        if not self.evaluator.strip():
            raise ValueError("evaluator name must not be empty")
        if not isfinite(self.score) or not -1.0 <= self.score <= 1.0:
            raise ValueError("evaluation score must be between -1 and 1")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evaluation confidence must be between 0 and 1")
        if not isfinite(self.reliability) or not 0.0 <= self.reliability <= 1.0:
            raise ValueError("evaluator reliability must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class KnowledgeEvidence:
    evidence_id: str
    source: str
    statement: str
    confidence: float
    provenance: str
    category: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.source.strip() or not self.provenance.strip():
            raise ValueError("knowledge evidence metadata must not be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("knowledge confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class CandidateDecisionContext:
    action_id: str
    kind: str
    label: str
    recognition_confidence: float
    allowed: bool
    evaluations: tuple[EvaluatorEvidence, ...]
    utility_score: float
    utility_confidence: float
    evaluator_conflict: bool
    knowledge: tuple[KnowledgeEvidence, ...]
    uncertainty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "label": self.label,
            "recognition_confidence": self.recognition_confidence,
            "allowed": self.allowed,
            "evaluation": {
                "score": self.utility_score,
                "confidence": self.utility_confidence,
                "conflict": self.evaluator_conflict,
                "evidence": [value.to_dict() for value in self.evaluations],
            },
            "knowledge": [value.to_dict() for value in self.knowledge],
            "uncertainty": list(self.uncertainty),
        }


@dataclass(frozen=True)
class DecisionContext:
    snapshot_id: str
    state: dict[str, Any]
    candidates: tuple[CandidateDecisionContext, ...]
    allowed_action_ids: tuple[str, ...]
    previous_outcome: dict[str, Any]
    recent_history: tuple[dict[str, Any], ...]
    goal: dict[str, str]
    uncertainty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "decision-context/v1",
            "snapshot_id": self.snapshot_id,
            "state": self.state,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "allowed_action_ids": list(self.allowed_action_ids),
            "previous_outcome": self.previous_outcome,
            "recent_history": list(self.recent_history),
            "goal": self.goal,
            "uncertainty": list(self.uncertainty),
        }


class CandidateContextEvaluator(Protocol):
    name: str
    reliability: float

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        ...


class SafetyContextEvaluator:
    name = "safety"
    reliability = 1.0

    def __init__(self, evaluator: ActionEvaluator | None = None) -> None:
        self.evaluator = evaluator or ActionEvaluator()

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        report = self.evaluator.explain(observation, [candidate])[0]
        accepted = bool(report["accepted"])
        return EvaluatorEvidence(
            self.name,
            0.0 if accepted else -1.0,
            1.0,
            self.reliability,
            str(report["reason"]),
        )


class RepetitionContextEvaluator:
    name = "repetition"
    reliability = 0.9

    def __init__(self, window: int = 5) -> None:
        if window < 1:
            raise ValueError("repetition window must be positive")
        self.window = window

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        history = recent_history[-self.window :]
        repeats = sum(1 for entry in history if str(entry.get("action_id", "")) == candidate.action_id)
        if repeats == 0:
            return EvaluatorEvidence(self.name, 0.0, 0.8, self.reliability, "action not present in recent history")
        state_changed = _state_changed_from_history(observation, history[-1] if history else None)
        stalled = previous_outcome.status in {"failure", "ongoing", "unknown"} and not state_changed
        penalty = min(1.0, 0.25 * repeats + (0.5 if stalled else 0.0))
        reason = f"recent repeats={repeats}; state_changed={state_changed}; previous_outcome={previous_outcome.status}"
        return EvaluatorEvidence(self.name, -penalty, max(0.5, previous_outcome.confidence), self.reliability, reason)


class EvaluationFusion:
    def __init__(self, conflict_threshold: float = 0.35) -> None:
        if not 0 <= conflict_threshold <= 1:
            raise ValueError("conflict threshold must be between 0 and 1")
        self.conflict_threshold = conflict_threshold

    def fuse(self, evaluations: list[EvaluatorEvidence]) -> tuple[float, float, bool]:
        weighted = [(entry, entry.confidence * entry.reliability) for entry in evaluations]
        denominator = sum(weight for _, weight in weighted)
        if denominator == 0:
            score = 0.0
            confidence = 0.0
        else:
            score = sum(entry.score * weight for entry, weight in weighted) / denominator
            confidence = denominator / max(1.0, sum(entry.reliability for entry in evaluations))
        positive = any(entry.score >= self.conflict_threshold and entry.confidence > 0 for entry in evaluations)
        negative = any(entry.score <= -self.conflict_threshold and entry.confidence > 0 for entry in evaluations)
        return round(score, 6), round(min(1.0, confidence), 6), positive and negative


class CandidateKnowledgeRetriever:
    def __init__(self, store: KnowledgeStore | None = None, limit: int = 3) -> None:
        if limit < 0:
            raise ValueError("knowledge limit must not be negative")
        self.store = store
        self.limit = limit

    def retrieve(self, candidate: ActionCandidate) -> list[KnowledgeEvidence]:
        if self.store is None or self.limit == 0:
            return []
        queries = [candidate.label, candidate.action_id]
        matches: list[dict[str, object]] = []
        seen: set[str] = set()
        for query in queries:
            if not query.strip():
                continue
            for entry in self.store.search(query):
                evidence_id = str(entry.get("id", ""))
                if not evidence_id or evidence_id in seen:
                    continue
                seen.add(evidence_id)
                matches.append(entry)
                if len(matches) >= self.limit:
                    break
            if len(matches) >= self.limit:
                break
        provenance = str(self.store.path)
        return [
            KnowledgeEvidence(
                str(entry["id"]),
                "knowledge_store",
                str(entry.get("statement", "")),
                _confidence(entry.get("confidence", 0.0)),
                provenance,
                str(entry.get("category", "")),
            )
            for entry in matches
        ]


class DecisionContextBuilder:
    """Builds the compact state/evaluation/knowledge package consumed by a decision provider."""

    def __init__(
        self,
        knowledge_store: KnowledgeStore | None = None,
        evaluators: list[CandidateContextEvaluator] | None = None,
        fusion: EvaluationFusion | None = None,
        history_limit: int = 5,
        knowledge_limit: int = 3,
    ) -> None:
        if history_limit < 0:
            raise ValueError("history limit must not be negative")
        self.evaluators = list(evaluators) if evaluators is not None else [SafetyContextEvaluator(), RepetitionContextEvaluator()]
        self.fusion = fusion or EvaluationFusion()
        self.history_limit = history_limit
        self.knowledge = CandidateKnowledgeRetriever(knowledge_store, knowledge_limit)

    def build(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        allowed_candidates: list[ActionCandidate],
        *,
        recent_history: list[dict[str, Any]] | None = None,
        previous_outcome: OutcomeAssessment | None = None,
        current_goal: str = "",
        short_term_goal: str = "",
    ) -> DecisionContext:
        history = list(recent_history or [])[-self.history_limit :] if self.history_limit else []
        outcome = previous_outcome or OutcomeAssessment("unknown", 0.0, "no previous action")
        allowed_ids = {candidate.action_id for candidate in allowed_candidates}
        candidate_contexts: list[CandidateDecisionContext] = []
        global_uncertainty: list[str] = []
        for candidate in candidates:
            evaluations = [evaluator.evaluate(observation, candidate, history, outcome) for evaluator in self.evaluators]
            score, confidence, conflict = self.fusion.fuse(evaluations)
            knowledge = self.knowledge.retrieve(candidate)
            uncertainty: list[str] = []
            if conflict:
                uncertainty.append("evaluator_conflict")
            if confidence < 0.5:
                uncertainty.append("low_evaluation_confidence")
            if candidate.confidence < 0.6:
                uncertainty.append("low_recognition_confidence")
            if not evaluations:
                uncertainty.append("missing_evaluation")
            if not knowledge:
                uncertainty.append("no_candidate_knowledge")
            candidate_contexts.append(
                CandidateDecisionContext(
                    candidate.action_id,
                    candidate.kind,
                    candidate.label,
                    candidate.confidence,
                    candidate.action_id in allowed_ids,
                    tuple(evaluations),
                    score,
                    confidence,
                    conflict,
                    tuple(knowledge),
                    tuple(uncertainty),
                )
            )
        if not allowed_ids:
            global_uncertainty.append("no_allowed_actions")
        if any(candidate.evaluator_conflict for candidate in candidate_contexts):
            global_uncertainty.append("candidate_evaluator_conflict")
        if outcome.confidence < 0.5:
            global_uncertainty.append("previous_outcome_uncertain")
        return DecisionContext(
            uuid4().hex,
            _state_summary(observation),
            tuple(candidate_contexts),
            tuple(candidate.action_id for candidate in allowed_candidates),
            {"status": outcome.status, "confidence": outcome.confidence, "reason": outcome.reason, "state_changed": _state_changed_from_history(observation, history[-1] if history else None)},
            tuple(_history_summary(entry) for entry in history),
            {"current_goal": current_goal, "short_term_goal": short_term_goal},
            tuple(global_uncertainty),
        )


class DecisionTraceStore:
    """Append-only trace that binds one observation snapshot to context, decision and previous outcome."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, context: DecisionContext, decision: ActionDecision) -> None:
        entries = self._read()
        entries.append(
            {
                "snapshot_id": context.snapshot_id,
                "screen_id": context.state.get("screen_id", ""),
                "state_signature": context.state.get("signature", ""),
                "action_id": decision.action_id,
                "decision": decision.to_dict(),
                "previous_outcome": context.previous_outcome,
                "used_evidence_ids": [
                    evidence.evidence_id
                    for candidate in context.candidates
                    if candidate.action_id == decision.action_id
                    for evidence in candidate.knowledge
                ],
                "context": context.to_dict(),
            }
        )
        self._write(entries)

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("history limit must not be negative")
        return self._read()[-limit:] if limit else []

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("decision trace must contain an array")
        return [entry for entry in value if isinstance(entry, dict)]

    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def _state_summary(observation: ScreenObservation) -> dict[str, Any]:
    features = observation.features
    visible_text: list[str] = []
    remaining = 160
    for raw in observation.ocr_text[:8]:
        text = " ".join(str(raw).split())
        if not text or remaining <= 0:
            continue
        text = text[:remaining]
        visible_text.append(text)
        remaining -= len(text)
    detected = features.get("detected_elements", [])
    return {
        "screen_id": observation.screen_id,
        "size": [observation.width, observation.height],
        "signature": str(features.get("signature", "")),
        "perceptual_hash": str(features.get("perceptual_hash", "")),
        "mean_brightness": features.get("mean_brightness"),
        "visible_text": visible_text,
        "detected_element_count": len(detected) if isinstance(detected, list) else 0,
    }


def _history_summary(entry: dict[str, Any]) -> dict[str, Any]:
    previous = entry.get("previous_outcome", {})
    return {
        "snapshot_id": str(entry.get("snapshot_id", "")),
        "screen_id": str(entry.get("screen_id", "")),
        "state_signature": str(entry.get("state_signature", "")),
        "action_id": str(entry.get("action_id", "")),
        "outcome": str(previous.get("status", "unknown")) if isinstance(previous, dict) else "unknown",
        "state_changed": bool(previous.get("state_changed", False)) if isinstance(previous, dict) else False,
    }


def _state_changed_from_history(observation: ScreenObservation, previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    current_signature = str(observation.features.get("signature", ""))
    previous_signature = str(previous.get("state_signature", ""))
    if current_signature and previous_signature:
        return current_signature != previous_signature
    current_hash = str(observation.features.get("perceptual_hash", ""))
    previous_context = previous.get("context", {})
    previous_state = previous_context.get("state", {}) if isinstance(previous_context, dict) else {}
    previous_hash = str(previous_state.get("perceptual_hash", "")) if isinstance(previous_state, dict) else ""
    if current_hash and previous_hash:
        return current_hash != previous_hash
    return str(previous.get("screen_id", "")) != observation.screen_id


def _confidence(value: object) -> float:
    result = float(value)
    if not isfinite(result):
        return 0.0
    return max(0.0, min(1.0, result))
