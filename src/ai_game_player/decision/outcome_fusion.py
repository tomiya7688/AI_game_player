from __future__ import annotations

from typing import Protocol

from ai_game_player.models import ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_detectors import (
    ScreenDiffDetector,
    StateDeltaDetector,
    TemporalChangeDetector,
    TerminalTextDetector,
)
from ai_game_player.outcome_models import OutcomeEvent, OutcomeEvidence


class SemanticOutcomeProvider(Protocol):
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None = None,
    ) -> OutcomeAssessment:
        ...


class DeterministicOutcomeFusion:
    """Fuses evidence while preserving uncertainty and detector disagreement."""

    def fuse(
        self,
        action_id: str,
        evidence: tuple[OutcomeEvidence, ...],
        *,
        semantic_fallback_used: bool = False,
    ) -> OutcomeEvent:
        terminal_success = self._support(evidence, "terminal", "success")
        terminal_failure = self._support(evidence, "terminal", "failure")
        semantic_success = self._support(evidence, "semantic", "success")
        semantic_failure = self._support(evidence, "semantic", "failure")

        if terminal_success > 0.0 and terminal_failure > 0.0:
            return self._event(
                action_id,
                "unknown",
                0.2,
                True,
                True,
                evidence,
                "conflicting terminal evidence",
                semantic_fallback_used,
            )
        if terminal_success > 0.0:
            return self._event(
                action_id,
                "success",
                self._bounded_support(terminal_success),
                False,
                False,
                evidence,
                "terminal success evidence",
                semantic_fallback_used,
            )
        if terminal_failure > 0.0:
            return self._event(
                action_id,
                "failure",
                self._bounded_support(terminal_failure),
                False,
                False,
                evidence,
                "terminal failure evidence",
                semantic_fallback_used,
            )

        if semantic_success > 0.0 and semantic_failure > 0.0:
            return self._event(
                action_id,
                "unknown",
                0.25,
                True,
                True,
                evidence,
                "conflicting semantic outcome evidence",
                semantic_fallback_used,
            )
        if semantic_success >= 0.45:
            return self._event(
                action_id,
                "success",
                self._bounded_support(semantic_success),
                False,
                False,
                evidence,
                "semantic fallback reported success",
                semantic_fallback_used,
            )
        if semantic_failure >= 0.45:
            return self._event(
                action_id,
                "failure",
                self._bounded_support(semantic_failure),
                False,
                False,
                evidence,
                "semantic fallback reported failure",
                semantic_fallback_used,
            )

        state_changed = self._support(evidence, "state_delta", "changed")
        state_stable = self._support(evidence, "state_delta", "stable")
        screen_changed = self._support(evidence, "screen_diff", "changed")
        screen_stable = self._support(evidence, "screen_diff", "stable")
        temporal_persistent = self._support(evidence, "temporal_change", "persistent")
        temporal_transient = self._support(evidence, "temporal_change", "transient")

        changed_support = state_changed + 0.5 * screen_changed + temporal_persistent
        stable_support = state_stable + 0.5 * screen_stable + temporal_transient
        conflict = (
            changed_support >= 0.45
            and stable_support >= 0.45
            and min(changed_support, stable_support) / max(changed_support, stable_support) >= 0.55
        )

        if state_changed >= 0.65:
            confidence = self._relative_confidence(changed_support, stable_support)
            return self._event(
                action_id,
                "changed",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "structured state changed",
                semantic_fallback_used,
            )
        if temporal_persistent >= 0.5 and screen_changed > 0.0:
            confidence = self._relative_confidence(changed_support, stable_support)
            return self._event(
                action_id,
                "changed",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "visual change persisted across follow-up observations",
                semantic_fallback_used,
            )
        if state_stable >= 0.6 and (screen_stable > 0.0 or temporal_transient >= 0.5):
            confidence = self._relative_confidence(stable_support, changed_support)
            return self._event(
                action_id,
                "unchanged",
                confidence,
                conflict,
                conflict and confidence < 0.6,
                evidence,
                "structured state remained stable",
                semantic_fallback_used,
            )
        if changed_support >= 0.65 and changed_support - stable_support >= 0.25:
            return self._event(
                action_id,
                "changed",
                self._relative_confidence(changed_support, stable_support),
                conflict,
                conflict,
                evidence,
                "multiple change detectors agree",
                semantic_fallback_used,
            )
        if stable_support >= 0.75 and stable_support - changed_support >= 0.25:
            return self._event(
                action_id,
                "unchanged",
                self._relative_confidence(stable_support, changed_support),
                conflict,
                conflict,
                evidence,
                "multiple detectors report stable state",
                semantic_fallback_used,
            )
        return self._event(
            action_id,
            "unknown",
            min(0.49, abs(changed_support - stable_support)),
            conflict,
            True,
            evidence,
            "insufficient or conflicting outcome evidence",
            semantic_fallback_used,
        )

    @staticmethod
    def _support(evidence: tuple[OutcomeEvidence, ...], signal: str, value: str) -> float:
        return sum(item.weight for item in evidence if item.signal == signal and item.value == value)

    @staticmethod
    def _bounded_support(value: float) -> float:
        return round(max(0.0, min(1.0, value)), 6)

    @staticmethod
    def _relative_confidence(primary: float, opposing: float) -> float:
        total = primary + opposing
        if total <= 0.0:
            return 0.0
        return round(max(0.0, min(1.0, primary / total)), 6)

    @staticmethod
    def _event(
        action_id: str,
        status: str,
        confidence: float,
        conflict: bool,
        abstained: bool,
        evidence: tuple[OutcomeEvidence, ...],
        reason: str,
        semantic_fallback_used: bool,
    ) -> OutcomeEvent:
        return OutcomeEvent(
            action_id,
            status,
            round(max(0.0, min(1.0, confidence)), 6),
            conflict,
            abstained,
            evidence,
            reason,
            semantic_fallback_used,
        )


