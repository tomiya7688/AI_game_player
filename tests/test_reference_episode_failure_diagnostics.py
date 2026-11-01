from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.reference_episode_failure_diagnostics import (
    collect_episode_failure_diagnostics,
)
from tools.run_2048_campaign import ReferenceGame2048Episode
import test_comparison_preflight as fixtures


class EpisodeFailureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events = []
        self.snapshot = Mock(
            side_effect=lambda: (
                self.events.append("snapshot") or {"schema_version": 1, "completed": []}
            )
        )
        self.browser = Mock(
            side_effect=lambda **_: (
                self.events.append("browser")
                or {"status": "available", "ready_state": "complete", "tile_count": 0}
            )
        )
        self.episode = SimpleNamespace(
            _save_browser_diagnostics=True,
            _save_heartbeat_diagnostics=True,
            _failure_diagnostics_attempted=False,
            _failure_diagnostics_path=Path(self.directory.name) / "failure.json",
            pipeline=SimpleNamespace(
                executor=SimpleNamespace(
                    fail_safe_runtime=SimpleNamespace(
                        heartbeat_diagnostic_snapshot=self.snapshot
                    )
                )
            ),
            launcher=SimpleNamespace(collect_failure_render_diagnostics=self.browser),
        )

    def collect(self, frame_preserved):
        with redirect_stdout(io.StringIO()):
            return collect_episode_failure_diagnostics(
                self.episode, frame_preserved=frame_preserved
            )

    def test_default_off_has_no_snapshot_browser_or_file(self):
        self.episode._save_browser_diagnostics = False
        self.episode._save_heartbeat_diagnostics = False
        self.assertIsNone(self.collect(True))
        self.snapshot.assert_not_called()
        self.browser.assert_not_called()
        self.assertFalse(self.episode._failure_diagnostics_path.exists())

    def test_frame_barrier_keeps_heartbeat_but_never_calls_browser(self):
        result = self.collect(False)
        self.assertEqual(result["browser"]["reason"], "failure_frame_not_preserved")
        self.assertEqual(self.events, ["snapshot"])
        self.assertEqual(
            json.loads(self.episode._failure_diagnostics_path.read_text()), result
        )
        self.assertTrue(result["failure_status_unchanged"])

    def test_snapshot_precedes_one_browser_probe_and_idempotent_publication(self):
        first = self.collect(True)
        self.assertEqual(self.events, ["snapshot", "browser"])
        self.assertIs(self.collect(True), first)
        self.assertEqual(self.events, ["snapshot", "browser"])
        self.browser.assert_called_once_with(
            failure_confirmed=True, frame_preserved=True
        )
        self.assertEqual(
            json.loads(self.episode._failure_diagnostics_path.read_text()), first
        )

    def test_browser_error_does_not_discard_valid_heartbeat_or_print_message(self):
        self.browser.side_effect = RuntimeError("secret endpoint and token")
        result = self.collect(True)
        self.assertIn("completed", result["heartbeat"])
        self.assertEqual(result["browser"]["error_type"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(result))

    def test_bad_or_oversized_section_does_not_destroy_the_other_section(self):
        for value in ({"clock": float("nan")}, {"blob": "x" * 33000}):
            with self.subTest(value_type=type(value)):
                self.episode._failure_diagnostics_attempted = False
                self.snapshot.side_effect = None
                self.snapshot.return_value = value
                result = self.collect(True)
                self.assertEqual(
                    result["heartbeat"]["reason"], "invalid_or_oversized_metadata"
                )
                self.assertEqual(result["browser"]["status"], "available")
                json.loads(self.episode._failure_diagnostics_path.read_text())

    def test_file_failure_preserves_original_diagnostic_and_does_not_raise(self):
        with patch(
            "tools.reference_episode_failure_diagnostics.stage_json_write",
            side_effect=OSError("secret path"),
        ):
            result = self.collect(True)
        self.assertEqual(result["persistence_error_type"], "OSError")
        self.assertNotIn("secret", json.dumps(result))
        self.assertIn("completed", result["heartbeat"])

    def test_episode_observe_preserves_original_failure_and_requires_saved_frame(self):
        for frame_preserved in (False, True):
            case = fixtures.ComparisonPreflightTests()
            case.setUp()
            try:
                case.episode._comparison_preflight = False
                case.source._failure_frame_preserved = frame_preserved
                original = RuntimeError("original capture failure")
                with (
                    patch.object(
                        case.source, "capture_playable_state", side_effect=original
                    ),
                    patch.object(
                        case.episode, "collect_failure_diagnostics"
                    ) as collect,
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        case.episode.observe()
                self.assertIs(raised.exception, original)
                collect.assert_called_once_with(frame_preserved=frame_preserved)
                case.pipeline.initialize_safety.assert_not_called()
                case.assert_no_input()
            finally:
                case.doCleanups()

    def test_episode_boundary_cannot_replace_failure_if_diagnostic_helper_raises(self):
        episode = ReferenceGame2048Episode.__new__(ReferenceGame2048Episode)
        episode._save_browser_diagnostics = True
        episode._save_heartbeat_diagnostics = False
        with patch(
            "tools.reference_episode_failure_diagnostics.collect_episode_failure_diagnostics",
            side_effect=MemoryError("diagnostic allocation"),
        ):
            self.assertEqual(
                episode.collect_failure_diagnostics(frame_preserved=False),
                {"status": "unavailable", "error_type": "MemoryError"},
            )

    def test_constructor_enables_recording_before_initialization_only_when_opted_in(
        self,
    ):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                source = SimpleNamespace(close=Mock())
                runtime = SimpleNamespace(configure_heartbeat_diagnostics=Mock())
                pipeline = SimpleNamespace(
                    executor=SimpleNamespace(fail_safe_runtime=runtime),
                    record_observation=Mock(),
                    initialize_safety=Mock(),
                    close=Mock(),
                )
                launcher = SimpleNamespace(
                    start=Mock(),
                    close=Mock(),
                    window_info=SimpleNamespace(handle=123, process_id=456),
                )
                with (
                    patch(
                        "tools.run_2048_campaign.ReferenceGame2048Launcher",
                        return_value=launcher,
                    ) as create_launcher,
                    patch(
                        "tools.run_2048_campaign.Cached2048ObservationSource",
                        return_value=source,
                    ),
                    patch(
                        "tools.run_2048_campaign.DecisionPipeline",
                        return_value=pipeline,
                    ),
                ):
                    episode = ReferenceGame2048Episode(
                        1,
                        Path(self.directory.name),
                        Path(self.directory.name),
                        None,
                        None,
                        save_browser_diagnostics=enabled,
                        save_heartbeat_diagnostics=enabled,
                    )
                try:
                    self.assertFalse(episode._safety_rearmed)
                    pipeline.initialize_safety.assert_not_called()
                    if enabled:
                        runtime.configure_heartbeat_diagnostics.assert_called_once_with(
                            enabled=True
                        )
                        self.assertTrue(
                            create_launcher.call_args.kwargs[
                                "post_failure_render_diagnostics"
                            ]
                        )
                    else:
                        runtime.configure_heartbeat_diagnostics.assert_not_called()
                        self.assertEqual(create_launcher.call_args.kwargs, {})
                finally:
                    episode.close()
