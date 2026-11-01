"""Explicit short Windows comparison, independent of the required 600s campaign."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.run_control import ExecutionCancelled
from tools.browser_probe_support import new_probe_directory, publish_validation_report
from tools.planning_resource_monitor import PlanningResourceMonitor
from tools.reference_game_2048_source import SOURCE_LOCK_PATH
from tools.run_2048_campaign import ReferenceGame2048Episode, CAMPAIGN_PURPOSE


def evaluate_visible_score(episode, ocr):
    """Post-decision evaluation only. Never write this OCR frame back to Source/Planner."""
    frame = episode.source.capture.capture(episode.source.window_handle)
    observation = FrameAnalyzer(ocr).analyze(frame, "comparison_score_evaluation")
    adapted, _ = ReferenceGame2048Adapter().adapt(frame, observation)
    game = adapted.features["game_2048"]
    return {
        "value": game.get("score"),
        "confidence": game.get("score_confidence"),
        "frame_sha256": sha256(frame.bgra).hexdigest(),
        "reference": "post-decision screenshot OCR only",
    }


def run_episode(provider, seed, max_steps, cache, output):
    monitor = None
    episode = None
    result = {
        "provider": provider,
        "seed": seed,
        "max_steps": max_steps,
        "actions": 0,
        "invalid_actions": 0,
        "policy_reobservations": 0,
        "fresh_input_captures": 0,
        "plans": [],
        "failure": None,
        "stopped_input_rejected": False,
        "stale_generation_rejected": False,
    }
    latencies = []
    started = time.monotonic()
    try:
        monitor = PlanningResourceMonitor().start()
        episode = ReferenceGame2048Episode(
            1,
            cache,
            output,
            None,
            None,
            True,
            decision_provider=provider,
            planner_conditional=provider == "planner",
            planning_settle_ms=1000,
            test_seed=seed,
        )
        result["seed_fixture"] = episode.launcher.server.test_seed_metadata
        state = episode.observe()
        result["initial_max_tile"] = state.max_tile
        initial_token = episode.pipeline.controller.rearm_token
        for _ in range(max_steps):
            if state.terminal:
                break
            before = state
            began = time.monotonic_ns()
            outcome = DecisionOrchestrator(episode.pipeline).run(
                execute=True, purpose=CAMPAIGN_PURPOSE
            )
            latencies.append((time.monotonic_ns() - began) / 1e6)
            if isinstance(outcome, DecisionDisposition):
                result["withheld"] = outcome.to_dict()
                break
            if not outcome.executed:
                raise RuntimeError("comparison did not execute actual input")
            episode.source.not_before = time.monotonic() + 1
            result["actions"] += 1
            state = (
                episode.observe()
            )  # explicit receipt completion, including final/terminal action
            result["invalid_actions"] += int(state.signature == before.signature)
            planning = episode.pipeline.engine.provider
            plan = getattr(planning, "last_planning_result", None)
            policy = getattr(planning, "last_policy_decision", None)
            if plan is not None:
                result["fresh_input_captures"] += 1
                result["policy_reobservations"] += (
                    policy.observation_requests if policy is not None else 0
                )
                result["plans"].append(
                    {
                        "completed_depth": plan.completed_depth,
                        "continuation_semantics": getattr(
                            plan, "continuation_semantics", "one_step"
                        ),
                        "probability_semantics": plan.probability_semantics,
                        "global_risk_known": policy.global_risk_known,
                        "counters": asdict(plan.counters),
                    }
                )
        result.update(
            {
                "max_tile": state.max_tile,
                "terminal": state.terminal,
                "survival_steps": result["actions"],
            }
        )
        ocr = TesseractOcrRecognizer.optional()
        if ocr is None:
            raise RuntimeError(
                "score evaluation requires the explicit comparison OCR dependency"
            )
        result["score_evaluation"] = evaluate_visible_score(episode, ocr)
        if type(result["score_evaluation"]["value"]) is not int:
            raise RuntimeError(
                "post-decision visible score OCR is unavailable; do not claim score comparison"
            )
        records = episode.pipeline.load_execution_history()
        result["execution_history_count"] = len(records)
        token = episode.pipeline.controller.rearm_token
        episode.pipeline.controller.stop("comparison Stop probe")
        try:
            episode.pipeline.run_and_execute(expected_rearm_token=token)
        except ExecutionCancelled:
            result["stopped_input_rejected"] = True
        episode.pipeline.controller.start()
        try:
            episode.pipeline.run_and_execute(expected_rearm_token=initial_token)
        except ExecutionCancelled:
            result["stale_generation_rejected"] = True
        if len(episode.pipeline.load_execution_history()) != len(records):
            raise AssertionError("Stop/stale probe executed input")
        result["minimum_actual_input_met"] = result["actions"] >= min(3, max_steps)
        if (
            not result["minimum_actual_input_met"]
            or not result["stopped_input_rejected"]
            or not result["stale_generation_rejected"]
        ):
            raise AssertionError(
                "comparison minimum actual input or Stop/stale guard failed"
            )
    except Exception as error:
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    finally:
        if episode is not None:
            try:
                episode.close()
            except Exception as error:
                result["cleanup_failure"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:1000],
                }
        try:
            result["resources"] = monitor.close() if monitor is not None else None
        except Exception as error:
            result["resources"] = None
            result["resource_failure"] = {
                "type": type(error).__name__,
                "message": str(error)[:1000],
            }
    ordered = sorted(latencies)
    result["latency_ms"] = {
        "p50": statistics.median(ordered) if ordered else None,
        "p95": ordered[int((len(ordered) - 1) * 0.95)] if ordered else None,
        "samples": latencies,
    }
    result["wall_seconds"] = time.monotonic() - started
    result["invalid_action_rate"] = result["invalid_actions"] / max(
        1, result["actions"]
    )
    result["policy_reobserve_per_action"] = result["policy_reobservations"] / max(
        1, result["actions"]
    )
    result["nodes"] = sum(plan["counters"]["nodes_visited"] for plan in result["plans"])
    result["success"] = (
        result["failure"] is None
        and not result.get("cleanup_failure")
        and not result.get("resource_failure")
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=479)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--allow-live-input", action="store_true")
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("build/reference_games/2048")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("build/planning-comparison/windows")
    )
    args = parser.parse_args(argv)
    if not args.allow_live_input:
        parser.error(
            "owned Windows input comparison requires explicit --allow-live-input"
        )
    if not 0 <= args.seed <= 0xFFFFFFFF or not 3 <= args.max_steps <= 60:
        parser.error("seed uint32 and steps3..60 required")
    if os.name != "nt":
        parser.error("this comparison requires actual Windows WGC/input")
    output = new_probe_directory(args.output)
    report = {
        "schema_version": 1,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "game_source_lock": json.loads(SOURCE_LOCK_PATH.read_text()),
        "seed": args.seed,
        "max_steps": args.max_steps,
        "profile": "short Windows comparison; independent of required 600s gate",
        "decision_state": "existing screenshot board recognizer only; post-decision score OCR never enters Source/Planner",
        "settle_ms": 1000,
        "baseline_profile": {"min_margin": 0, "min_recognition": 0.65},
        "planning_profile": {
            "conditional_opt_in": True,
            "global_world_risk": "unknown",
            "depth": 2,
            "nodes": 10000,
            "wall_time_ms": 400,
        },
        "episodes": [],
    }
    for provider in ("cycling", "baseline", "planner"):
        result = run_episode(
            provider, args.seed, args.max_steps, args.cache_dir, output / provider
        )
        report["episodes"].append(result)
        report["success"] = len(report["episodes"]) == 3 and all(
            e["success"] for e in report["episodes"]
        )
        publish_validation_report(output / "report.json", report)
    print(output / "report.json")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