class OutcomeDetector:
    """Lightweight Before + Action + After outcome pipeline with optional semantic fallback."""

    def __init__(
        self,
        semantic_provider: SemanticOutcomeProvider | None = None,
        semantic_threshold: float = 0.55,
        fusion: DeterministicOutcomeFusion | None = None,
    ) -> None:
        if not 0.0 <= semantic_threshold <= 1.0:
            raise ValueError("semantic threshold must be between 0 and 1")
        self.semantic_provider = semantic_provider
        self.semantic_threshold = semantic_threshold
        self.terminal = TerminalTextDetector()
        self.state_delta = StateDeltaDetector()
        self.screen_diff = ScreenDiffDetector()
        self.temporal = TemporalChangeDetector()
        self.fusion = fusion or DeterministicOutcomeFusion()

    def detect(
        self,
        before: ScreenObservation,
        action_id: str,
        after: ScreenObservation,
        temporal_observations: tuple[ScreenObservation, ...] = (),
    ) -> OutcomeEvent:
        evidence = list(self.terminal.detect(after))
        evidence.append(self.state_delta.detect(before, after))
        evidence.append(self.screen_diff.detect(before, after))
        evidence.append(self.temporal.detect(before, after, temporal_observations))
        event = self.fusion.fuse(action_id, tuple(evidence))
        if self.semantic_provider is None:
            return event
        if not event.abstained and event.confidence >= self.semantic_threshold:
            return event

        assessment = self.semantic_provider.assess_outcome(after, before)
        evidence.append(
            OutcomeEvidence(
                "semantic_outcome",
                "semantic",
                assessment.status,
                assessment.confidence,
                0.7,
                f"{type(self.semantic_provider).__name__}/semantic_outcome",
                {"reason": assessment.reason},
            )
        )
        return self.fusion.fuse(action_id, tuple(evidence), semantic_fallback_used=True)
