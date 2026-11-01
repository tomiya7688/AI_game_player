from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.runtime.contracts import RuntimeCapability
from ai_game_player.runtime.native import NativeLoadStatus
from ai_game_player.campaign_runner import CampaignFailure
from tools.run_2048_campaign import Cached2048ObservationSource
from tools.compare_2048_windows_planning import evaluate_visible_score


class WindowsPlanningFrameFreshnessTests(unittest.TestCase):
    def test_frame_expiring_during_analysis_cannot_publish_candidates_or_cache(self):
        source = self.source([35.1])
        source.source_id = "source"
        source.failure_evidence = None
        now = [40.0]

        def slow_analysis(*args):
            now[0] = 41.0
            return ScreenObservation("frame", 1, 1, features={"signature": "pixels"})

        source.analyzer = SimpleNamespace(analyze=slow_analysis)
        observation = ScreenObservation(
            "frame",
            1,
            1,
            features={
                "game_2048": {
                    "board": [[2]],
                    "state_signature": "board",
                    "game_over": False,
                }
            },
        )
        source.adapter = SimpleNamespace(adapt=lambda *args: (observation, [object()]))
        callback = Mock()
        with (
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
            patch(
                "tools.run_2048_campaign.time.get_clock_info",
                return_value=SimpleNamespace(
                    implementation="QueryPerformanceCounter()"
                ),
            ),
        ):
            with self.assertRaises(CampaignFailure):
                source.capture_playable_state(on_fresh_frame=callback)
        callback.assert_called_once()
        self.assertEqual(source.capture_sequence, 9)
        self.assertIsNone(source._cached_read)
        self.assertIsNone(source.latest_observation)
        self.assertEqual(source._last_accepted_frame_ns, 21_000_000_000)

    def source(self, times):
        frames = iter(
            ScreenFrame(1, 1, bytes(4), captured_at=t, frame_id=i + 1)
            for i, t in enumerate(times)
        )
        source = Cached2048ObservationSource.__new__(Cached2048ObservationSource)
        source.window_handle = 1
        source.capture = SimpleNamespace(
            capture=lambda handle: next(frames),
            last_capture_backend="wgc",
            last_backend_error=None,
        )
        source.not_before = 20
        source._last_accepted_frame_ns = 21_000_000_000
        source._cached_read = "original cache"
        source.latest_observation = "original observation"
        source.capture_sequence = 9
        return source

    def test_stale_score_frame_cannot_bypass_settle_and_last_accepted_barriers(self):
        source = self.source([10, 20.5])
        with (
            patch("tools.run_2048_campaign.time.monotonic", return_value=40),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns", return_value=40_000_000_000
            ),
            patch(
                "tools.run_2048_campaign.time.get_clock_info",
                return_value=SimpleNamespace(
                    implementation="QueryPerformanceCounter()"
                ),
            ),
        ):
            result = evaluate_visible_score(SimpleNamespace(source=source), object())
        self.assertIsNone(result["value"])
        self.assertEqual(result["capture"]["minimum_produced_at_ns"], 21_000_000_000)
        self.assertEqual(result["capture"]["produced_at_ns"], 20_500_000_000)
        self.assertEqual(source._cached_read, "original cache")
        self.assertEqual(source.latest_observation, "original observation")
        self.assertEqual(source.capture_sequence, 9)

    def test_unknown_or_future_clock_is_rejected_without_timestamp_rewrite(self):
        for implementation, stamp in [
            ("unrelated clock", 22),
            ("QueryPerformanceCounter()", 50),
            ("QueryPerformanceCounter()", 0),
        ]:
            source = self.source([stamp])
            with (
                self.subTest(implementation=implementation),
                patch("tools.run_2048_campaign.time.monotonic", return_value=40),
                patch(
                    "tools.run_2048_campaign.time.monotonic_ns",
                    return_value=40_000_000_000,
                ),
                patch(
                    "tools.run_2048_campaign.time.get_clock_info",
                    return_value=SimpleNamespace(implementation=implementation),
                ),
            ):
                frame, diagnostic = source.capture_evaluation_frame()
            self.assertIsNone(frame)
            self.assertEqual(diagnostic["status"], "evaluation_clock_unverified")
            self.assertEqual(diagnostic["produced_at_ns"], stamp * 1_000_000_000)

    def test_queued_old_frame_does_not_refresh_lease_or_publish_a_new_source_id(self):
        source = self.source([22, 39])
        source.source_id = "source"
        source.failure_evidence = None
        source.analyzer = SimpleNamespace(
            analyze=Mock(
                return_value=ScreenObservation(
                    "frame", 1, 1, features={"signature": "pixels"}
                )
            )
        )
        observation = ScreenObservation(
            "frame",
            1,
            1,
            features={
                "game_2048": {
                    "board": [[2]],
                    "state_signature": "board",
                    "game_over": False,
                }
            },
        )
        source.adapter = SimpleNamespace(
            adapt=lambda frame, obs: (observation, [object()])
        )
        callback = Mock()
        with (
            patch("tools.run_2048_campaign.time.monotonic", return_value=40),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns", return_value=40_000_000_000
            ),
            patch(
                "tools.run_2048_campaign.time.get_clock_info",
                return_value=SimpleNamespace(
                    implementation="QueryPerformanceCounter()"
                ),
            ),
        ):
            source.capture_playable_state(on_fresh_frame=callback)
        callback.assert_called_once()
        source.analyzer.analyze.assert_called_once()
        self.assertEqual(source.capture_sequence, 10)
        self.assertEqual(
            source.latest_observation.features["capture_metadata"]["monotonic_ns"],
            39_000_000_000,
        )

    def test_native_capability_is_required_and_close_attempts_both_owned_resources(
        self,
    ):
        for supported in (False, True):
            runtime = SimpleNamespace(
                descriptor=SimpleNamespace(
                    capabilities=frozenset({RuntimeCapability.FAST_CV})
                    if supported
                    else frozenset()
                ),
                close=Mock(),
            )
            capture = SimpleNamespace(close=Mock())
            loaded = SimpleNamespace(status=NativeLoadStatus.AVAILABLE, runtime=runtime)
            with (
                self.subTest(supported=supported),
                patch(
                    "ai_game_player.runtime.native.discover_native_runtime",
                    return_value=loaded,
                ),
                patch(
                    "tools.run_2048_campaign.WindowsScreenCapture", return_value=capture
                ),
            ):
                if not supported:
                    with self.assertRaises(RuntimeError):
                        Cached2048ObservationSource(1, None, native_preprocessing=True)
                else:
                    source = Cached2048ObservationSource(
                        1, None, native_preprocessing=True
                    )
                    self.assertIs(source.analyzer.frame_preprocessor, runtime)
                    capture.close.side_effect = RuntimeError("primary capture close")
                    runtime.close.side_effect = RuntimeError("secondary native close")
                    with self.assertRaisesRegex(RuntimeError, "primary capture close"):
                        source.close()
                    capture.close.assert_called_once()
            runtime.close.assert_called_once()
