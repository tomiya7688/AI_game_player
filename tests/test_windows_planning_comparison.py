from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.campaign_contract import CampaignState
from ai_game_player.run_control import RunController
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
