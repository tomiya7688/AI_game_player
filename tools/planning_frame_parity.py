"""Same-pixel, no-input validation of the explicit native comparison profile."""

from dataclasses import asdict
from hashlib import sha256
import time

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.planner_2048_observation import Reference2048PlanningObservation


def validate_native_preprocessing(frame, runtime):
    if frame is None or len(frame.bgra) > 16 * 1024 * 1024:
        raise ValueError("native comparison parity requires one bounded accepted frame")
    path = runtime.library_path
    if (
        path is None
        or not path.is_file()
        or not 0 < path.stat().st_size <= 64 * 1024 * 1024
    ):
        raise ValueError("native comparison binary hash is unavailable")
    digest = sha256()
    with path.open("rb") as stream:
        remaining = 64 * 1024 * 1024
        while chunk := stream.read(min(1024 * 1024, remaining + 1)):
            if len(chunk) > remaining:
                raise ValueError("native comparison binary exceeded its byte bound")
            remaining -= len(chunk)
            digest.update(chunk)
    analyses = []
    timings = []
    for preprocessor in (None, runtime):
        started = time.monotonic_ns()
        observation = FrameAnalyzer(frame_preprocessor=preprocessor).analyze(
            frame, "same_pixel_parity"
        )
        timings.append(time.monotonic_ns() - started)
        analyses.append(observation)
    primitive_fields = (
        "mean_rgb",
        "mean_brightness",
        "perceptual_hash",
        "signature",
        "image_candidates",
        "detected_elements",
    )
    fields = {
        name: analyses[0].features.get(name) == analyses[1].features.get(name)
        for name in primitive_fields
    }
    adapted = [
        ReferenceGame2048Adapter().adapt(frame, observation) for observation in analyses
    ]
    candidate_equal = [asdict(candidate) for candidate in adapted[0][1]] == [
        asdict(candidate) for candidate in adapted[1][1]
    ]
    sensor = Reference2048PlanningObservation(clock_id="same_pixel_parity")
    states = [sensor.semantic_state(observation, 0) for observation, _ in adapted]
    semantic_equal = states[0] is not None and states[0] == states[1]
    decisions = []
    for observation, candidates in adapted:
        if not candidates:
            decisions.append(None)
        else:
            context = DecisionContextBuilder().build(
                observation, candidates, candidates
            )
            decisions.append(
                BaselineDecisionProvider(min_margin=0, min_recognition=0.65)
                .decide(context)
                .to_dict()
            )
    decision_equal = decisions[0] is not None and decisions[0] == decisions[1]
    return {
        "same_pixel_sha256": sha256(frame.bgra).hexdigest(),
        "primitive_fields_equal": fields,
        "candidate_equal": candidate_equal,
        "semantic_equal": semantic_equal,
        "baseline_decision_equal": decision_equal,
        "planner_input_equal": candidate_equal and semantic_equal,
        "scope": "same frame statistics/components, reference candidates/state and Baseline decision; Planner inputs equal, deadline-dependent search depth is not an identical-choice guarantee",
        "analysis_ns": {"python": timings[0], "native": timings[1]},
        "binary": {
            "sha256": digest.hexdigest(),
            "abi_version": runtime.info.abi_version,
            "capabilities": sorted(
                capability.value for capability in runtime.descriptor.capabilities
            ),
        },
        "match": all(fields.values())
        and candidate_equal
        and semantic_equal
        and decision_equal,
    }
