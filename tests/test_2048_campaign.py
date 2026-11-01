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
        current_time = [100.0]
        observed_state = CampaignState("board-1", False, 0, 2)

        class FakeSource:
            def capture_playable_state(self, on_fresh_frame=None):
                events.append("capture")
                if on_fresh_frame is None:
                    current_time[0] += (
                        run_2048_campaign.CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS + 1.0
                    )
                else:
                    events.append("fresh_frame")
                    on_fresh_frame()
                return observed_state

            def close(self):
                events.append("source_closed")

        class FakePipeline:
            last_safety_result = None
            observation_deadline = None

            def rearm_safety(self):
                events.append("rearmed")
                self.observation_deadline = (
                    current_time[0]
                    + run_2048_campaign.CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS
                )

            def record_observation(self):
                if current_time[0] > self.observation_deadline:
                    raise RuntimeError(
                        "first observation expired before it could be recorded"
                    )
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

        test_directory = Path("build") / "issue-459-observation-test"
        with (
            patch.object(
                run_2048_campaign,
                "ReferenceGame2048Launcher",
                return_value=fake_launcher,
            ),
            patch.object(
                run_2048_campaign,
                "Cached2048ObservationSource",
                return_value=fake_source,
            ),
            patch.object(
                run_2048_campaign, "DecisionPipeline", return_value=fake_pipeline
            ) as pipeline_factory,
        ):
            episode = run_2048_campaign.ReferenceGame2048Episode(
                1,
                test_directory / "cache",
                test_directory / "runtime",
                None,
                None,
            )
            result = episode.observe()
            second_result = episode.observe()
            observation_timeout = pipeline_factory.call_args.kwargs[
                "fail_safe_config"
            ].observation_timeout_seconds
            episode.close()

        self.assertIs(result, observed_state)
        self.assertIs(second_result, observed_state)
        self.assertEqual(
            observation_timeout,
            run_2048_campaign.CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            events[:3],
            ["launcher_started", "capture", "rearmed"],
        )
        self.assertEqual(
            events[3:],
            [
                "observation_recorded",
                "capture",
                "fresh_frame",
                "observation_recorded",
                "pipeline_closed",
                "source_closed",
                "launcher_closed",
            ],
        )

    def test_capture_refreshes_observation_immediately_after_raw_frame(self):
        events = []

        class FakeCapture:
            frame_number = 0

            def capture(self, _window_handle):
                self.frame_number += 1
                events.append(f"captured-{self.frame_number}")
                return self.frame_number

        class FakeAnalyzer:
            def analyze(self, frame, _screen_id):
                events.append(f"analyzed-{frame}")
                return SimpleNamespace(features={"signature": f"frame-{frame}"})

        class FakeAdapter:
            def adapt(self, frame, _observation):
                events.append(f"adapted-{frame}")
                state = SimpleNamespace(
                    features={
                        "game_2048": {
                            "board": [[2]],
                            "game_over": False,
                            "score": 0,
                            "state_signature": f"board-{frame}",
                        }
                    }
                )
                candidates = [] if frame == 1 else [object()]
                return state, candidates

        source = run_2048_campaign.Cached2048ObservationSource.__new__(
            run_2048_campaign.Cached2048ObservationSource
        )
        source.window_handle = 123
        source.capture = FakeCapture()
        source.analyzer = FakeAnalyzer()
        source.adapter = FakeAdapter()
        source._cached_read = None
        source.failure_evidence = None

        with patch.object(run_2048_campaign, "SCREEN_READY_POLL_SECONDS", 0):
            state = source.capture_playable_state(
                on_fresh_frame=lambda: events.append("observation_recorded")
            )

        self.assertEqual(state.signature, "board-2")
        self.assertEqual(
            events,
            [
                "captured-1",
                "observation_recorded",
                "analyzed-1",
                "adapted-1",
                "captured-2",
                "observation_recorded",
                "analyzed-2",
                "adapted-2",
            ],
        )
        self.assertEqual(len(source.read()[1]), 1)
        self.assertEqual(
            {
                "capture",
                "observation_refresh",
                "frame_analysis",
                "board_adaptation",
                "retry_wait",
            },
            set(source.observation_timing.snapshot()["stage_ms"]),
        )
        self.assertIsNone(source.observation_timing.snapshot()["active_stage"])

    def test_fresh_frame_observation_rejection_remains_a_safety_block(self):
        def reject_observation():
            raise RuntimeError("reason=observation_stale")

        episode = run_2048_campaign.ReferenceGame2048Episode.__new__(
            run_2048_campaign.ReferenceGame2048Episode
        )
        episode.source = SimpleNamespace(
            capture_playable_state=lambda on_fresh_frame: on_fresh_frame()
        )
        episode.pipeline = SimpleNamespace(record_observation=reject_observation)
        episode._safety_rearmed = True

        with self.assertRaises(CampaignFailure) as raised:
            episode.observe()

        self.assertEqual(raised.exception.category, "safety_block")
        self.assertIn("first_observation=False", raised.exception.detail)
        self.assertIn("capture_elapsed_seconds=", raised.exception.detail)
        self.assertIn("reason=observation_stale", raised.exception.detail)

    def test_post_action_completion_keeps_generation_from_capture_start(self):
        controller = SimpleNamespace(rearm_token=7)
        completed = []
        screen = object()
        state = CampaignState("board", False, 0, 2)

        def capture(on_fresh_frame):
            controller.rearm_token = 8
            return state

        episode = run_2048_campaign.ReferenceGame2048Episode.__new__(
            run_2048_campaign.ReferenceGame2048Episode
        )
        episode.source = SimpleNamespace(
            capture_playable_state=capture, latest_observation=screen
        )
        episode.pipeline = SimpleNamespace(
            controller=controller,
            complete_execution_observation=lambda observed, expected_rearm_token: (
                completed.append((observed, expected_rearm_token))
            ),
        )
        episode._safety_rearmed = True
        self.assertIs(episode.observe(), state)
        self.assertEqual(completed, [(screen, 7)])

    def test_recognition_timeout_reports_capture_diagnostics(self):
        class FakeCapture:
            frame_number = 0

            def capture(self, _window_handle):
                self.frame_number += 1
                return self.frame_number

        class FakeAnalyzer:
            def analyze(self, frame, _screen_id):
                return SimpleNamespace(features={"signature": f"frame-{frame}"})

        class FakeAdapter:
            minimum_candidate_confidence = 0.65

            def adapt(self, _frame, _observation):
                return (
                    SimpleNamespace(
                        features={
                            "game_2048": {
                                "board": None,
                                "game_over": None,
                                "confidence": 0.417334,
                            }
                        }
                    ),
                    [],
                )

        source = run_2048_campaign.Cached2048ObservationSource.__new__(
            run_2048_campaign.Cached2048ObservationSource
        )
        source.window_handle = 123
        source.capture = FakeCapture()
        source.analyzer = FakeAnalyzer()
        source.adapter = FakeAdapter()
        source._cached_read = None
        source.failure_evidence = None
        current_time = [0.0]

        def advance_monotonic_clock():
            current_time[0] += 0.5
            return current_time[0]

        with (
            patch.object(run_2048_campaign, "SCREEN_READY_TIMEOUT_SECONDS", 1.0),
            patch.object(run_2048_campaign, "SCREEN_READY_POLL_SECONDS", 0.5),
            patch.object(
                run_2048_campaign.time, "monotonic", side_effect=advance_monotonic_clock
            ),
            patch.object(run_2048_campaign.time, "sleep"),
        ):
            with self.assertRaises(CampaignFailure) as raised:
                source.capture_playable_state()

        self.assertEqual(raised.exception.category, "recognition_failure")
        self.assertIn("capture_attempts=1", raised.exception.detail)
        self.assertIn("board_detected=False", raised.exception.detail)
        self.assertIn("game_over=None", raised.exception.detail)
        self.assertIn("actionable_candidate_count=0", raised.exception.detail)
        self.assertIn("distinct_frame_signatures=1", raised.exception.detail)
        self.assertIn("last_frame_signature=frame-1", raised.exception.detail)
        self.assertIn("candidate_confidence_threshold=0.65", raised.exception.detail)
        self.assertIn("board_confidence=0.417334", raised.exception.detail)

    def test_action_failure_report_keeps_the_fail_safe_stop_reason(self):
        def fail_action(**_arguments):
            raise RuntimeError("FailSafeRuntime blocked action [runtime_not_active]")

        episode = run_2048_campaign.ReferenceGame2048Episode.__new__(
            run_2048_campaign.ReferenceGame2048Episode
        )
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
                print(
                    f"2048 campaign restart integration: starting episode {episode_number}",
                    flush=True,
                )
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
                    print(
                        f"2048 campaign restart integration: episode {episode_number} observed a playable board",
                        flush=True,
                    )
                finally:
                    episode.close()
                    print(
                        f"2048 campaign restart integration: episode {episode_number} closed",
                        flush=True,
                    )


if __name__ == "__main__":
    unittest.main()
