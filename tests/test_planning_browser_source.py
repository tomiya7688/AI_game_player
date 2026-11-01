from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player.models import ScreenObservation
from tools.check_2048_screen_transition import BrowserScreenSource
from tools.planning_browser_source import FreshCompletedBrowserSource


class PlanningBrowserSourceTests(unittest.TestCase):
    def test_cached_completion_is_deduplicated_and_capture_generation_is_preserved(
        self,
    ):
        source = FreshCompletedBrowserSource.__new__(FreshCompletedBrowserSource)
        source.source_id = "fixture_source"
        source.frame_sequence = 0
        source.completed = []
        source._completed_receipts = set()
        source.cached = None
        controller = SimpleNamespace(rearm_token=7)
        receipt = SimpleNamespace(
            snapshot_id="snapshot",
            action_id="move",
            action_generation=7,
            execution_status="executed",
            input_dispatch_started=True,
            eligible_for_learning=True,
        )
        actual = SimpleNamespace(
            receipt=receipt,
            outcome=SimpleNamespace(status="unknown", state_changed=False),
            after_signature="pixels",
        )
        completed_tokens = []

        def complete(_observation, expected_rearm_token):
            completed_tokens.append(expected_rearm_token)
            return actual

        source.pipeline = SimpleNamespace(
            controller=controller, complete_execution_observation=complete
        )

        def capture(self):
            self.cached = (
                ScreenObservation("fixture", 1, 1, features={"signature": "pixels"}),
                [],
            )
            controller.rearm_token += 1
            return "state"

        with patch.object(BrowserScreenSource, "capture", capture):
            self.assertEqual(source.capture(), "state")
            self.assertEqual(source.capture(), "state")
        self.assertEqual(len(source.completed), 1)
        self.assertEqual(completed_tokens, [7, 8])
        self.assertEqual(
            source.cached[0].features["capture_metadata"]["frame_id"],
            "fixture_source:0:2",
        )

    def test_failed_new_capture_clears_previous_state_observation_and_pixels(self):
        source = FreshCompletedBrowserSource.__new__(FreshCompletedBrowserSource)
        source.last_state = "previous board"
        source.last_observation = ScreenObservation("previous", 1, 1)
        source.last_png = b"previous pixels"
        source.cached = ("previous", [])
        source.pipeline = None
        with patch.object(
            BrowserScreenSource, "capture", side_effect=ValueError("unrecognized cell")
        ):
            with self.assertRaises(ValueError):
                source.capture()
        self.assertIsNone(source.last_state)
        self.assertIsNone(source.last_observation)
        self.assertIsNone(source.last_png)
        self.assertIsNone(source.cached)
