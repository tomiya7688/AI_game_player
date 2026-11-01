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

from ai_game_player.event_journal import EventJournal
from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.run_control import ExecutionCancelled
from tools.browser_probe_support import new_probe_directory, publish_validation_report
from tools.planning_comparison_source import source_fingerprint
from tools.planning_resource_monitor import PlanningResourceMonitor
from tools.planning_failure_header import OwnedGameFailureHeader
from tools.planning_score_evaluation import reread_score_header_label, reread_score_line
from tools.reference_game_2048_source import SOURCE_LOCK_PATH
from tools.run_2048_campaign import (
    ReferenceGame2048Episode,
    CAMPAIGN_PURPOSE,
    CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS,
    SCREEN_READY_TIMEOUT_SECONDS,
)


def failure_header_bbox(frame, label_bbox=None):
    width = min(640, frame.width)
    center = frame.width / 2
    if isinstance(label_bbox, (list, tuple)) and len(label_bbox) == 4:
        center = label_bbox[0] + label_bbox[2] / 2
    left = max(0, min(frame.width - width, int(center - width / 2)))
    return [left, 0, width, min(240, frame.height)]


def evaluate_visible_score(episode, ocr, *, failure_header=None):
    """Post-decision evaluation only. Never write this OCR frame back to Source/Planner."""
    frame, capture_diagnostic = episode.source.capture_evaluation_frame()
    if frame is None:
        return {
            "value": None,
            "confidence": 0.0,
            "capture": capture_diagnostic,
            "attempts": [],
        }
    observation = FrameAnalyzer(ocr).analyze(frame, "comparison_score_evaluation")
    adapted, _ = ReferenceGame2048Adapter().adapt(frame, observation)
    game = adapted.features["game_2048"]
    attempts = [
        {
            "profile": "document",
            "value": game.get("score"),
            "confidence": game.get("score_confidence"),
        }
    ]
    # Require the labelled numeric line even when a legacy whole-page parser guessed a number.
    reread, diagnostic = reread_score_line(frame, observation, ocr)
    if (
        reread is None
        and diagnostic["status"] == "score_label_unavailable_or_ambiguous"
        and game.get("confidence", 0) >= 0.65
    ):
        label_observation, label_diagnostic = reread_score_header_label(
            frame, observation, game.get("board_bbox"), ocr
        )
        if label_observation is not None:
            reread, diagnostic = reread_score_line(frame, label_observation, ocr)
        diagnostic["label_search"] = label_diagnostic
    game = {"score": None, "score_confidence": 0.0}
    if reread is not None:
        adapted, _ = ReferenceGame2048Adapter().adapt(frame, reread)
        game = adapted.features["game_2048"]
    attempts.append(
        {
            "profile": "single_numeric_line",
            **diagnostic,
            "value": game.get("score"),
            "confidence": game.get("score_confidence"),
        }
    )
    result = {
        "value": game.get("score"),
        "confidence": game.get("score_confidence"),
        "frame_sha256": sha256(frame.bgra).hexdigest(),
        "reference": "post-decision screenshot OCR only",
        "attempts": attempts,
        "capture": capture_diagnostic,
    }
    if failure_header is not None and (
        type(result["value"]) is not int or not 0.65 <= result["confidence"] <= 1
    ):
        label_bbox = diagnostic.get("label_search", {}).get("crop_bbox")
        result["failure_header"] = failure_header.emit(
            episode,
            failed=True,
            frame=frame,
            crop_bbox=failure_header_bbox(frame, label_bbox),
            capture_diagnostics=capture_diagnostic,
        )
        result["failure_header"]["original_ocr_crop_bbox"] = label_bbox
    return result


def observed_board(episode):
    observation = getattr(episode.source, "latest_observation", None)
    board = getattr(observation, "features", {}).get("game_2048", {}).get("board")
    if (
        not isinstance(board, (list, tuple))
        or len(board) != 4
        or any(
            not isinstance(row, (list, tuple))
            or len(row) != 4
            or any(type(cell) is not int for cell in row)
            for row in board
        )
    ):
        return None
    return tuple(tuple(row) for row in board)


