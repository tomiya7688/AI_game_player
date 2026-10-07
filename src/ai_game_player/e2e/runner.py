import json
import time
from pathlib import Path
from typing import Callable

from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation
from ai_game_player.pipeline import DecisionPipeline


def append_e2e_event(path: Path, event: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def run_continuous_e2e(
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
    purpose: str = "continue the sample game",
) -> dict[str, object]:
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    if max_wall_seconds <= 0:
        raise ValueError("max_wall_seconds must be positive")
    if minimum_duration_seconds < 0:
        raise ValueError("minimum_duration_seconds must not be negative")
    if settle_seconds < 0 or step_delay_seconds < 0:
        raise ValueError("delays must not be negative")

    active_guard = loop_guard or LoopGuard(limit=4)
    active_guard.reset()
    started = time.monotonic()
    live_verified = False
    completed_steps = 0

    for step_index in range(1, max_steps + 1):
        elapsed = time.monotonic() - started
        if elapsed >= max_wall_seconds:
            return _finish(log_path, False, completed_steps, started, "wall_time_limit", live_verified)
        if minimum_duration_seconds and elapsed >= minimum_duration_seconds:
            return _finish(log_path, True, completed_steps, started, "duration_reached", live_verified)
        if not pipeline.controller.is_running:
            return _finish(log_path, False, completed_steps, started, "controller_stopped", live_verified)

        step_started = time.monotonic()
        try:
            result = pipeline.run_and_execute(purpose=purpose)
            live_verified = live_verified or bool(result.executed and result.mode == "live")
            if settle_seconds:
                time.sleep(settle_seconds)
            after = observation_probe()
            trace_entries = pipeline.engine.trace.recent(1)
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
            candidate_ids = [
                str(candidate.get("action_id", ""))
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("action_id")
            ] if isinstance(candidates, list) else []
            before_signature = str(state.get("signature", ""))
            after_signature = str(after.features.get("signature", ""))
            state_changed = bool(before_signature and after_signature and before_signature != after_signature)
            milestone = bool(milestone_probe and milestone_probe())
            outcome_status = "success" if milestone else "ongoing" if state_changed else "unknown"
            outcome_confidence = 1.0 if milestone else 0.9 if state_changed else 0.25
            pipeline.record_safety_outcome(
                outcome_status,
                outcome_confidence,
                "milestone reached" if milestone else "screen signature changed" if state_changed else "no screen change detected",
            )
            append_e2e_event(
                log_path,
                {
                    "event": "step",
                    "step": step_index,
                    "observation_id": str(state.get("screen_id", "")),
                    "frame_signature": before_signature,
                    "recognized_elements": int(state.get("detected_element_count", 0) or 0),
                    "candidate_actions": candidate_ids,
                    "selected_action": str(decision.get("action_id", result.action_id)),
                    "provider": str(decision.get("provider", "")),
                    "reason": str(decision.get("reason", "")),
                    "execution": {
                        "action_id": result.action_id,
                        "executed": result.executed,
                        "mode": result.mode,
                        "detail": result.detail,
                    },
                    "outcome": {
                        "status": outcome_status,
                        "confidence": outcome_confidence,
                        "state_changed": state_changed,
                        "after_signature": after_signature,
                    },
                    "latency_ms": round((time.monotonic() - step_started) * 1000.0, 3),
                },
            )
            completed_steps = step_index

            if milestone:
                return _finish(log_path, True, completed_steps, started, "milestone_reached", live_verified)
            if active_guard.observe(after):
                return _finish(log_path, False, completed_steps, started, "repeated_observation", live_verified)
            if step_delay_seconds:
                time.sleep(step_delay_seconds)
        except Exception as exc:
            category = _classify_error(exc)
            append_e2e_event(log_path, {"event": "error", "step": step_index, "error": str(exc), "category": category})
            return _finish(log_path, False, completed_steps, started, category, live_verified)

    return _finish(log_path, False, completed_steps, started, "step_limit", live_verified)


def _finish(path: Path, success: bool, steps: int, started: float, stop_reason: str, live_verified: bool) -> dict[str, object]:
    report = {
        "event": "summary",
        "success": success,
        "steps": steps,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stop_reason": stop_reason,
        "live_input_verified": live_verified,
    }
    append_e2e_event(path, report)
    return report


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
