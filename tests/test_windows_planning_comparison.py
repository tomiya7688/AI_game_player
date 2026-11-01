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
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools import compare_2048_windows_planning as comparison


class FakeMonitor:
    def start(self):
        return self

    def close(self):
        return {"samples": 3}


class FakeEpisode:
    def validate_preprocessing(self):
        return {"match": True, "binary": {"sha256": "fixture"}}

    def __init__(self, *args, **kwargs):
        self.wgc_timeout_ms = kwargs.get("wgc_timeout_ms")
        self.records = []
        self.closed = False
        self.launcher = SimpleNamespace(
            server=SimpleNamespace(test_seed_metadata={"seed": kwargs["test_seed"]})
        )
        self.source = SimpleNamespace(not_before=0, latest_observation=None)
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

    def observe(self, **kwargs):
        self.source.latest_observation = ScreenObservation(
            "fixture",
            100,
            100,
            features={
                "game_2048": {
                    "board": [
                        [2 + 2 * len(self.records), 0, 0, 0],
                        [0] * 4,
                        [0] * 4,
                        [0] * 4,
                    ]
                }
            },
        )
        return CampaignState(f"board{len(self.records)}", False, 0, 2)

    def close(self):
        self.closed = True


class WindowsPlanningComparisonTests(unittest.TestCase):
    def test_initial_unknown_requires_fresh_available_parity_before_any_input(self):
        unknown = {
            "match": False,
            "semantic_available": [False, False],
            "semantic_values_equal": True,
            "primitive_fields_equal": {"signature": True},
            "candidate_equal": True,
            "baseline_values_equal": True,
        }
        for recover in (True, False):
            with self.subTest(recover=recover):
                instances = []
                observations = []

                class InitialEpisode(FakeEpisode):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.parity_calls = 0
                        instances.append(self)

                    def observe(self, **kwargs):
                        observations.append((len(self.records), kwargs))
                        return super().observe(**kwargs)

                    def validate_preprocessing(self):
                        self.parity_calls += 1
                        return (
                            {"match": True}
                            if recover and self.parity_calls == 2
                            else unknown
                        )

                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
                    patch.object(
                        comparison, "ReferenceGame2048Episode", InitialEpisode
                    ),
                    patch.object(
                        comparison.TesseractOcrRecognizer,
                        "optional",
                        return_value=object(),
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
                self.assertEqual(result["actions"], 3 if recover else 0)
                self.assertEqual(instances[0].parity_calls, 2 if recover else 3)
                self.assertEqual(
                    len(result["preprocessing_attempts"]), 2 if recover else 3
                )
                self.assertTrue(
                    all(count == 0 for count, _ in observations[: 2 if recover else 3])
                )
                self.assertLessEqual(
                    observations[1][1]["timeout_seconds"],
                    comparison.SCREEN_READY_TIMEOUT_SECONDS,
                )
                self.assertGreater(instances[0].source.not_before, 0)
                self.assertEqual(result["success"], recover)

    def test_initial_available_mismatch_is_not_retried(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", FakeEpisode),
            patch.object(
                FakeEpisode,
                "validate_preprocessing",
                return_value={"match": False, "semantic_available": [True, True]},
            ) as parity,
        ):
            result = comparison.run_episode(
                "planner", 479, 3, Path(directory), Path(directory)
            )
        self.assertEqual(parity.call_count, 1)
        self.assertEqual(result["actions"], 0)
        self.assertFalse(result["success"])

    def test_initial_capture_cannot_extend_the_original_ready_deadline(self):
        now = [100.0]

        class SlowInitialEpisode(FakeEpisode):
            def observe(self, **kwargs):
                now[0] += comparison.SCREEN_READY_TIMEOUT_SECONDS + 1
                return super().observe(**kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", SlowInitialEpisode),
            patch.object(comparison.time, "monotonic", side_effect=lambda: now[0]),
        ):
            result = comparison.run_episode(
                "planner", 479, 3, Path(directory), Path(directory)
            )
        self.assertEqual(result["actions"], 0)
        self.assertFalse(result["success"])
        self.assertIn("ready deadline", result["failure"]["message"])

    def test_fresh_after_parity_cannot_turn_unknown_board_into_an_input(self):
        class UnknownFreshEpisode(FakeEpisode):
            def observe(self, **kwargs):
                state = super().observe(**kwargs)
                self.source.latest_observation.features["game_2048"]["board"][0][0] = (
                    None
                )
                return state

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", UnknownFreshEpisode),
        ):
            result = comparison.run_episode(
                "cycling", 479, 3, Path(directory), Path(directory)
            )
        self.assertEqual(result["actions"], 0)
        self.assertFalse(result["success"])
        self.assertIn("typed board unavailable", result["failure"]["message"])

    def test_failure_header_is_diagnostic_top_strip_without_changing_ocr_region(self):
        frame = ScreenFrame(1000, 800, bytes(1000 * 800 * 4))
        original = [410, 63, 225, 94]
        self.assertEqual(
            comparison.failure_header_bbox(frame, original), [202, 0, 640, 240]
        )
        self.assertEqual(original, [410, 63, 225, 94])
        self.assertEqual(
            comparison.failure_header_bbox(ScreenFrame(20, 10, bytes(800))),
            [0, 0, 20, 10],
        )

    def test_whole_page_number_without_a_score_label_is_never_the_metric(self):
        frame = ScreenFrame(2, 2, bytes(16))
        episode = SimpleNamespace(
            source=SimpleNamespace(
                window_handle=1,
                capture_evaluation_frame=lambda: (
                    frame,
                    {"status": "fresh_evaluation_frame"},
                ),
            )
        )
        observation = ScreenObservation(
            "score",
            2,
            2,
            features={"ocr_candidates": [{"text": "2048", "confidence": 0.99}]},
        )
        with (
            patch.object(comparison.FrameAnalyzer, "analyze", return_value=observation),
            patch.object(
                comparison.ReferenceGame2048Adapter,
                "adapt",
                return_value=(
                    ScreenObservation(
                        "score",
                        2,
                        2,
                        features={
                            "game_2048": {"score": 2048, "score_confidence": 0.99}
                        },
                    ),
                    [],
                ),
            ),
        ):
            result = comparison.evaluate_visible_score(episode, object())
        self.assertIsNone(result["value"])
        self.assertEqual(result["confidence"], 0)
        self.assertEqual(result["attempts"][0]["value"], 2048)
        self.assertEqual(
            result["attempts"][1]["status"], "score_label_unavailable_or_ambiguous"
        )

    def test_short_comparison_requires_actual_inputs_and_stop_stale_rejection(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(
                comparison, "ReferenceGame2048Episode", side_effect=FakeEpisode
            ) as factory,
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
        self.assertEqual(factory.call_args.kwargs["wgc_timeout_ms"], 250)
        self.assertEqual(result["actions"], 3)
        self.assertEqual(result["execution_history_count"], 3)
        self.assertTrue(result["stopped_input_rejected"])
        self.assertTrue(result["stale_generation_rejected"])
        self.assertEqual(result["score_evaluation"]["value"], 8)
        self.assertEqual(len(result["latency_ms"]["samples"]), 3)
        self.assertEqual(result["board_changes"], 3)

    def test_sent_messages_without_observed_board_changes_cannot_pass(self):
        observe = FakeEpisode.observe

        def unchanged(episode, **kwargs):
            state = observe(episode, **kwargs)
            episode.source.latest_observation.features["game_2048"]["board"] = [
                [2, 0, 0, 0],
                [0] * 4,
                [0] * 4,
                [0] * 4,
            ]
            return state

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comparison, "PlanningResourceMonitor", FakeMonitor),
            patch.object(comparison, "ReferenceGame2048Episode", FakeEpisode),
            patch.object(FakeEpisode, "observe", unchanged),
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
                "cycling", 479, 3, Path(directory), Path(directory)
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["actions"], 3)
        self.assertEqual(result["board_changes"], 0)
        self.assertTrue(result["stopped_input_rejected"])
        self.assertIn("observed board change", result["failure"]["message"])

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
