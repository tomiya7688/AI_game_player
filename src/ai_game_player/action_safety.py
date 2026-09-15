import json
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_game_player.models import ActionCandidate, ScreenObservation


class SafetyStatus(str, Enum):
    SAFE = "SAFE"
    SUSPICIOUS = "SUSPICIOUS"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class SafetyEvidence:
    check: str
    severity: str
    confidence: float
    message: str
    source: str = "action_safety_rule/v1"

    def __post_init__(self) -> None:
        if not self.check.strip() or not self.message.strip() or not self.source.strip():
            raise ValueError("safety evidence metadata must not be empty")
        if self.severity not in {"info", "warning", "block"}:
            raise ValueError("safety evidence severity must be info, warning, or block")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("safety evidence confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class SafetyEvaluationContext:
    current_goal: str = ""
    short_term_goal: str = ""
    expected_effect_consistent: bool | None = None
    utility_score: float | None = None
    utility_confidence: float | None = None
    target_scope_hint: str | None = None
    reversible_hint: bool | None = None

    def __post_init__(self) -> None:
        if self.utility_score is not None and (not isfinite(self.utility_score) or not -1.0 <= self.utility_score <= 1.0):
            raise ValueError("utility score must be between -1 and 1")
        if self.utility_confidence is not None and (
            not isfinite(self.utility_confidence) or not 0.0 <= self.utility_confidence <= 1.0
        ):
            raise ValueError("utility confidence must be between 0 and 1")
        if self.target_scope_hint is not None and self.target_scope_hint not in {"local", "game", "session", "system"}:
            raise ValueError("target scope hint must be local, game, session, or system")


@dataclass(frozen=True)
class ActionSafetyResult:
    assessment_id: str
    action_id: str
    status: SafetyStatus
    recognition_confidence: float
    safety_score: float
    risk_score: float
    risk_level: str
    reversible: bool | None
    blast_radius: str
    target_scope: str
    goal_alignment: bool | None
    expected_effect_consistent: bool | None
    requires_verification: bool
    verification_requests: tuple[str, ...]
    upstream_anomaly: bool
    evidence: tuple[SafetyEvidence, ...]

    def __post_init__(self) -> None:
        if not self.assessment_id.strip() or not self.action_id.strip():
            raise ValueError("safety result identity must not be empty")
        for name, value in (
            ("recognition_confidence", self.recognition_confidence),
            ("safety_score", self.safety_score),
            ("risk_score", self.risk_score),
        ):
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid risk level")
        if self.blast_radius not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid blast radius")
        if self.target_scope not in {"local", "game", "session", "system"}:
            raise ValueError("invalid target scope")
        if self.status == SafetyStatus.SAFE and self.requires_verification:
            raise ValueError("SAFE result cannot require verification")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "action-safety/v1",
            "assessment_id": self.assessment_id,
            "action_id": self.action_id,
            "status": self.status.value,
            "recognition_confidence": self.recognition_confidence,
            "safety_score": self.safety_score,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "blast_radius": self.blast_radius,
            "target_scope": self.target_scope,
            "goal_alignment": self.goal_alignment,
            "expected_effect_consistent": self.expected_effect_consistent,
            "requires_verification": self.requires_verification,
            "verification_requests": list(self.verification_requests),
            "upstream_anomaly": self.upstream_anomaly,
            "evidence": [entry.to_dict() for entry in self.evidence],
        }