def run_episode(
    provider, seed, max_steps, cache, output, *, save_failure_evidence=False
):
    monitor = None
    episode = None
    journal = None
    result = {
        "provider": provider,
        "seed": seed,
        "max_steps": max_steps,
        "actions": 0,
        "invalid_actions": 0,
        "board_changes": 0,
        "policy_reobservations": 0,
        "fresh_input_captures": 0,
        "plans": [],
        "failure": None,
        "stopped_input_rejected": False,
        "stale_generation_rejected": False,
    }
    latencies = []
    started = time.monotonic()
    failure_header = OwnedGameFailureHeader(
        enabled=save_failure_evidence, provider=provider, seed=seed
    )
    phase = "resource_initialization"
    try:
        monitor = PlanningResourceMonitor().start()
        phase = "game_initialization"
        output.mkdir(parents=True, exist_ok=True)
        journal = EventJournal(output / "events.db")
        episode = ReferenceGame2048Episode(
            1,
            cache,
            output,
            None,
            None,
            save_failure_evidence,
            decision_provider=provider,
            planner_conditional=provider == "planner",
            planning_settle_ms=1000,
            native_preprocessing=True,
            wgc_timeout_ms=250,
            test_seed=seed,
            event_journal=journal,
        )
        result["frame_preprocessing_profile"] = getattr(
            episode.source, "frame_preprocessing_profile", "unknown"
        )
        result["seed_fixture"] = episode.launcher.server.test_seed_metadata
        phase = "initial_observation"
        ready_deadline = time.monotonic() + SCREEN_READY_TIMEOUT_SECONDS
        state = episode.observe(timeout_seconds=SCREEN_READY_TIMEOUT_SECONDS)
        result["initial_max_tile"] = state.max_tile
        phase = "preprocessing_validation"
        result["preprocessing_attempts"] = []
        for attempt in range(3):
            parity = episode.validate_preprocessing()
            result["preprocessing_validation"] = parity
            result["preprocessing_attempts"].append(parity)
            if parity["match"] or not (
                parity.get("semantic_available") == [False, False]
                and parity.get("semantic_values_equal") is True
                and all(parity.get("primitive_fields_equal", {}).values())
                and parity.get("candidate_equal") is True
                and parity.get("baseline_values_equal") is True
            ):
                break
            remaining = ready_deadline - time.monotonic()
            if attempt == 2 or remaining <= 0:
                break
            # A new physical observation is required; do not relabel the cached unknown frame.
            episode.source.not_before = max(episode.source.not_before, time.monotonic())
            state = episode.observe(timeout_seconds=remaining)
        if (
            not result["preprocessing_validation"]["match"]
            or time.monotonic() >= ready_deadline
        ):
            raise RuntimeError(
                "explicit native preprocessing inputs differ or remain unavailable within the ready deadline"
            )
        # Validation consumed time; plan from a newly accepted real frame without extending TTL.
        phase = "post_validation_observation"
        state = episode.observe(timeout_seconds=ready_deadline - time.monotonic())
        if time.monotonic() >= ready_deadline:
            raise RuntimeError("comparison initial observation ready deadline expired")
        initial_token = episode.pipeline.controller.rearm_token
        for _ in range(max_steps):
            if state.terminal:
                break
            before = state
            began = time.monotonic_ns()
            phase = "decision_execution"
            if observed_board(episode) is None:
                raise RuntimeError("comparison typed board unavailable before decision")
            result["last_core_reason"] = None
            result["last_policy_reason_codes"] = []
            orchestrator = DecisionOrchestrator(episode.pipeline)
            decision_started_ns = time.monotonic_ns()
            outcome = orchestrator.run(execute=True, purpose=CAMPAIGN_PURPOSE)
            returned_ns = time.monotonic_ns()
            result["decision_attempts"] = list(orchestrator.last_diagnostics)
            latencies.append((time.monotonic_ns() - began) / 1e6)
            planning = episode.pipeline.engine.provider
            plan = getattr(planning, "last_planning_result", None)
            policy = getattr(planning, "last_policy_decision", None)
            result["last_core_reason"] = plan.reason if plan is not None else None
            result["last_policy_reason_codes"] = (
                list(policy.reason_codes) if policy is not None else []
            )
            diagnostics = getattr(planning, "runtime_diagnostics", None)
            result["planning_runtime"] = (
                diagnostics() if callable(diagnostics) else None
            )
            if isinstance(outcome, DecisionDisposition):
                result["withheld"] = outcome.to_dict()
                break
            if not outcome.executed:
                raise RuntimeError("comparison did not execute actual input")
            episode.source.not_before = time.monotonic() + 1
            result["actions"] += 1
            before_board = observed_board(episode)
            backend = getattr(
                getattr(episode.pipeline, "executor", None), "live_executor", None
            )
            attempt_timing = {
                "action_id": outcome.action_id,
                "decision_started_ns": decision_started_ns,
                "returned_ns": returned_ns,
                "dispatch_boundary_ns": getattr(
                    backend, "input_dispatch_started_at_ns", None
                ),
                "before_capture": dict(
                    getattr(episode.source, "latest_capture_diagnostics", {})
                ),
                "post_capture": None,
            }
            result["input_timings"] = [
                *result.get("input_timings", []),
                attempt_timing,
            ][-3:]
            phase = "post_input_observation"
            state = (
                episode.observe()
            )  # explicit receipt completion, including final/terminal action
            result["invalid_actions"] += int(state.signature == before.signature)
            after_board = observed_board(episode)
            attempt_timing["post_capture"] = dict(
                getattr(episode.source, "latest_capture_diagnostics", {})
            )
            result["board_changes"] += int(
                before_board is not None
                and after_board is not None
                and after_board != before_board
            )
            if plan is not None:
                result["planning_budget"] = asdict(planning.search_budget)
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
        phase = "score_evaluation"
        ocr = TesseractOcrRecognizer.optional()
        if ocr is None:
            raise RuntimeError(
                "score evaluation requires the explicit comparison OCR dependency"
            )
        result["score_evaluation"] = evaluate_visible_score(
            episode, ocr, failure_header=failure_header
        )
        score_confidence = result["score_evaluation"].get("confidence")
        if (
            type(result["score_evaluation"]["value"]) is not int
            or not isinstance(score_confidence, (int, float))
            or isinstance(score_confidence, bool)
            or not 0.65 <= score_confidence <= 1
        ):
            raise RuntimeError(
                "post-decision visible score OCR is unavailable; do not claim score comparison"
            )
        records = episode.pipeline.load_execution_history()
        result["execution_history_count"] = len(records)
        phase = "stop_probe"
        token = episode.pipeline.controller.rearm_token
        episode.pipeline.controller.stop("comparison Stop probe")
        try:
            episode.pipeline.run_and_execute(expected_rearm_token=token)
        except ExecutionCancelled:
            result["stopped_input_rejected"] = True
        phase = "stale_generation_probe"
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
            or result["board_changes"] < 1
            or not result["stopped_input_rejected"]
            or not result["stale_generation_rejected"]
        ):
            raise AssertionError(
                "comparison minimum actual input, observed board change or Stop/stale guard failed"
            )
    except Exception as error:
        result["failure_header"] = failure_header.emit(
            episode, failed=True, frame=None, crop_bbox=None, capture_diagnostics={}
        )
        planning = getattr(
            getattr(getattr(episode, "pipeline", None), "engine", None),
            "provider",
            None,
        )
        diagnostics = getattr(planning, "runtime_diagnostics", None)
        if callable(diagnostics):
            result["planning_runtime"] = diagnostics()
        result["failure_stage"] = phase
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    finally:
        timing = getattr(getattr(episode, "source", None), "observation_timing", None)
        result["last_capture"] = dict(
            getattr(getattr(episode, "source", None), "latest_capture_diagnostics", {})
        )
        result["recent_captures"] = list(
            getattr(getattr(episode, "source", None), "recent_capture_diagnostics", [])
        )[-4:]
        result["observation_stage_ms"] = {
            stage: round(seconds * 1000, 3)
            for stage, seconds in getattr(timing, "stage_seconds", {}).items()
            if stage
            in {
                "capture",
                "frame_analysis",
                "board_adaptation",
                "observation_refresh",
                "retry_wait",
            }
        }
        if episode is not None:
            try:
                episode.close()
            except Exception as error:
                result["cleanup_failure"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:1000],
                }
        if journal is not None:
            try:
                journal.close()
            except Exception as error:
                result["journal_cleanup_failure"] = {
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
        and not result.get("journal_cleanup_failure")
    )
    return result


