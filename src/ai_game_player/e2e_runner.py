import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation
from ai_game_player.pipeline import DecisionPipeline


@dataclass(frozen=True)
class E2EStepRecord:
    step: int
    observation_id: str
    frame_signature: str
    recognized_elements: int
    candidate_actions: tuple[str, ...]
    selected_action: str
    provider: str
    reason: str
    execution: dict[str, object]
    outcome: dict[str, object]
    latency_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "event": "step",
            "step": self.step,
            "observation_id": self.observation_id,
            "frame_signature": self.frame_signature,
            "recognized_elements": self.recognized_elements,
            "candidate_actions": list(self.candidate_actions),
            "selected_action": self.selected_action,
            "provider": self.provider,
            "reason": self.reason,
            "execution": self.execution,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class E2ERunReport:
    success: bool
    steps: int
    elapsed_seconds: float
    stop_reason: str
    live_input_verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "event": "summary",
            "success": self.success,
            "steps": self.steps,
            "elapsed_seconds": self.elapsed_seconds,
            "stop_reason": self.stop_reason,
            "live_input_verified": self.live_input_verified,
        }


class E2ETraceLog:
    """Append-only JSONL log for reproducible end-to-end runs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


class ContinuousE2ERunner:
    """Runs the real DecisionPipeline repeatedly and records why the run stopped."""

    def __init__(
        self,
        pipeline: DecisionPipeline,
        observation_probe: Callable[[], ScreenObservation],
        log_path: Path,
        *,
        milestone_probe: Callable[[], bool] | None = None,
        loop_guard: LoopGuard | None = None,
        max_steps: int = 1000,
        max_wall_seconds: float = 900.0,
        minimum_duration_seconds: float = 0.0,
        settle_seconds: float = 0.05,
        step_delay_seconds: float = 0.0,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")
        if minimum_duration_seconds < 0:
            raise ValueError("minimum_duration_seconds must not be negative")
        if settle_seconds < 0 or step_delay_seconds < 0:
            raise ValueError("delays must not be negative")
        self.pipeline = pipeline
        self.observation_probe = observation_probe
        self.log = E2ETraceLog(log_path)
        self.milestone_probe = milestone_probe
        self.loop_guard = loop_guard or LoopGuard(limit=4)
        self.max_steps = max_steps
        self.max_wall_seconds = max_wall_seconds
        self.minimum_duration_seconds = minimum_duration_seconds
        self.settle_seconds = settle_seconds
        self.step_delay_seconds = step_delay_seconds

    def run(self, purpose: str = "continue the sample game") -> E2ERunReport:
        started = time.monotonic()
        self.loop_guard.reset()
        live_verified = False
        completed_steps = 0

        for step_index in range(1, self.max_steps + 1):
            elapsed = time.monotonic() - started
            if elapsed >= self.max_wall_seconds:
                return self._finish(False, completed_steps, started, "wall_time_limit", live_verified)
            if self.minimum_duration_seconds and elapsed >= self.minimum_duration_seconds:
                return self._finish(True, completed_steps, started, "duration_reached", live_verified)
            if not self.pipeline.controller.is_running:
                return self._finish(False, completed_steps, started, "controller_stopped", live_verified)

            step_started = time.monotonic()
            try:
                result = self.pipeline.run_and_execute(purpose=purpose)
                live_verified = live_verified or bool(result.executed and result.mode == "live")
                if self.settle_seconds:
                    time.sleep(self.settle_seconds)
                after = self.observation_probe()
                trace_entries = self.pipeline.engine.trace.recent(1)
                if not trace_entries:
                    raise RuntimeError("decision trace missing after execution")
                trace = trace_entries[-1]
                context = trace.get("context", {})
                if not isinstance(context, dict):
                    raise RuntimeError("decision context missing from trace")
                decision = trace.get("decision", {})
                if not isinstance(decision, dict):
                    decision = {}
                state = context.get("state", {})
                if not isinstance(state, dict):
                    state = {}
                candidates = context.get("candidates", [])
                candidate_ids = tuple(
                    str(candidate.get("action_id", ""))
                    for candidate in candidates
                    if isinstance(candidate, dict) and candidate.get("action_id")
                ) if isinstance(candidates, list) else ()
                before_signature = str(state.get("signature", ""))
                after_signature = str(after.features.get("signature", ""))
                state_changed = bool(before_signature and after_signature and before_signature != after_signature)
                milestone = bool(self.milestone_probe and self.milestone_probe())
                outcome_status = "success" if milestone else "ongoing" if state_changed else "unknown"
                outcome_confidence = 1.0 if milestone else 0.9 if state_changed else 0.25
                self.pipeline.record_safety_outcome(
                    outcome_status,
                    outcome_confidence,
                    "milestone reached" if milestone else "screen signature changed" if state_changed else "no screen change detected",
                )
                record = E2EStepRecord(
                    step_index,
                    str(state.get("screen_id", "")),
                    before_signature,
                    int(state.get("detected_element_count", 0) or 0),
                    candidate_ids,
                    str(decision.get("action_id", result.action_id)),
                    str(decision.get("provider", "")),
                    str(decision.get("reason", "")),
                    {
                        "action_id": result.action_id,
                        "executed": result.executed,
                        "mode": result.mode,
                        "detail": result.detail,
                    },
                    {
                        "status": outcome_status,
                        "confidence": outcome_confidence,
                        "state_changed": state_changed,
                        "after_signature": after_signature,
                    },
                    round((time.monotonic() - step_started) * 1000.0, 3),
                )
                self.log.append(record.to_dict())
                completed_steps = step_index

                if milestone:
                    return self._finish(True, completed_steps, started, "milestone_reached", live_verified)
                if self.loop_guard.observe(after):
                    return self._finish(False, completed_steps, started, "repeated_observation", live_verified)
                if self.step_delay_seconds:
                    time.sleep(self.step_delay_seconds)
            except Exception as exc:
                self.log.append({"event": "error", "step": step_index, "error": str(exc), "category": self._classify_error(exc)})
                return self._finish(False, completed_steps, started, self._classify_error(exc), live_verified)

        return self._finish(False, completed_steps, started, "step_limit", live_verified)

    def _finish(self, success: bool, steps: int, started: float, stop_reason: str, live_verified: bool) -> E2ERunReport:
        report = E2ERunReport(success, steps, round(time.monotonic() - started, 3), stop_reason, live_verified)
        self.log.append(report.to_dict())
        return report

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        text = str(exc).casefold()
        if "safety" in text or "verification" in text or "blocked" in text:
            return "safety_block"
        if "candidate" in text or "候補" in text:
            return "recognition_or_candidate_failure"
        if "window" in text or "capture" in text or "getwindowrect" in text:
            return "target_or_capture_failure"
        if "停止" in text or "stopped" in text:
            return "controller_stopped"
        return "execution_failure"
