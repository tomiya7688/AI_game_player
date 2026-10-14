import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player.campaign_runner import CampaignFailure, CampaignState
from tools import run_2048_campaign


class ReferenceGame2048EpisodeTests(unittest.TestCase):
    def test_capture_refreshes_fail_safe_and_uses_campaign_observation_timeout(self):
        events = []
        observed_state = CampaignState("board-1", False, 0, 2)

        class FakeSource:
            def capture_playable_state(self):
                events.append("capture")
                return observed_state

            def close(self):
                events.append("source_closed")

        class FakePipeline:
            last_safety_result = None

            def rearm_safety(self):
                events.append("rearmed")

            def record_observation(self):
                events.append("observation_recorded")

            def close(self):
                events.append("pipeline_closed")

        fake_launcher = SimpleNamespace(
            start=lambda: events.append("launcher_started"),
            close=lambda: events.append("launcher_closed"),
            window_info=SimpleNamespace(handle=123, process_id=456),
        )
        fake_source = FakeSource()
        fake_pipeline = FakePipeline()

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(run_2048_campaign, "ReferenceGame2048Launcher", return_value=fake_launcher),
                patch.object(run_2048_campaign, "Cached2048ObservationSource", return_value=fake_source),
                patch.object(run_2048_campaign, "DecisionPipeline", return_value=fake_pipeline) as pipeline_factory,
            ):
                episode = run_2048_campaign.ReferenceGame2048Episode(
                    1,
                    Path(directory) / "cache",
                    Path(directory) / "runtime",
                    None,
                    None,
                )
                result = episode.observe()
                observation_timeout = pipeline_factory.call_args.kwargs["fail_safe_config"].observation_timeout_seconds
                episode.close()

        self.assertIs(result, observed_state)
        self.assertEqual(observation_timeout, 5.0)
        self.assertEqual(
            events[:3],
            ["launcher_started", "rearmed", "capture"],
        )
        self.assertEqual(events[3:], ["observation_recorded", "pipeline_closed", "source_closed", "launcher_closed"])

    def test_action_failure_report_keeps_the_fail_safe_stop_reason(self):
        def fail_action(**_arguments):
            raise RuntimeError("FailSafeRuntime blocked action [runtime_not_active]")

        episode = run_2048_campaign.ReferenceGame2048Episode.__new__(run_2048_campaign.ReferenceGame2048Episode)
        episode.pipeline = SimpleNamespace(
            run_and_execute=fail_action,
            last_safety_result=None,
            executor=SimpleNamespace(
                fail_safe_runtime=SimpleNamespace(
                    state=SimpleNamespace(value="recovery_required"),
                    reason="observation_stale",
                )
            ),
        )

        with self.assertRaises(CampaignFailure) as raised:
            episode.act()

        self.assertEqual(raised.exception.category, "safety_block")
        self.assertIn("reason=observation_stale", raised.exception.detail)


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("KADOKA_RUN_2048_INTEGRATION") == "1",
    "real browser restart integration runs only in the Windows E2E workflow",
)
class WindowsReferenceGameCampaignRestartTests(unittest.TestCase):
    def test_second_episode_can_capture_after_the_first_episode_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            cache_root = temporary_root / "source-cache"
            runtime_root = temporary_root / "runtime"

            for episode_number in (1, 2):
                print(f"2048 campaign restart integration: starting episode {episode_number}", flush=True)
                episode = run_2048_campaign.ReferenceGame2048Episode(
                    episode_number,
                    cache_root,
                    runtime_root,
                    None,
                    None,
                )
                try:
                    state = episode.observe()
                    self.assertTrue(state.signature)
                    self.assertFalse(state.terminal)
                    print(f"2048 campaign restart integration: episode {episode_number} observed a playable board", flush=True)
                finally:
                    episode.close()
                    print(f"2048 campaign restart integration: episode {episode_number} closed", flush=True)


if __name__ == "__main__":
    unittest.main()