def comparison_summary(result):
    """Bounded operational diagnostics; exclude frames, profiles and environment."""

    def bounded(value, limit=256):
        return (
            str(value).split("\n", 1)[0].replace("\r", " ")[:limit]
            if value is not None
            else None
        )

    score = result.get("score_evaluation") or {}
    confidence = score.get("confidence")
    score_valid = (
        type(score.get("value")) is int
        and type(confidence) in (int, float)
        and 0.65 <= confidence <= 1
    )
    withheld = result.get("withheld") or {}
    failure = result.get("failure") or {}
    return {
        "event": "windows_planning_comparison_episode",
        "provider": bounded(result.get("provider"), 32),
        "seed": result.get("seed"),
        "success": result.get("success", False),
        "failure_stage": bounded(result.get("failure_stage"), 64),
        "failure_type": bounded(failure.get("type"), 64),
        "failure_message": bounded(failure.get("message"), 512),
        "cleanup_failure_type": bounded(
            (result.get("cleanup_failure") or {}).get("type"), 64
        ),
        "resource_failure_type": bounded(
            (result.get("resource_failure") or {}).get("type"), 64
        ),
        "journal_cleanup_failure_type": bounded(
            (result.get("journal_cleanup_failure") or {}).get("type"), 64
        ),
        "disposition": bounded(withheld.get("outcome"), 32),
        "reason_codes": [
            bounded(reason) for reason in withheld.get("reason_codes", ())[:8]
        ],
        "core_reason": bounded(result.get("last_core_reason")),
        "policy_reason_codes": [
            bounded(reason) for reason in result.get("last_policy_reason_codes", ())[:8]
        ],
        "confirmed_actual_actions": result.get("actions", 0),
        "observed_board_changes": result.get("board_changes", 0),
        "frame_preprocessing_profile": result.get("frame_preprocessing_profile"),
        "preprocessing_validation": result.get("preprocessing_validation"),
        "preprocessing_attempts": result.get("preprocessing_attempts", [])[:3],
        "last_capture": result.get("last_capture"),
        "recent_captures": result.get("recent_captures", [])[-4:],
        "input_timings": result.get("input_timings", [])[-3:],
        "planning_runtime": result.get("planning_runtime"),
        "decision_attempts": result.get("decision_attempts", [])[-3:],
        "observation_stage_ms": result.get("observation_stage_ms"),
        "execution_history_count": result.get("execution_history_count"),
        "score_status": "verified_ocr" if score_valid else "unavailable",
        "score": score.get("value") if score_valid else None,
        "score_confidence": score.get("confidence"),
        "score_attempts": score.get("attempts", [])[:2],
        "failure_header": score.get("failure_header", result.get("failure_header")),
        "elapsed_seconds": result.get("wall_seconds"),
        "stop_probe": "rejected"
        if result.get("stopped_input_rejected")
        else "unverified",
        "stale_generation_probe": (
            "rejected" if result.get("stale_generation_rejected") else "unverified"
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=479)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--allow-live-input", action="store_true")
    parser.add_argument("--save-failure-evidence", action="store_true")
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
        "source_at_start": source_fingerprint(),
        "game_source_lock": json.loads(SOURCE_LOCK_PATH.read_text()),
        "seed": args.seed,
        "failure_evidence_enabled": args.save_failure_evidence,
        "max_steps": args.max_steps,
        "profile": "short Windows comparison; independent of required 600s gate",
        "capture_profile": {
            "wgc_timeout_ms": 250,
            "timeout_behavior": "existing same-target GDI fallback; WGC session remains reusable",
            "default_campaign_wgc_timeout_ms": 2000,
            "observation_age_limit_seconds": CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS,
        },
        "persistence_profile": "explicit EventJournal; same append-only path as ManagedSession",
        "frame_preprocessing_profile": "explicit native FAST_CV for all three providers; unavailable capability fails comparison; campaign default remains Python",
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
            provider,
            args.seed,
            args.max_steps,
            args.cache_dir,
            output / provider,
            save_failure_evidence=args.save_failure_evidence,
        )
        print(json.dumps(comparison_summary(result), ensure_ascii=True), flush=True)
        report["episodes"].append(result)
        report["success"] = len(report["episodes"]) == 3 and all(
            e["success"] for e in report["episodes"]
        )
        publish_validation_report(output / "report.json", report)
    report["source_at_end"] = source_fingerprint()
    report["source_consistent"] = report["source_at_start"] == report["source_at_end"]
    binaries = [
        episode.get("preprocessing_validation", {}).get("binary", {}).get("sha256")
        for episode in report["episodes"]
    ]
    report["native_binary_consistent"] = (
        len(binaries) == 3
        and all(isinstance(value, str) and len(value) == 64 for value in binaries)
        and len(set(binaries)) == 1
    )
    report["success"] = (
        report["success"]
        and report["source_consistent"]
        and report["native_binary_consistent"]
    )
    publish_validation_report(output / "report.json", report)
    print(output / "report.json")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