class ActionSafetyEvaluator:
    """Semantic/invariant pre-execution evaluator. It is not the final #33 hard guard."""

    SUPPORTED_KINDS = frozenset({"click", "double_click", "key", "wait"})
    IRREVERSIBLE_TERMS = (
        "delete save",
        "erase save",
        "wipe save",
        "overwrite save",
        "reset save",
        "delete data",
        "erase data",
        "new game overwrite",
    )
    SESSION_TERMS = (
        "quit game",
        "exit game",
        "close game",
        "restart game",
        "reset game",
        "return to desktop",
    )
    TRANSACTION_TERMS = (
        "confirm purchase",
        "real money",
        "spend currency",
        "buy with",
    )
    SYSTEM_KEY_TERMS = (
        "alt+f4",
        "alt f4",
        "ctrl+alt+delete",
        "ctrl alt delete",
        "windows key",
        "win+",
    )

    def __init__(self, suspicious_threshold: float = 0.45, high_risk_threshold: float = 0.65) -> None:
        if not 0.0 <= suspicious_threshold <= high_risk_threshold <= 1.0:
            raise ValueError("safety thresholds must satisfy 0 <= suspicious <= high-risk <= 1")
        self.suspicious_threshold = suspicious_threshold
        self.high_risk_threshold = high_risk_threshold

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        context: SafetyEvaluationContext | None = None,
    ) -> ActionSafetyResult:
        context = context or SafetyEvaluationContext()
        evidence: list[SafetyEvidence] = []
        verification: list[str] = []
        risk_score = 0.05
        reversible: bool | None = context.reversible_hint
        blast_radius = "low"
        target_scope = context.target_scope_hint or "local"
        goal_alignment: bool | None = None

        def add(check: str, severity: str, confidence: float, message: str) -> None:
            evidence.append(SafetyEvidence(check, severity, confidence, message))

        if candidate.kind not in self.SUPPORTED_KINDS:
            add("action_schema", "block", 1.0, f"unsupported action kind: {candidate.kind}")
            risk_score = 1.0

        if candidate.kind in {"click", "double_click"}:
            if candidate.x is None or candidate.y is None:
                add("spatial_invariant", "block", 1.0, "click action has no target coordinates")
                risk_score = 1.0
            elif not (0 <= candidate.x < observation.width and 0 <= candidate.y < observation.height):
                add("spatial_invariant", "block", 1.0, "target coordinates are outside the current screen")
                risk_score = 1.0
            if candidate.bbox is not None:
                left, top, width, height = candidate.bbox
                if left < 0 or top < 0 or left + width > observation.width or top + height > observation.height:
                    add("spatial_invariant", "block", 1.0, "candidate bounding box crosses the current screen boundary")
                    risk_score = 1.0
                elif candidate.x is not None and candidate.y is not None and not (
                    left <= candidate.x < left + width and top <= candidate.y < top + height
                ):
                    add("target_consistency", "warning", 0.95, "target coordinate is outside its recognized bounding box")
                    verification.append("reobserve_target")
                    risk_score = max(risk_score, 0.5)

        if candidate.dangerous:
            add("explicit_danger", "block", 1.0, "candidate is explicitly marked dangerous")
            risk_score = 1.0
            reversible = False
            blast_radius = "critical"
            target_scope = "game"

        normalized = self._normalize(f"{candidate.action_id} {candidate.label}")
        intent: str | None = None
        if self._contains(normalized, self.IRREVERSIBLE_TERMS):
            intent = "irreversible_data_change"
            risk_score = max(risk_score, 0.9)
            reversible = False
            blast_radius = "high"
            target_scope = "game"
            add("semantic_risk", "warning", 0.95, "action appears to modify or erase persistent game data")
            verification.extend(("confirm_irreversible_action", "independent_safety_evidence"))
        elif self._contains(normalized, self.SESSION_TERMS):
            intent = "session_control"
            risk_score = max(risk_score, 0.72)
            reversible = False if "quit" in normalized or "close" in normalized else reversible
            blast_radius = "high"
            target_scope = "session"
            add("semantic_risk", "warning", 0.9, "action appears to terminate or reset the current game session")
            verification.append("independent_safety_evidence")
        elif self._contains(normalized, self.TRANSACTION_TERMS):
            intent = "transaction"
            risk_score = max(risk_score, 0.7)
            blast_radius = "medium"
            target_scope = "game"
            add("semantic_risk", "warning", 0.85, "action appears to commit a purchase or resource-spending operation")
            verification.append("independent_safety_evidence")

        if candidate.kind == "key" and self._contains(normalized, self.SYSTEM_KEY_TERMS):
            intent = "system_control"
            risk_score = max(risk_score, 0.9)
            blast_radius = "critical"
            target_scope = "system"
            add("target_scope", "warning", 0.95, "key action may escape the game target or affect the operating system")
            verification.extend(("verify_target_scope", "independent_safety_evidence"))

        goal = self._normalize(" ".join(part for part in (context.current_goal, context.short_term_goal) if part))
        if intent is not None:
            if goal:
                goal_alignment = self._goal_supports(intent, goal)
                if not goal_alignment:
                    add("goal_alignment", "warning", 0.9, "high-risk action is not supported by the current goal")
                    verification.append("verify_goal_alignment")
                    risk_score = max(risk_score, 0.78)
            else:
                goal_alignment = None
                add("goal_alignment", "warning", 0.7, "high-risk action has no goal evidence")
                verification.append("verify_goal_alignment")

        expected_consistency = context.expected_effect_consistent
        if expected_consistency is False:
            add("expected_effect", "warning", 0.9, "candidate conflicts with the expected transition or effect")
            verification.append("verify_expected_effect")
            risk_score = max(risk_score, 0.75)

        if candidate.confidence < 0.5:
            add(
                "recognition_confidence",
                "info",
                1.0,
                "recognition confidence is low; this is tracked separately from safety",
            )
            if risk_score >= self.high_risk_threshold:
                add("high_risk_low_recognition", "warning", 0.9, "high-risk target also has weak recognition evidence")
                verification.append("reobserve_target")

        upstream_anomaly = bool(
            context.utility_score is not None
            and context.utility_score >= 0.75
            and risk_score >= self.high_risk_threshold
        )
        if upstream_anomaly:
            add("upstream_score_anomaly", "warning", context.utility_confidence or 0.7, "high utility conflicts with high semantic risk")
            verification.append("recheck_upstream_evaluation")

        blocked = any(entry.severity == "block" for entry in evidence)
        warned = any(entry.severity == "warning" for entry in evidence)
        if blocked:
            status = SafetyStatus.BLOCK
        elif warned or risk_score >= self.suspicious_threshold:
            status = SafetyStatus.SUSPICIOUS
        else:
            status = SafetyStatus.SAFE

        verification_requests = tuple(dict.fromkeys(verification))
        requires_verification = status == SafetyStatus.SUSPICIOUS and bool(verification_requests)
        warning_count = sum(entry.severity == "warning" for entry in evidence)
        safety_score = 0.0 if status == SafetyStatus.BLOCK else max(0.0, min(1.0, 1.0 - risk_score - 0.08 * warning_count))
        return ActionSafetyResult(
            uuid4().hex,
            candidate.action_id,
            status,
            candidate.confidence,
            round(safety_score, 6),
            round(risk_score, 6),
            self._risk_level(risk_score),
            reversible,
            blast_radius,
            target_scope,
            goal_alignment,
            expected_consistency,
            requires_verification,
            verification_requests,
            upstream_anomaly,
            tuple(evidence),
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.casefold().replace("_", " ").replace("-", " ").split())

    @staticmethod
    def _contains(text: str, terms: tuple[str, ...]) -> bool:
        return any(term in text for term in terms)

    @staticmethod
    def _risk_level(score: float) -> str:
        if score < 0.25:
            return "low"
        if score < 0.5:
            return "medium"
        if score < 0.8:
            return "high"
        return "critical"

    @staticmethod
    def _goal_supports(intent: str, goal: str) -> bool:
        terms = {
            "irreversible_data_change": ("delete save", "erase save", "reset save", "start over", "new game"),
            "session_control": ("quit game", "exit game", "close game", "restart game", "reset game", "stop playing"),
            "transaction": ("purchase", "buy", "spend", "shop"),
            "system_control": ("close game", "switch window", "operating system", "desktop"),
        }
        return any(term in goal for term in terms.get(intent, ()))


