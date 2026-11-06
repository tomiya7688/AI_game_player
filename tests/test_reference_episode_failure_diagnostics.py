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
            json.loads(
                self.episode._failure_diagnostics_path.read_text(encoding="utf-8")
            ),
            result,
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
            json.loads(
                self.episode._failure_diagnostics_path.read_text(encoding="utf-8")
            ),
            first,
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
                json.loads(
                    self.episode._failure_diagnostics_path.read_text(encoding="utf-8")
                )

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


class CorrelationSectionTests(unittest.TestCase):
    """Issue #791 T8: 相関節の時刻順・Frame結果・byte上限・startup_summary。"""

    def setUp(self):
        from test_reference_startup_correlation import make_record

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        record = make_record(frame={"empty_board": False, "board_accepted": False})
        self.record = record
        self.calls = []
        server = SimpleNamespace(
            test_seed=481,
            correlation_snapshot=lambda: (
                self.calls.append("preserved") or {"now_ms": 110.0}
            ),
            relative_ms=lambda ns: round(ns / 1_000_000 - 1_000_000, 1),
        )
        render = record["cdp"]["render"]

        def browser(**_):
            self.calls.append("browser")
            return {
                "status": "observed",
                "render": render,
                "expression_sha256": "e" * 64,
                "correlation_parts": {
                    "correlation_id": record["correlation_id"],
                    "server": {**record["server"]},
                    "expected_body_sha256": record["expected_body_sha256"],
                    "cdp_started_ms": 120.0,
                    "cdp_finished_ms": 130.0,
                    "target_matches_generation": True,
                    "browser_version": "1.2.3.4",
                },
            }

        self.episode = SimpleNamespace(
            episode_number=1,
            _save_browser_diagnostics=True,
            _save_heartbeat_diagnostics=False,
            _failure_diagnostics_attempted=False,
            _failure_diagnostics_path=Path(self.directory.name) / "failure.json",
            _failure_header_digests={"frame_sha256": "f" * 64, "png_sha256": "p" * 64},
            source=SimpleNamespace(
                _failure_frame_probe={
                    "captured_at": 1_000.1,
                    "state": {"unavailable_reason": None, "state_available": False},
                }
            ),
            launcher=SimpleNamespace(
                server=server, collect_failure_render_diagnostics=browser
            ),
        )

    def collect(self):
        with redirect_stdout(io.StringIO()):
            return collect_episode_failure_diagnostics(
                self.episode, frame_preserved=True
            )

    def test_correlation_section_orders_frame_before_cdp_and_classifies(self):
        result = self.collect()
        correlation = result["correlation"]
        frame, cdp = correlation["frame"], correlation["cdp"]
        self.assertEqual(self.calls, ["preserved", "browser"])
        self.assertLessEqual(frame["captured_ms"], frame["preserved_ms"])
        self.assertLessEqual(frame["preserved_ms"], cdp["started_ms"])
        self.assertLessEqual(cdp["started_ms"], cdp["finished_ms"])
        self.assertIs(frame["empty_board"], False)
        self.assertIs(frame["board_accepted"], False)
        self.assertEqual(frame["frame_sha256"], "f" * 64)
        self.assertEqual(
            correlation["classification"]["category"], "frame_nonempty_not_recognized"
        )
        self.assertNotIn("correlation_parts", result["browser"])
        self.assertEqual(correlation["seed"], 481)

    def test_missing_frame_probe_fails_closed_without_guessing(self):
        self.episode.source = SimpleNamespace()
        classification = self.collect()["correlation"]["classification"]
        self.assertEqual(classification["category"], "evidence_incomplete")
        self.assertIn("time_order", classification["missing"])
        self.assertIn("frame_result", classification["missing"])

    def test_empty_nonterminal_state_is_recorded_as_an_empty_board(self):
        self.episode.source._failure_frame_probe["state"] = {
            "unavailable_reason": "empty_nonterminal",
            "state_available": False,
        }
        frame = self.collect()["correlation"]["frame"]
        self.assertIs(frame["empty_board"], True)

    def test_without_parts_there_is_no_correlation_section(self):
        original = self.episode.launcher.collect_failure_render_diagnostics

        def without_parts(**kwargs):
            value = original(**kwargs)
            value.pop("correlation_parts")
            return value

        self.episode.launcher.collect_failure_render_diagnostics = without_parts
        self.assertNotIn("correlation", self.collect())

    def test_file_stays_within_the_diagnostic_byte_limit(self):
        self.collect()
        size = self.episode._failure_diagnostics_path.stat().st_size
        self.assertLess(size, 64 * 1024)
        written = json.loads(
            self.episode._failure_diagnostics_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            written["correlation"]["schema"], "reference-startup-correlation/v1"
        )

    def test_startup_summary_contains_only_fixed_non_secret_fields(self):
        server = SimpleNamespace(
            test_seed=7,
            correlation_snapshot=lambda: {
                "correlation_id": "a" * 32,
                "server": {
                    "connections_accepted": 3,
                    "connections_awaiting_request_line": 0,
                    "connections_without_request_line": 1,
                    "request_line_errors": 0,
                    "requests_parsed": 2,
                },
            },
            relative_ms=lambda ns: 1234.5,
        )
        episode = object.__new__(ReferenceGame2048Episode)
        episode._save_browser_diagnostics = True
        episode.episode_number = 2
        episode.launcher = SimpleNamespace(server=server, browser_executable=None)
        episode.source = SimpleNamespace(first_board_accepted_ns=5)
        with patch.dict(
            "os.environ",
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_RUN_ID": "9",
                "GITHUB_RUN_ATTEMPT": "",
                "GITHUB_SHA": "",
            },
        ):
            summary = episode.startup_summary()
        self.assertEqual(summary["first_board_accepted_ms"], 1234.5)
        self.assertEqual(summary["server_started_ms"], 0)
        self.assertEqual(
            summary["server_connection_summary"]["connections_without_request_line"], 1
        )
        self.assertEqual(summary["ci_context"]["event"], "pull_request")
        self.assertIsNone(summary["ci_context"]["run_attempt"])
        self.assertNotIn("http", json.dumps(summary))
        episode.source = SimpleNamespace(first_board_accepted_ns=None)
        self.assertIsNone(episode.startup_summary()["first_board_accepted_ms"])
        episode._save_browser_diagnostics = False
        self.assertIsNone(episode.startup_summary())

    def test_startup_summary_file_is_saved_at_close_and_never_raises(self):
        episode = object.__new__(ReferenceGame2048Episode)
        episode._failure_diagnostics_path = Path(self.directory.name) / "failure.json"
        episode._save_startup_summary({"schema": "reference-startup-summary/v1"})
        saved = Path(self.directory.name) / "startup-summary.json"
        self.assertEqual(
            json.loads(saved.read_text(encoding="utf-8"))["schema"],
            "reference-startup-summary/v1",
        )
        episode._failure_diagnostics_path = (
            Path(self.directory.name) / "missing" / "x" / "f.json"
        )
        with patch("ai_game_player.atomic_json.stage_json_write", side_effect=OSError):
            episode._save_startup_summary({"schema": "x"})
        episode._save_startup_summary(None)


class CiContextTests(unittest.TestCase):
    def test_only_identifier_like_values_are_recorded(self):
        from tools.reference_episode_failure_diagnostics import ci_context

        with patch.dict(
            "os.environ",
            {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "bad value with spaces",
                "GITHUB_SHA": "a" * 65,
            },
        ):
            value = ci_context()
        self.assertEqual(value["event"], "pull_request")
        self.assertEqual(value["run_id"], "123")
        self.assertIsNone(value["run_attempt"])
        self.assertIsNone(value["sha"])
