"""Explicit isolated-browser comparison using production decision/execution paths."""

import argparse
from contextlib import ExitStack
from collections import Counter
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time


from tools.planning_comparison_source import source_fingerprint
from tools.planning_browser_source import FreshCompletedBrowserSource
from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.game_2048_transition import ReferenceGame2048TransitionModel
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.event_journal import EventJournal
from ai_game_player.planner_factory import reference_2048_planner_provider
from ai_game_player.planning_contract import SearchBudget
from ai_game_player.run_control import ExecutionCancelled, RunController
from tools.browser_probe_support import new_probe_directory, publish_validation_report
from tools.reference_game_2048_source import PinnedGameSource
from tools.run_2048_campaign import CyclingDirectionProvider


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class SettledBrowserExecutor:
    def __init__(self, page, target_url, before_dispatch):
        self.page, self.target_url, self.before_dispatch = (
            page,
            target_url,
            before_dispatch,
        )
        self.confirmed_key_sends = 0

    def execute(self, candidate):
        keys = {
            "UP": "ArrowUp",
            "RIGHT": "ArrowRight",
            "DOWN": "ArrowDown",
            "LEFT": "ArrowLeft",
        }
        if (
            self.page.is_closed()
            or self.page.url != self.target_url
            or candidate.kind != "key"
            or candidate.label not in keys
        ):
            raise RuntimeError(
                "comparison accepts only arrow candidates for its isolated page"
            )
        self.before_dispatch()
        self.page.keyboard.press(keys[candidate.label])
        # Count immediately after confirmed key send, before any settling error.
        self.confirmed_key_sends += 1
        self.page.wait_for_timeout(1000)
        return ExecutionResult(
            candidate.action_id, True, "browser_test", "arrow key sent to isolated page"
        )


