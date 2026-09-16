from __future__ import annotations

from math import ceil
from typing import Any, Iterable

from ai_game_player.models import ScreenObservation
from ai_game_player.outcome_models import OutcomeEvidence


_SUCCESS_TERMS = ("SUCCESS", "CLEAR", "VICTORY", "COMPLETE", "成功", "クリア", "勝利")
_FAILURE_TERMS = ("GAME OVER", "FAILED", "DEFEAT", "失敗", "敗北", "ゲームオーバー")
_NUMERIC_STATE_KEYS = (
    "score",
    "progress",
    "level",
    "hp",
    "health",
    "lives",
    "resource",
    "resources",
    "currency",
)


class TerminalTextDetector:
    """Produces terminal evidence without deciding the final outcome."""

    def detect(self, observation: ScreenObservation) -> tuple[OutcomeEvidence, ...]:
        text = " ".join(observation.ocr_text).upper()
        evidence: list[OutcomeEvidence] = []
        success = [term for term in _SUCCESS_TERMS if term.upper() in text]
        failure = [term for term in _FAILURE_TERMS if term.upper() in text]
        if success:
            evidence.append(
                OutcomeEvidence(
                    "terminal_text",
                    "terminal",
                    "success",
                    0.9,
                    0.95,
                    "terminal_text/v1",
                    {"matched_terms": success},
                )
            )
        if failure:
            evidence.append(
                OutcomeEvidence(
                    "terminal_text",
                    "terminal",
                    "failure",
                    0.9,
                    0.95,
                    "terminal_text/v1",
                    {"matched_terms": failure},
                )
            )
        return tuple(evidence)


class StateDeltaDetector:
    """Compares structured state separately from visual change."""

    def detect(self, before: ScreenObservation, after: ScreenObservation) -> OutcomeEvidence:
        before_state = _structured_state(before)
        after_state = _structured_state(after)
        changed_fields: dict[str, dict[str, Any]] = {}
        for key in sorted(set(before_state) | set(after_state)):
            left = before_state.get(key)
            right = after_state.get(key)
            if left != right:
                changed_fields[key] = {"before": left, "after": right}

        before_text = _normalized_text(before.ocr_text)
        after_text = _normalized_text(after.ocr_text)
        text_changed = before_text != after_text
        before_elements = _element_signature(before)
        after_elements = _element_signature(after)
        elements_changed = before_elements != after_elements
        has_state_evidence = bool(
            before_state
            or after_state
            or before_text
            or after_text
            or before_elements
            or after_elements
        )

        details: dict[str, Any] = {
            "changed_fields": changed_fields,
            "text_changed": text_changed,
            "elements_changed": elements_changed,
            "state_evidence_available": has_state_evidence,
        }
        _add_generic_deltas(details, before_state, after_state)

        if not has_state_evidence:
            return OutcomeEvidence(
                "state_delta",
                "state_delta",
                "unknown",
                0.0,
                0.9,
                "state_delta/v1",
                details,
            )
        if changed_fields:
            return OutcomeEvidence(
                "state_delta",
                "state_delta",
                "changed",
                0.9,
                0.95,
                "state_delta/v1",
                details,
            )
        if text_changed or elements_changed:
            return OutcomeEvidence(
                "state_delta",
                "state_delta",
                "changed",
                0.55,
                0.7,
                "state_delta/v1",
                details,
            )
        return OutcomeEvidence(
            "state_delta",
            "state_delta",
            "stable",
            0.85,
            0.9,
            "state_delta/v1",
            details,
        )


class ScreenDiffDetector:
    """Produces screen-change evidence without treating change as progress."""

    def __init__(self, change_threshold: float = 0.08) -> None:
        if not 0.0 < change_threshold <= 1.0:
            raise ValueError("screen change threshold must be between 0 and 1")
        self.change_threshold = change_threshold

    def detect(self, before: ScreenObservation, after: ScreenObservation) -> OutcomeEvidence:
        distance, source = visual_distance(before, after)
        if distance is None:
            return OutcomeEvidence(
                "screen_diff",
                "screen_diff",
                "unknown",
                0.0,
                0.5,
                "screen_diff/v1",
                {"distance": None, "source": source},
            )
        if distance >= self.change_threshold:
            confidence = min(1.0, 0.5 + distance)
            reliability = 0.7 if source == "perceptual_hash" else 0.3
            value = "changed"
        else:
            confidence = min(1.0, 0.7 + (self.change_threshold - distance))
            reliability = 0.8 if source == "perceptual_hash" else 0.65
            value = "stable"
        return OutcomeEvidence(
            "screen_diff",
            "screen_diff",
            value,
            confidence,
            reliability,
            "screen_diff/v1",
            {"distance": round(distance, 6), "source": source, "threshold": self.change_threshold},
        )


