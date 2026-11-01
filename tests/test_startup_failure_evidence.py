import ctypes
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_game_player.screen_capture import ScreenFrame
from tools.reference_game_2048_launcher import BrowserWindowSession, WINDOW_WAIT_SECONDS
from tools import startup_failure_evidence as evidence
from tools import run_2048_campaign


def window(handle=11, *, visible=True, rect=None):
    return {
        "handle": handle,
        "visible": visible,
        "rect": rect or [0, 0, 2, 2],
        "title_class": "other_owned_title",
    }


class StartupFailureEvidenceTests(unittest.TestCase):
    def launch_failure(self, recorder, original):
        events = []
        manifest_path = Path("manifest.json")
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        session.failure_evidence = recorder
        if recorder is not None:
            recorder.save.side_effect = lambda *args: (
                events.append("evidence") or manifest_path
            )
        with (
            patch("tools.reference_game_2048_launcher.os.name", "nt"),
            patch.object(Path, "is_file", return_value=True),
            patch(
                "tools.reference_game_2048_launcher.tempfile.TemporaryDirectory",
                return_value=Mock(name="profile"),
            ),
            patch(
                "tools.reference_game_2048_launcher.subprocess.Popen",
                return_value=Mock(pid=41),
            ),
            patch.object(session, "_wait_for_window", side_effect=original) as wait,
            patch.object(session, "close", side_effect=lambda: events.append("close")),
        ):
            with self.assertRaises(type(original)) as raised:
                session.start("http://127.0.0.1:1/")
        self.assertIs(raised.exception, original)
        wait.assert_called_once_with(WINDOW_WAIT_SECONDS)
        return session, events

    def test_opt_in_saves_before_cleanup_and_preserves_original_timeout(self):
        recorder = Mock()
        original = TimeoutError("original startup failure")
        session, events = self.launch_failure(recorder, original)
        self.assertEqual(events, ["evidence", "close"])
        self.assertEqual(original.args, ("original startup failure",))
        self.assertEqual(recorder.save.call_args.args[0], 41)
        self.assertEqual(
            set(recorder.save.call_args.args[1]),
            {"browser_spawn", "window_wait", "startup_total"},
        )
        self.assertEqual(str(session.failure_evidence_manifest), "manifest.json")

    def test_default_does_not_create_or_capture_diagnostics(self):
        session, events = self.launch_failure(None, TimeoutError("failure"))
        self.assertEqual(events, ["close"])
        self.assertIsNone(session.failure_evidence_manifest)

    def test_evidence_exception_cannot_replace_original_or_skip_cleanup(self):
        events = []
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        session.failure_evidence = Mock()
        session.failure_evidence.save.side_effect = OSError("unavailable evidence disk")
        original = OSError(5, "startup failed", "browser.exe")
        with (
            patch("tools.reference_game_2048_launcher.os.name", "nt"),
            patch.object(Path, "is_file", return_value=True),
            patch(
                "tools.reference_game_2048_launcher.tempfile.TemporaryDirectory",
                return_value=Mock(),
            ),
            patch(
                "tools.reference_game_2048_launcher.subprocess.Popen",
                return_value=Mock(pid=41),
            ),
            patch.object(session, "_wait_for_window", side_effect=original),
            patch.object(session, "close", side_effect=lambda: events.append("close")),
        ):
            with self.assertRaises(OSError) as raised:
                session.start("http://127.0.0.1:1/")
        self.assertIs(raised.exception, original)
        self.assertEqual((original.errno, original.filename), (5, "browser.exe"))
        self.assertEqual(events, ["close"])
        self.assertEqual(session.failure_evidence_error, "OSError")

    def test_campaign_existing_save_flag_enables_startup_evidence_only_when_on(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), TemporaryDirectory() as directory:
                launcher = SimpleNamespace(
                    start=Mock(side_effect=TimeoutError("start")),
                    close=Mock(),
                    failure_evidence=None,
                )
                with patch.object(
                    run_2048_campaign,
                    "ReferenceGame2048Launcher",
                    return_value=launcher,
                ):
                    with self.assertRaises(TimeoutError):
                        run_2048_campaign.ReferenceGame2048Episode(
                            1,
                            Path(directory) / "cache",
                            Path(directory),
                            None,
                            None,
                            save_failure_evidence=enabled,
                        )
                self.assertEqual(
                    isinstance(
                        launcher.failure_evidence, evidence.StartupFailureEvidence
                    ),
                    enabled,
                )
                launcher.close.assert_called_once()

    def test_worker_timeout_preserves_manifest_and_uses_finite_process_budget(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(
                evidence.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("capture", 5),
            ) as run,
        ):
            manifest = evidence.StartupFailureEvidence(Path(directory)).save(
                41, {"window_wait": 25000.0}
            )
            value = json.loads(manifest.read_text())
            self.assertEqual(value["worker_status"], "worker_timeout")
            self.assertEqual(value["windows"], [])
            self.assertEqual(
                run.call_args.kwargs["timeout"], evidence.DIAGNOSTIC_TIMEOUT_SECONDS
            )
            self.assertEqual(value["phase_durations_ms"]["window_wait"], 25000.0)

    def test_metadata_and_pixels_are_bounded_and_use_existing_frame_hashes(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                evidence.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ):
                manifest = evidence.StartupFailureEvidence(Path(directory)).save(41, {})
            frame = ScreenFrame(2, 2, b"\x01\x02\x03\xff" * 4)
            capture = Mock(return_value=frame)
            records = [
                window(11),
                window(12, visible=False),
                window(13, rect=[0, 0, 10000, 10000]),
                window(14),
            ]
            evidence.collect_evidence(
                manifest.parent,
                41,
                enumerate_windows=lambda _: records,
                capture=capture,
            )
            value = json.loads(manifest.read_text())
            self.assertEqual(len(value["windows"]), 3)
            self.assertEqual(
                [row["capture_status"] for row in value["windows"]],
                ["captured", "hidden_window", "size_budget"],
            )
            capture.assert_called_once_with(11, 41, evidence.MAX_FRAME_BYTES)
            saved = value["windows"][0]["frame"]
            self.assertEqual(saved["bgra_sha256"], sha256(frame.bgra).hexdigest())
            self.assertEqual((manifest.parent / saved["bgra"]).read_bytes(), frame.bgra)
            self.assertTrue(
                (manifest.parent / saved["png"]).read_bytes().startswith(b"\x89PNG")
            )

    def test_capture_failure_keeps_metadata_and_error_type_without_sensitive_text(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                evidence.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ):
                manifest = evidence.StartupFailureEvidence(Path(directory)).save(41, {})
            evidence.collect_evidence(
                manifest.parent,
                41,
                enumerate_windows=lambda _: [window()],
                capture=Mock(side_effect=RuntimeError("secret browser title")),
            )
            text = manifest.read_text()
            self.assertNotIn("secret browser title", text)
            self.assertEqual(
                json.loads(text)["windows"][0]["error_type"], "RuntimeError"
            )

    def test_partial_writer_failures_still_consume_total_pixel_budget(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                evidence.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ):
                manifest = evidence.StartupFailureEvidence(Path(directory)).save(41, {})
            capture = Mock(return_value=ScreenFrame(2, 2, b"\x00" * 16))
            with (
                patch.object(evidence, "MAX_FRAME_BYTES", 16),
                patch.object(evidence, "MAX_TOTAL_FRAME_BYTES", 32),
                patch.object(
                    evidence, "_write_frame", side_effect=OSError("partial write")
                ) as writer,
            ):
                evidence.collect_evidence(
                    manifest.parent,
                    41,
                    enumerate_windows=lambda _: [window(1), window(2), window(3)],
                    capture=capture,
                )
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(writer.call_count, 2)
            self.assertEqual(
                [
                    row["capture_status"]
                    for row in json.loads(manifest.read_text())["windows"]
                ],
                ["unavailable", "unavailable", "size_budget"],
            )

    def test_owned_metadata_filters_other_pid_and_never_serializes_titles(self):
        api = Mock()
        api.IsWindow.return_value = True
        api.IsWindowVisible.side_effect = lambda handle: handle == 11
        api.GetWindowThreadProcessId.side_effect = lambda handle, pointer: (
            setattr(pointer._obj, "value", 41) or 1
        )

        def rect(handle, pointer):
            for index, value in enumerate([1, 2, 101, 82]):
                pointer._obj[index] = value
            return 1

        api.GetWindowRect.side_effect = rect
        titles = {
            10: "personal other app",
            11: "private owned browser dialog",
            12: "2048",
            13: "",
        }
        api.GetWindowThreadProcessId.side_effect = lambda handle, pointer: (
            setattr(pointer._obj, "value", 99 if handle == 10 else 41) or 1
        )
        api.GetWindowTextW.side_effect = lambda handle, buffer, capacity: (
            setattr(buffer, "value", titles[handle]) or len(titles[handle])
        )
        api.EnumWindows.side_effect = lambda callback, parameter: all(
            callback(handle, parameter) for handle in titles
        )
        with (
            patch.object(ctypes, "windll", SimpleNamespace(user32=api), create=True),
        ):
            selected = evidence._owned_windows(41)
        self.assertEqual([record["handle"] for record in selected], [11, 12, 13])
        self.assertEqual(
            [record["title_class"] for record in selected],
            ["other_owned_title", "game_title", "untitled"],
        )
        self.assertEqual(
            [call.args[0] for call in api.GetWindowTextW.call_args_list], [11, 12, 13]
        )
        self.assertNotIn("private", json.dumps(selected))
        self.assertNotIn("personal", json.dumps(selected))

    def test_wgc_checks_ownership_before_and_after_and_never_uses_gdi(self):
        runtime = Mock()
        runtime.__enter__ = Mock(return_value=runtime)
        runtime.__exit__ = Mock(return_value=False)
        capture = Mock()
        capture.__enter__ = Mock(return_value=capture)
        capture.__exit__ = Mock(return_value=False)
        runtime.open_wgc_window.return_value = capture
        capture.capture.return_value = ScreenFrame(2, 2, b"\x00" * 16)
        from ai_game_player.runtime.native import NativeLoadStatus

        loaded = SimpleNamespace(status=NativeLoadStatus.AVAILABLE, runtime=runtime)
        with (
            patch.object(evidence, "_is_owned", side_effect=[True, False]),
            patch(
                "ai_game_player.runtime.native.discover_native_runtime",
                return_value=loaded,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "ownership"):
                evidence._capture_owned(11, 41, 16)
        capture.capture.assert_called_once_with(
            timeout_ms=evidence.CAPTURE_TIMEOUT_MS, max_frame_bytes=16
        )
        runtime.__exit__.assert_called_once()
