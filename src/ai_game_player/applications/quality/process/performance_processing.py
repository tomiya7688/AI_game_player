from __future__ import annotations

import statistics
import time
from collections.abc import Callable

from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.safety_guard import EmergencyStop, SafetyGuard, SafetyGuardConfig
from ai_game_player.screen_capture import ScreenFrame


def run_performance_checks(budgets: dict[str, float]) -> dict[str, object]:
    cases = (
        _case_frame_analyzer(budgets["frame_analyzer_ms"]),
        _case_candidate_merger(budgets["candidate_merger_ms"]),
        _case_decision_context(budgets["decision_context_ms"]),
        _case_safety_guard(budgets["safety_guard_ms"]),
    )
    failed = [case["name"] for case in cases if not case["passed"]]
    return {
        "schema": "ai-game-player/performance-check/v1",
        "passed": not failed,
        "failed_cases": failed,
        "cases": list(cases),
    }


def _case_frame_analyzer(budget_ms: float) -> dict[str, object]:
    width, height = 160, 90
    row = bytes((24, 24, 24, 255)) * width
    data = bytearray(row * height)
    for y in range(25, 65):
        for x in range(45, 115):
            offset = (y * width + x) * 4
            data[offset : offset + 4] = bytes((245, 245, 245, 255))
    frame = ScreenFrame(width, height, bytes(data))
    analyzer = FrameAnalyzer()
    return _measure("frame_analyzer", 15, lambda: analyzer.analyze(frame, "perf"), budget_ms)


def _case_candidate_merger(budget_ms: float) -> dict[str, object]:
    merger = CandidateMerger()
    configured = [
        ActionCandidate("configured-start", "click", "START", 80, 45, 0.95, bbox=(50, 30, 60, 30))
    ]
    detected = [
        ActionCandidate(
            f"ocr-{index}",
            "click",
            f"item-{index}",
            10 + index,
            20 + index,
            0.7,
            bbox=(index, index, 20, 10),
        )
        for index in range(16)
    ]
    image = [
        ActionCandidate(
            f"image-{index}",
            "click",
            "region",
            20 + index,
            30 + index,
            0.6,
            bbox=(index + 2, index + 2, 24, 12),
        )
        for index in range(16)
    ]
    return _measure(
        "candidate_merger",
        500,
        lambda: merger.merge(configured, detected, image),
        budget_ms,
    )


def _case_decision_context(budget_ms: float) -> dict[str, object]:
    observation = ScreenObservation(
        "perf",
        320,
        180,
        ["START"],
        {"signature": "sig", "perceptual_hash": "hash", "mean_brightness": 32},
    )
    candidates = [
        ActionCandidate("start", "click", "START", 100, 80, 0.95, bbox=(80, 60, 40, 30)),
        ActionCandidate("wait", "wait", "Wait", confidence=0.8),
    ]
    builder = DecisionContextBuilder(knowledge_limit=0)
    return _measure(
        "decision_context",
        400,
        lambda: builder.build(observation, candidates, candidates, current_goal="continue"),
        budget_ms,
    )


def _case_safety_guard(budget_ms: float) -> dict[str, object]:
    config = SafetyGuardConfig(
        max_actions_per_second=1_000_000,
        max_burst_actions=1_000_000,
        max_same_action_repeats=1_000_000,
        max_cycle_repeats=1_000_000,
        max_session_actions=10_000_000,
        no_progress_repeat_limit=1_000_000,
        require_target=False,
        require_foreground=False,
    )
    guard = SafetyGuard(config, emergency_stop=EmergencyStop(), native_validator=None)
    candidates = (
        ActionCandidate("a", "wait", "0"),
        ActionCandidate("b", "wait", "0"),
    )
    counter = 0

    def operation() -> object:
        nonlocal counter
        candidate = candidates[counter & 1]
        counter += 1
        return guard.check(candidate)

    return _measure("safety_guard", 2000, operation, budget_ms)


def _measure(
    name: str,
    iterations: int,
    operation: Callable[[], object],
    budget_ms: float,
) -> dict[str, object]:
    if iterations < 1 or budget_ms <= 0:
        raise ValueError("performance check requires positive iterations and budget")
    for _ in range(min(5, iterations)):
        operation()
    samples: list[float] = []
    batch_count = 5
    batch_size = max(1, iterations // batch_count)
    for _ in range(batch_count):
        started = time.perf_counter_ns()
        for _ in range(batch_size):
            operation()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples.append(elapsed_ms / batch_size)
    median_ms = statistics.median(samples)
    p95_ms = max(samples)
    return {
        "name": name,
        "iterations": batch_count * batch_size,
        "median_ms": round(median_ms, 6),
        "p95_batch_ms_per_op": round(p95_ms, 6),
        "budget_ms": float(budget_ms),
        "passed": p95_ms <= budget_ms,
    }
