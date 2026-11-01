from pathlib import Path
from contextlib import redirect_stdout
import io
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.campaign_contract import CampaignState
from ai_game_player.run_control import RunController
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.planning_outcome import PlanningOutcome
from tools import compare_2048_windows_planning as comparison


class FakeMonitor:
    def start(self):
        return self

    def close(self):
        return {"samples": 3}


class FakeEpisode:
    def __init__(self, *args, **kwargs):
        self.records = []
        self.closed = False
        self.launcher = SimpleNamespace(
            server=SimpleNamespace(test_seed_metadata={"seed": kwargs["test_seed"]})
        )
        self.source = SimpleNamespace(not_before=0)
        self.pipeline = SimpleNamespace(
            controller=RunController(),
            engine=SimpleNamespace(provider=SimpleNamespace()),
            run_and_execute=self.execute,
            load_execution_history=lambda: self.records,
        )

    def execute(self, expected_rearm_token=None, **kwargs):
        self.pipeline.controller.ensure_running(expected_rearm_token)
        result = ExecutionResult("move", True, "fixture", "sent")
        self.records.append(result)
        return result

    def observe(self):
        return CampaignState(f"board{len(self.records)}", False, 0, 2)

    def close(self):
        self.closed = True


class WindowsPlanningComparisonTests(unittest.TestCase):
    def test_short_comparison_requires_actual_inputs_and_stop_stale_rejection(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", FakeEpisode),
            (
                patch.object(
                    comparison.TesseractOcrRecognizer, "optional", return_value=object()
                )
            ),
            (
                patch.object(
                    comparison,
                    "evaluate_visible_score",
                    return_value={"value": 8, "confidence": 0.9},
                )
            ),
        ):
            result = comparison.run_episode(
                "planner", 479, 3, Path(directory), Path(directory)
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], 3)
        self.assertEqual(result["execution_history_count"], 3)
        self.assertTrue(result["stopped_input_rejected"])
        self.assertTrue(result["stale_generation_rejected"])
        self.assertEqual(result["score_evaluation"]["value"], 8)
        self.assertEqual(len(result["latency_ms"]["samples"]), 3)

    def test_missing_score_is_failure_and_owned_episode_is_closed(self):
        instances = []

        def factory(*args, **kwargs):
            episode = FakeEpisode(*args, **kwargs)
            instances.append(episode)
            return episode

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", side_effect=factory),
            (
                patch.object(
                    comparison.TesseractOcrRecognizer, "optional", return_value=object()
                )
            ),
            (
                patch.object(
                    comparison, "evaluate_visible_score", return_value={"value": None}
                )
            ),
        ):
            result = comparison.run_episode(
                "baseline", 479, 3, Path(directory), Path(directory)
            )
        self.assertFalse(result["success"])
        self.assertIn("visible score OCR", result["failure"]["message"])
        self.assertTrue(instances[0].closed)
        summary = comparison.comparison_summary(result)
        self.assertEqual(summary["failure_stage"], "score_evaluation")
        self.assertEqual(summary["confirmed_actual_actions"], 3)
        self.assertEqual(summary["score_status"], "unavailable")
        self.assertEqual(summary["stop_probe"], "unverified")

    def test_missing_optional_sampler_becomes_a_reported_failure(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                comparison,
                "PlanningResourceMonitor",
                side_effect=ImportError("optional sampler unavailable"),
            ),
        ):
            result = comparison.run_episode(
                "planner", 479, 3, Path(directory), Path(directory)
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["failure"]["type"], "ImportError")
        self.assertIsNone(result["resources"])

    def test_nonselect_preserves_current_core_reason_without_fake_input(self):
        def factory(*args, **kwargs):
            episode = FakeEpisode(*args, **kwargs)
            episode.pipeline.engine.provider.last_planning_result = SimpleNamespace(
                reason="invalid_model_or_quality"
            )
            episode.pipeline.engine.provider.last_policy_decision = SimpleNamespace(
                reason_codes=("planning_unavailable",)
            )
            return episode

        withheld = DecisionDisposition(
            PlanningOutcome.ABSTAIN, ("planning_unavailable",), "planner"
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", side_effect=factory),
            patch.object(comparison.DecisionOrchestrator, "run", return_value=withheld),
            patch.object(
                comparison.TesseractOcrRecognizer, "optional", return_value=object()
            ),
            patch.object(
                comparison,
                "evaluate_visible_score",
                return_value={"value": 0, "confidence": 0.9},
            ),
        ):
            result = comparison.run_episode(
                "planner", 479, 3, Path(directory), Path(directory)
            )
        summary = comparison.comparison_summary(result)
        self.assertFalse(summary["success"])
        self.assertEqual(summary["core_reason"], "invalid_model_or_quality")
        self.assertEqual(summary["policy_reason_codes"], ["planning_unavailable"])
        self.assertEqual(summary["disposition"], "abstain")
        self.assertEqual(summary["confirmed_actual_actions"], 0)
        self.assertEqual(summary["score"], 0)
        self.assertEqual(summary["stop_probe"], "rejected")

    def test_cli_emits_bounded_episode_summary_before_report_publication(self):
        captured = io.StringIO()
        published_counts = []

        def episode(provider, *args, **kwargs):
            return {
                "provider": provider,
                "seed": 479,
                "success": False,
                "actions": 0,
                "failure": {
                    "type": "RuntimeError",
                    "message": "x" * 2000 + "\nsecond line",
                },
                "failure_stage": "game_initialization",
                "wall_seconds": 1.5,
                "raw_frame": "must not be logged",
                "profile": {"private": True},
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "os", SimpleNamespace(name="nt")),
            patch.object(
                comparison, "new_probe_directory", return_value=Path(directory)
            ),
            patch.object(comparison, "run_episode", side_effect=episode),
            patch.object(comparison.subprocess, "check_output", return_value="test\n"),
            patch.object(
                comparison, "source_fingerprint", return_value={"hash": "same"}
            ),
            patch.object(
                comparison,
                "publish_validation_report",
                side_effect=lambda *args: published_counts.append(
                    len(captured.getvalue().splitlines())
                ),
            ),
            redirect_stdout(captured),
        ):
            self.assertEqual(comparison.main(["--allow-live-input"]), 1)
        lines = captured.getvalue().splitlines()
        summaries = [json.loads(line) for line in lines[:3]]
        self.assertEqual(
            [s["provider"] for s in summaries], ["cycling", "baseline", "planner"]
        )
        self.assertEqual(published_counts, [1, 2, 3, 3])
        self.assertTrue(all(len(s["failure_message"]) == 512 for s in summaries))
        self.assertNotIn("must not be logged", captured.getvalue())
        self.assertNotIn("private", captured.getvalue())