class TemporalChangeDetector:
    """Uses optional follow-up frames to distinguish persistent change from one-frame noise."""

    def __init__(self, similarity_threshold: float = 0.04) -> None:
        if not 0.0 < similarity_threshold <= 1.0:
            raise ValueError("temporal similarity threshold must be between 0 and 1")
        self.similarity_threshold = similarity_threshold

    def detect(
        self,
        before: ScreenObservation,
        after: ScreenObservation,
        followups: Iterable[ScreenObservation] = (),
    ) -> OutcomeEvidence:
        samples = tuple(followups)
        if not samples:
            return OutcomeEvidence(
                "temporal",
                "temporal_change",
                "unverified",
                0.0,
                0.7,
                "temporal_change/v1",
                {"samples": 0},
            )

        after_matches = 0
        before_matches = 0
        usable = 0
        for sample in samples:
            after_distance, _ = visual_distance(after, sample)
            before_distance, _ = visual_distance(before, sample)
            if after_distance is None or before_distance is None:
                continue
            usable += 1
            after_matches += after_distance <= self.similarity_threshold
            before_matches += before_distance <= self.similarity_threshold
        if usable == 0:
            return OutcomeEvidence(
                "temporal",
                "temporal_change",
                "unverified",
                0.0,
                0.7,
                "temporal_change/v1",
                {"samples": len(samples), "usable_samples": 0},
            )

        required = ceil(usable / 2)
        details = {
            "samples": len(samples),
            "usable_samples": usable,
            "after_matches": after_matches,
            "before_matches": before_matches,
            "similarity_threshold": self.similarity_threshold,
        }
        if after_matches >= required and after_matches > before_matches:
            return OutcomeEvidence(
                "temporal",
                "temporal_change",
                "persistent",
                after_matches / usable,
                0.9,
                "temporal_change/v1",
                details,
            )
        if before_matches >= required and before_matches >= after_matches:
            return OutcomeEvidence(
                "temporal",
                "temporal_change",
                "transient",
                before_matches / usable,
                0.9,
                "temporal_change/v1",
                details,
            )
        return OutcomeEvidence(
            "temporal",
            "temporal_change",
            "ambiguous",
            0.35,
            0.7,
            "temporal_change/v1",
            details,
        )


def visual_distance(before: ScreenObservation, after: ScreenObservation) -> tuple[float | None, str]:
    left_hash = before.features.get("perceptual_hash")
    right_hash = after.features.get("perceptual_hash")
    if isinstance(left_hash, str) and isinstance(right_hash, str) and left_hash and len(left_hash) == len(right_hash):
        try:
            left = int(left_hash, 16)
            right = int(right_hash, 16)
        except ValueError:
            pass
        else:
            bits = max(1, len(left_hash) * 4)
            return (left ^ right).bit_count() / bits, "perceptual_hash"

    left_signature = before.features.get("signature")
    right_signature = after.features.get("signature")
    if isinstance(left_signature, str) and isinstance(right_signature, str) and left_signature and right_signature:
        return (0.0 if left_signature == right_signature else 1.0), "signature"
    return None, "unavailable"


def _structured_state(observation: ScreenObservation) -> dict[str, Any]:
    result: dict[str, Any] = {}
    raw_state = observation.features.get("state")
    if isinstance(raw_state, dict):
        for key, value in raw_state.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[str(key)] = value
    for key in _NUMERIC_STATE_KEYS:
        value = observation.features.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = float(value)
    return result


def _normalized_text(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted(" ".join(str(value).casefold().split()) for value in values if str(value).strip()))


def _element_signature(observation: ScreenObservation) -> tuple[tuple[str, str, str], ...]:
    raw = observation.features.get("detected_elements", [])
    if not isinstance(raw, list):
        return ()
    result: list[tuple[str, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        result.append(
            (
                str(item.get("element_id", item.get("action_id", ""))),
                str(item.get("element_type", item.get("kind", ""))),
                " ".join(str(item.get("text", item.get("label", ""))).casefold().split()),
            )
        )
    return tuple(sorted(result))


def _add_generic_deltas(details: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in ("progress", "score", "level"):
        left = before.get(key)
        right = after.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and right != left:
            details["progress_delta"] = float(right) - float(left)
            break
    for key in ("resource", "resources", "currency"):
        left = before.get(key)
        right = after.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and right != left:
            details["resource_delta"] = float(right) - float(left)
            break