def episode(browser, target, seed, provider_name, max_steps, output):
    from PIL import Image

    context = None
    started = time.monotonic()
    steps, dispositions = [], []
    confirmed_executions = 0
    record = {
        "seed": seed,
        "provider": provider_name,
        "steps": steps,
        "dispositions": dispositions,
    }
    pipeline = None
    cleanup_errors = []

    def close_resource(name, close):
        try:
            close()
        except Exception as error:
            cleanup_errors.append(
                {
                    "resource": name,
                    "type": type(error).__name__,
                    "message": str(error)[:2000],
                }
            )

    source = None
    backend = None
    try:
        context = browser.new_context(
            viewport={"width": 650, "height": 850}, device_scale_factor=1
        )
        context.add_init_script(f"""let testSeed={seed}; Math.random=()=>{{
            testSeed=(1664525*testSeed+1013904223)>>>0; return testSeed/4294967296;
        }};""")
        page = context.new_page()
        page.goto(target)
        page.wait_for_timeout(1000)
        source = FreshCompletedBrowserSource(page, Image)
        controller = RunController()
        provider = (
            reference_2048_planner_provider(
                controller=controller,
                allow_conditional=True,
                search_budget=SearchBudget(
                    max_depth=1,
                    max_nodes=10000,
                    wall_time_ms=400,
                    allow_conditional_selection=True,
                ),
            )
            if provider_name == "planner"
            else (
                BaselineDecisionProvider(min_margin=0.0, min_recognition=0.65)
                if provider_name == "baseline"
                else CyclingDirectionProvider()
            )
        )
        with (
            tempfile.TemporaryDirectory(
                prefix="kadoka-planner-comparison-"
            ) as directory,
            ExitStack() as cleanup,
        ):
            journal = EventJournal(Path(directory) / "events.db")
            cleanup.callback(close_resource, "journal", journal.close)
            pipeline = DecisionPipeline(
                source,
                Path(directory),
                provider,
                dry_run=True,
                controller=controller,
                event_journal=journal,
            )
            cleanup.callback(close_resource, "pipeline", pipeline.close)
            pipeline.executor.close()
            generation = controller.rearm_token
            dispatch_reference = {}

            def final_dispatch_check():
                controller.ensure_running(generation)
                check = getattr(provider, "check_planned_dispatch", None)
                if callable(check):
                    check(generation)
                dispatch_reference["state"] = source.last_state
                dispatch_reference["signature"] = source.last_observation.features.get(
                    "signature"
                )
                dispatch_reference["capture"] = dict(
                    source.last_observation.features["capture_metadata"]
                )

            backend = SettledBrowserExecutor(page, target, final_dispatch_check)
            pipeline.executor = ActionExecutor(
                dry_run=False,
                live_executor=backend,
                run_controller_stop_checker=lambda: not controller.is_running,
            )
            loop = DecisionOrchestrator(pipeline)
            model = ReferenceGame2048TransitionModel()
            state = source.capture()
            source.pipeline = pipeline
            for index in range(max_steps):
                if state is None or state.over or state.won:
                    break
                step_started_ns = time.monotonic_ns()
                cpu_started_ns = time.process_time_ns()
                result = loop.run(
                    execute=True, purpose="screen-only bounded planner comparison"
                )
                decision_execution_ms = (time.monotonic_ns() - step_started_ns) / 1e6
                if isinstance(result, DecisionDisposition):
                    dispositions.append(
                        {
                            "outcome": result.outcome.value,
                            "reason_codes": result.reason_codes,
                            "reobservations": result.reobservations,
                        }
                    )
                    break
                if not result.executed:
                    record["not_executed"] = {
                        "action_id": result.action_id,
                        "detail": result.detail,
                    }
                    break
                confirmed_executions += 1
                before = dispatch_reference.get("state")
                if before is None:
                    raise AssertionError(
                        "execution has no bound observed pre-input board"
                    )
                slide = model.apply_slide(
                    before, result.action_id.removeprefix("2048-move-").upper()
                )
                predicted = {
                    outcome.state.cells for outcome in model.spawn_outcomes(slide)
                }
                state = source.capture()
                matched = state.cells in predicted if state is not None else None
                audit = getattr(provider, "last_planning_audit", {})
                prediction = audit.get("prediction", {})
                planning = {
                    key: prediction[key]
                    for key in (
                        "completed_depth",
                        "continuation_semantics",
                        "counters",
                        "probability_semantics",
                        "residual_unknown_mass",
                        "model_id",
                        "model_version",
                        "evaluator_id",
                        "evaluator_version",
                        "quality_profile_version",
                        "value_unit",
                        "limit_enforcement",
                    )
                    if key in prediction
                }
                planning["policy"] = audit.get("policy", {})
                step = {
                    "index": index,
                    "action_id": result.action_id,
                    "prediction_matched": matched,
                    "merge_score_delta": slide.reward_delta,
                    "oracle_action_changed": slide.changed,
                    "max_tile": max(map(max, state.cells)) if state else None,
                    "planning": planning,
                    "before_capture": dispatch_reference["capture"],
                    "before_signature": dispatch_reference["signature"],
                    "decision_execution_ms": decision_execution_ms,
                    "step_wall_ms": (time.monotonic_ns() - step_started_ns) / 1e6,
                    "host_cpu_ms": (time.process_time_ns() - cpu_started_ns) / 1e6,
                }
                steps.append(step)
                if matched is False:
                    raise AssertionError(
                        "observed board outside reference transition distribution"
                    )
                if index % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "provider": provider_name,
                                "seed": seed,
                                "actions": len(steps),
                                "max_tile": step["max_tile"],
                            }
                        ),
                        flush=True,
                    )
            sent_before_stop = backend.confirmed_key_sends
            controller.stop("comparison Stop test")
            try:
                pipeline.run_and_execute(purpose="verify Stop rejects input")
            except ExecutionCancelled as error:
                if (
                    "comparison Stop test" not in str(error)
                    or backend.confirmed_key_sends != sent_before_stop
                ):
                    raise AssertionError(
                        "Stop evidence or backend send count changed"
                    ) from error
                record["stopped_input_rejected"] = True
                record["key_sends_after_stop"] = (
                    backend.confirmed_key_sends - sent_before_stop
                )
            else:
                raise AssertionError("Stop accepted input")
            record.update(
                {
                    "game_over": state.over if state else None,
                    "game_won": bool(source.victory_observed),
                    "max_tile": max(map(max, state.cells)) if state else None,
                    "step_cap_reached": confirmed_executions == max_steps,
                }
            )
            if len(source.completed) != confirmed_executions:
                raise AssertionError(
                    "unique completed receipt count differs from confirmed executions"
                )
            # Visible score is an evaluation metric, read only after all decisions.
            record["visible_score"] = page.locator(".score-container").evaluate(
                "e=>e.childNodes[0].textContent"
            )
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    finally:
        if source is not None:
            record["game_won"] = bool(source.victory_observed)
            record["terminal_board_unverified"] = source.last_state is None
            record["actual_observations"] = source.completed
            record["execution_status_counts"] = dict(
                Counter(x["execution_status"] for x in source.completed)
            )
            record["outcome_status_counts"] = dict(
                Counter(x["outcome_status"] for x in source.completed)
            )
            if source.last_png:
                (output / f"final-{provider_name}-{seed}.png").write_bytes(
                    source.last_png
                )
        if backend is not None:
            record["confirmed_backend_key_sends"] = backend.confirmed_key_sends
        record["cleanup_errors"] = cleanup_errors
        try:
            if context is not None:
                context.close()
        except Exception as error:
            record["context_cleanup_error"] = {
                "type": type(error).__name__,
                "message": str(error)[:2000],
            }
    record["actions"] = confirmed_executions
    record["validated_transition_count"] = sum(
        step["prediction_matched"] is True for step in steps
    )
    record["transition_mismatch_count"] = sum(
        step["prediction_matched"] is False for step in steps
    )
    record["unverified_transition_count"] = sum(
        step["prediction_matched"] is None for step in steps
    )
    record["invalid_action_count"] = sum(
        step.get("oracle_action_changed") is False for step in steps
    )
    record["invalid_action_rate"] = record["invalid_action_count"] / max(
        1, confirmed_executions
    )
    record["invalid_metric"] = (
        "deterministic no-op under reference model from dispatch-bound observed board; not canonical causal learning evidence"
    )
    record["policy_reobservations"] = sum(
        step.get("planning", {}).get("policy", {}).get("observation_requests", 0)
        for step in steps
    )
    record["policy_reobserve_per_action"] = record["policy_reobservations"] / max(
        1, confirmed_executions
    )
    record["survival_steps"] = confirmed_executions
    record["nodes"] = sum(
        step.get("planning", {}).get("counters", {}).get("nodes_visited", 0)
        for step in steps
    )
    record["duration_seconds"] = time.monotonic() - started
    for metric in ("decision_execution_ms", "step_wall_ms", "host_cpu_ms"):
        samples = [step[metric] for step in steps]
        record[metric] = {
            "p50": statistics.median(samples) if samples else None,
            "p95": statistics.quantiles(samples, n=100, method="inclusive")[94]
            if len(samples) > 1
            else (samples[0] if samples else None),
        }
    try:
        import resource

        record["host_process_peak_rss_kib"] = resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss
    except ImportError:
        record["host_process_peak_rss_kib"] = None
    record["resource_scope"] = (
        "Linux harness host process only; RSS is cumulative high-water and includes retained reports. Browser/worker CPU and RAM are excluded. Both decision_execution_ms and step_wall_ms include 1000ms backend animation settling; neither is planner-only latency."
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in record.items()
                if k not in ("steps", "actual_observations")
            }
        ),
        flush=True,
    )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-input", action="store_true")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=("cycling", "baseline", "planner"),
        default=["cycling", "baseline", "planner"],
    )
    parser.add_argument("--seeds", nargs="+", type=int, default=[479, 480, 481])
    parser.add_argument("--max-steps", type=int, default=180)
    parser.add_argument(
        "--output", type=Path, default=Path("build/transition-2048/planner-comparison")
    )
    args = parser.parse_args()
    if (
        not args.allow_live_input
        or not 1 <= args.max_steps <= 1000
        or not 1 <= len(args.seeds) <= 10
        or len(set(args.seeds)) != len(args.seeds)
        or any(not 0 <= seed <= 0xFFFFFFFF for seed in args.seeds)
        or len(set(args.providers)) != len(args.providers)
    ):
        parser.error("explicit live input and bounded episodes/steps required")
    from playwright.sync_api import sync_playwright

    output = new_probe_directory(args.output)
    pinned = PinnedGameSource(Path("build/reference-game"))
    root = pinned.materialize()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    report = {
        "schema_version": 1,
        "git_sha_at_start": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "git_status_at_start": subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ),
        "source_at_start": source_fingerprint(),
        "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "sensor_sha256": sha256(
            Path(__file__).with_name("planning_browser_source.py").read_bytes()
        ).hexdigest(),
        "pinned_commit": pinned.lock["commit"],
        "profiles": {
            "baseline": {"min_margin": 0.0, "min_recognition": 0.65},
            "planner": {
                "allow_conditional": True,
                "unknown_world_mass": 1,
                "calibrated_accuracy_claim": False,
                "max_depth": 1,
                "max_nodes": 10000,
                "wall_time_ms": 400,
            },
        },
        "settle_milliseconds": 1000,
        "decision_data": "screenshot and existing recognizer only; seeded RNG initialization is test setup, score DOM read only after play",
        "windows_field_play": "Unverified; isolated Chromium input backend",
        "episodes": [],
    }
    report["persistence_profile"] = (
        "explicit EventJournal; same append-only path as ManagedSession"
    )
    report["profiles"]["cycling"] = {
        "provider": "CyclingDirectionProvider",
        "configuration": "existing default",
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/chromium",
                headless=True,
                args=["--no-sandbox"],
            )
            try:
                target = f"http://127.0.0.1:{server.server_port}/index.html"
                for provider in args.providers:
                    for seed in args.seeds:
                        report["episodes"].append(
                            episode(
                                browser, target, seed, provider, args.max_steps, output
                            )
                        )
                        publish_validation_report(output / "report.json", report)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    report["git_sha_at_end"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    report["git_status_at_end"] = subprocess.check_output(
        ["git", "status", "--porcelain"], text=True
    )
    report["source_at_end"] = source_fingerprint()
    report["source_changed_during_run"] = (
        report["source_at_start"] != report["source_at_end"]
    )
    report["harness_sha256_at_end"] = sha256(Path(__file__).read_bytes()).hexdigest()
    report["sensor_sha256_at_end"] = sha256(
        Path(__file__).with_name("planning_browser_source.py").read_bytes()
    ).hexdigest()
    report["source_consistent"] = (
        not report["source_changed_during_run"]
        and report["harness_sha256"] == report["harness_sha256_at_end"]
        and report["sensor_sha256"] == report["sensor_sha256_at_end"]
    )
    report["evidence_directory"] = str(output)
    publish_validation_report(output / "report.json", report)
    report["success"] = report["source_consistent"] and all(
        not item.get("error")
        and not item.get("cleanup_errors")
        and not item.get("context_cleanup_error")
        and item["actions"] > 0
        and item.get("stopped_input_rejected")
        for item in report["episodes"]
    )
    publish_validation_report(output / "report.json", report)
    print(json.dumps({"report": str(output / "report.json")}), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
