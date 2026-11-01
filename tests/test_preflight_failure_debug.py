from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import base64
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from PIL import Image
except ImportError:
    Image = None

import test_comparison_preflight as fixtures
from test_game_2048_adapter import read_fixture, render_fixture
from tools.planning_failure_header import OwnedGameFailureHeader
from tools.compare_2048_windows_planning import preflight_failure_bbox
from tools import compare_2048_windows_planning as comparison
import test_windows_planning_comparison as comparison_fixtures
from ai_game_player.fail_safe_runtime import FailSafeState


class PreflightFailureDebugTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ComparisonPreflightTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.source = self.case.source
        self.source._retain_failure_debug = True
        data = read_fixture("standard.json")
        data["board"] = [[0] * 4 for _ in range(4)]
        self.case.frame = render_fixture(data)

    def fail_at_deadline(self, *, during_capture=False):
        now = [100.0]
        self.case.deadline = 101.0
        if during_capture:
            self.case.capture_hook = lambda: now.__setitem__(0, 102.0)

        with (
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
            patch(
                "tools.run_2048_campaign.time.sleep",
                side_effect=lambda _seconds: now.__setitem__(0, 102.0),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "ready deadline expired"):
                self.case.preflight()
        self.assertIsNone(self.source._cached_read)
        self.assertIsNone(self.source.latest_frame)
        self.assertIsNone(self.source.latest_observation)
        self.assertEqual(self.source._last_accepted_frame_ns, 0)
        self.case.pipeline.record_observation.assert_not_called()
        self.case.pipeline.initialize_safety.assert_not_called()
        self.case.pipeline.complete_execution_observation.assert_not_called()
        self.assertIs(self.case.runtime.state, FailSafeState.SAFE_IDLE)
        self.case.assert_no_input()
        return self.source.failure_debug_snapshot()

    def test_deadline_after_unready_analysis_keeps_same_physical_frame_not_cache(self):
        frame, capture, state = self.fail_at_deadline()
        self.assertEqual(frame.bgra, self.case.frame.bgra)
        self.assertEqual(frame.captured_at, 100.0)
        self.assertEqual(capture["produced_at_ns"], 100_000_000_000)
        self.assertEqual(state["board"], [[0] * 4 for _ in range(4)])
        self.assertEqual(state["unknown_cells"], 0)
        self.assertEqual(state["confidence"], 1)
        self.assertFalse(state["state_available"])
        self.assertEqual(state["unavailable_reason"], "empty_nonterminal")
        self.assertTrue(state["estimated_observation_not_ground_truth"])
        self.assertEqual(self.case.captures, 1)

    def test_cancelled_capture_return_is_kept_without_running_analysis(self):
        with patch.object(
            self.source.analyzer, "analyze", wraps=self.source.analyzer.analyze
        ) as analyze:
            frame, capture, state = self.fail_at_deadline(during_capture=True)
        self.assertEqual(frame.captured_at, 102.0)
        self.assertEqual(capture["produced_at_ns"], 102_000_000_000)
        self.assertEqual(state, {"analysis_status": "unavailable"})
        analyze.assert_not_called()
        self.assertEqual(self.source.capture_sequence, 0)

    def test_explicit_debug_off_retains_no_raw_frame_or_projection(self):
        self.source._retain_failure_debug = False
        frame, capture, state = self.fail_at_deadline()
        self.assertIsNone(frame)
        self.assertEqual(capture, {})
        self.assertEqual(state, {})

    def test_new_request_clears_previous_evidence_before_capture_or_cancel(self):
        self.fail_at_deadline()
        self.assertIsNotNone(self.source.failure_debug_snapshot()[0])
        original_error = RuntimeError("current request cancelled")
        with self.assertRaises(RuntimeError) as raised:
            self.source.capture_playable_state(
                check_cancelled=Mock(side_effect=original_error)
            )
        self.assertIs(raised.exception, original_error)
        self.assertEqual(self.source.failure_debug_snapshot(), (None, {}, {}))

    def test_close_releases_diagnostic_frame_and_state(self):
        self.fail_at_deadline()
        self.source.close()
        self.assertEqual(self.source.failure_debug_snapshot(), (None, {}, {}))

    def test_projection_does_not_copy_arbitrary_text_or_invalid_numeric_values(self):
        frame = replace(self.case.frame, captured_at=1)
        capture = {
            "backend": "gdi",
            "request_started_ns": 1,
            "produced_at_ns": 1_000_000_000,
            "host_received_ns": 1_000_000_001,
        }
        self.source._remember_failure_debug(
            frame,
            capture,
            {
                "board": [["secret", 0, None, -1]] * 4,
                "confidence": float("nan"),
                "state_available": "secret",
                "unavailable_reason": "arbitrary private text",
                "actor": "private",
                "model": "private",
                "board_bbox": "private",
            },
        )
        _frame, _capture, state = self.source.failure_debug_snapshot()
        self.assertEqual(state["board"], [[None, 0, None, None]] * 4)
        self.assertIsNone(state["confidence"])
        self.assertIsNone(state["unavailable_reason"])
        self.assertNotIn("private", json.dumps(state))
        self.assertNotIn("actor", state)
        self.assertNotIn("model", state)

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_owned_same_frame_emission_covers_board_and_preserves_timestamp(self):
        frame, capture, state = self.fail_at_deadline()
        rows = []
        emitter = OwnedGameFailureHeader(
            enabled=True,
            provider="baseline",
            seed=479,
            write_line=rows.append,
            probe=self.case.probe.inspect,
        )
        result = emitter.emit(
            self.case.episode,
            failed=True,
            frame=frame,
            crop_bbox=preflight_failure_bbox(frame),
            capture_diagnostics=capture,
            diagnostic_scope="preflight_board",
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["frame_sha256"], sha256(frame.bgra).hexdigest())
        self.assertEqual(result["capture"]["produced_at_ns"], capture["produced_at_ns"])
        chunks = [json.loads(row) for row in rows[1:]]
        png = base64.b64decode("".join(row["base64"] for row in chunks))
        self.assertEqual(sha256(png).hexdigest(), result["png_sha256"])
        self.assertLessEqual(len(png), 128 * 1024)
        with Image.open(BytesIO(png)) as image:
            self.assertEqual(image.size, (frame.width, frame.height))
        self.assertEqual(
            emitter.emit(
                self.case.episode,
                failed=True,
                frame=frame,
                crop_bbox=preflight_failure_bbox(frame),
                capture_diagnostics=capture,
                diagnostic_scope="preflight_board",
            )["status"],
            "already_attempted",
        )
        self.assertEqual(self.case.captures, 1)

    def test_pid_reuse_disabled_debug_and_active_episode_cannot_export_board(self):
        frame, capture, _state = self.fail_at_deadline()
        for condition in ("foreign_pid", "disabled", "active"):
            with self.subTest(condition=condition):
                self.case.episode._safety_rearmed = condition == "active"
                emitter = OwnedGameFailureHeader(
                    enabled=condition != "disabled",
                    provider="baseline",
                    seed=479,
                    write_line=lambda _: None,
                    probe=lambda _handle: (
                        replace(self.case.target, pid=999)
                        if condition == "foreign_pid"
                        else self.case.target
                    ),
                )
                result = emitter.emit(
                    self.case.episode,
                    failed=True,
                    frame=frame,
                    crop_bbox=preflight_failure_bbox(frame),
                    capture_diagnostics=capture,
                    diagnostic_scope="preflight_board",
                )
                self.assertEqual(
                    result["status"],
                    "disabled" if condition == "disabled" else "unavailable",
                )
        self.case.episode._safety_rearmed = False
        self.case.assert_no_input()

    def test_runner_uses_retained_frame_without_recapture_or_replacing_deadline(self):
        now = [100.0]
        self.case.episode.launcher.server = SimpleNamespace(
            test_seed_metadata={"seed": 479}
        )
        captured_output = io.StringIO()
        with (
            patch.object(
                comparison, "ReferenceGame2048Episode", return_value=self.case.episode
            ),
            patch.object(
                comparison, "PlanningResourceMonitor", comparison_fixtures.FakeMonitor
            ),
            patch.object(
                comparison.TesseractOcrRecognizer, "optional", return_value=None
            ),
            patch(
                "ai_game_player.safety_guard.WindowsTargetProbe.inspect",
                side_effect=self.case.probe.inspect,
            ),
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
            patch(
                "tools.run_2048_campaign.time.sleep",
                side_effect=lambda _: now.__setitem__(0, 146.0),
            ),
            redirect_stdout(captured_output),
        ):
            result = comparison.run_episode(
                "baseline",
                479,
                12,
                Path(self.case.directory.name),
                Path(self.case.directory.name),
                save_failure_evidence=True,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_stage"], "initial_observation")
        self.assertIn("ready deadline expired", result["failure"]["message"])
        self.assertEqual(
            result["failure_observation"]["board"], [[0] * 4 for _ in range(4)]
        )
        self.assertFalse(result["failure_observation"]["state_available"])
        self.assertEqual(result["actions"], 0)
        self.assertEqual(self.case.captures, 1)
        self.case.pipeline.record_observation.assert_not_called()
        self.case.pipeline.complete_execution_observation.assert_not_called()
        self.assertEqual(
            result["failure_header"]["status"],
            "unavailable" if Image is None else "ready",
        )
        summary = comparison.comparison_summary(result)
        self.assertEqual(summary["failure_observation"], result["failure_observation"])
        if Image is not None:
            self.assertEqual(
                result["failure_header"]["frame_sha256"],
                sha256(self.case.frame.bgra).hexdigest(),
            )
            self.assertEqual(
                result["failure_header"]["capture"]["produced_at_ns"], 100_000_000_000
            )

    def test_preview_bounds_and_bad_frame_are_diagnostic_only(self):
        from ai_game_player.screen_capture import ScreenFrame

        frame = ScreenFrame(904, 648, bytes(904 * 648 * 4))
        self.assertEqual(preflight_failure_bbox(frame), [132, 0, 640, 640])
        self.assertIsNone(preflight_failure_bbox(object()))
        self.assertIsNone(preflight_failure_bbox(replace(frame, width=-1)))

    def test_png_failure_cannot_replace_original_failure_or_publish_cache(self):
        frame, capture, _state = self.fail_at_deadline()
        emitter = OwnedGameFailureHeader(
            enabled=True,
            provider="baseline",
            seed=479,
            write_line=lambda _: None,
            probe=self.case.probe.inspect,
        )
        with patch.object(
            emitter, "_encode_png", side_effect=OSError("encoding failed")
        ):
            result = emitter.emit(
                self.case.episode,
                failed=True,
                frame=frame,
                crop_bbox=preflight_failure_bbox(frame),
                capture_diagnostics=capture,
                diagnostic_scope="preflight_board",
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(self.source.latest_frame)
        self.assertIsNone(self.source._cached_read)
        self.assertEqual(self.case.captures, 1)
        self.case.assert_no_input()