class ActionSafetyAuditLog:
    """Append-only audit events linking assessment, execution, and eventual outcome."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append_evaluation(self, result: ActionSafetyResult, *, snapshot_id: str = "", goal: str = "") -> None:
        self._append(
            {
                "event": "evaluation",
                "assessment_id": result.assessment_id,
                "action_id": result.action_id,
                "snapshot_id": snapshot_id,
                "goal": goal,
                "result": result.to_dict(),
            }
        )

    def append_execution(self, assessment_id: str, execution: Any) -> None:
        if not assessment_id.strip():
            raise ValueError("assessment_id must not be empty")
        self._append(
            {
                "event": "execution",
                "assessment_id": assessment_id,
                "action_id": str(getattr(execution, "action_id", "")),
                "executed": bool(getattr(execution, "executed", False)),
                "mode": str(getattr(execution, "mode", "")),
                "detail": str(getattr(execution, "detail", "")),
            }
        )

    def append_outcome(self, assessment_id: str, status: str, confidence: float, evidence: str = "") -> None:
        if not assessment_id.strip():
            raise ValueError("assessment_id must not be empty")
        if status not in {"success", "failure", "ongoing", "unknown"}:
            raise ValueError("invalid outcome status")
        if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("outcome confidence must be between 0 and 1")
        self._append(
            {
                "event": "actual_outcome",
                "assessment_id": assessment_id,
                "status": status,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    def entries(self) -> list[dict[str, Any]]:
        return self._read()

    def _append(self, event: dict[str, Any]) -> None:
        entries = self._read()
        entries.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("action safety audit log must contain an array")
        return [entry for entry in value if isinstance(entry, dict)]
