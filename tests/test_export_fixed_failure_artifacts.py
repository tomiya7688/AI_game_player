"""Focused offline tests of the /tmp diagnostic candidate; no repository edits."""

from contextlib import redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from PIL import Image
except ImportError:
    Image = None

from tools import export_fixed_failure_artifacts as m


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "known-8dc" / m.FIXED_IMAGE
        self.image.mkdir(parents=True)
        # Numeric/lease fixtures remain usable without the optional image extra.
        bgra = b""
        out = BytesIO()
        if Image is not None:
            rgb = Image.new("RGB", (904, 648), (201, 151, 101))
            bgra = rgb.convert("RGBA").tobytes("raw", "BGRA")
            rgb.save(out, "PNG")
        self.manifest = {
            "schema": "2048-recognition-evidence/v1",
            "diagnostics": {"failure_detail": "SECRET_URL_TOKEN"},
            "frames": {
                "last_capture": {
                    "width": 904,
                    "height": 648,
                    "png": "last-captured.png",
                    "bgra": "last-captured.bgra",
                    "bgra_sha256": sha256(bgra).hexdigest(),
                    "captured_at": 123.5,
                    "frame_id": 7,
                    "game_state": {
                        "board_bbox": [260, 232, 375, 375],
                        "label": "SECRET_TEXT",
                    },
                }
            },
        }
        self.write_manifest()
        (self.image / "last-captured.png").write_bytes(out.getvalue())
        (self.image / "last-captured.bgra").write_bytes(bgra)
        self.lease = self.root / "known-d954" / m.FIXED_LEASE
        self.episode = self.lease / "runtime/episode-0001"
        (self.episode / "fail_safe").mkdir(parents=True)
        self.write(
            self.lease / "report.json",
            {
                "schema": "campaign-report/v1",
                "success": False,
                "actions_executed": 77,
                "failure_detail": 'SECRET_URL_TOKEN lease_expired observation_timing={"elapsed_ms":3889.643,"stage_ms":{"capture":24.504,"private":123}}',
                "metadata": {"browser_profile": "SECRET_PROFILE"},
                "trace_path": "SECRET_PATH",
            },
        )
        self.write(
            self.episode / "fail_safe/lease.json",
            {
                "schema": "kadoka-control-lease/v1",
                "state": "recovery_required",
                "reason": "lease_expired",
                "lease_expires_at": 0.0,
                "observation_at": 171.5,
                "updated_at": 173.5,
                "epoch": "SECRET_EPOCH",
            },
        )
        self.write(
            self.episode / "fail_safe/journal.json",
            {
                "schema": "kadoka-failsafe-journal/v1",
                "reason": "lease_expired",
                "timestamp": 173.5,
                "details": {"owner_pid": 33, "secret": "SECRET_PID"},
            },
        )
        self.write(
            self.episode / "fail_safe/input_ledger.json",
            {
                "schema": "kadoka-input-ledger/v1",
                "held_keys": {"SECRET_KEY": 174.0},
                "held_mouse": {},
                "updated_at": 173.5,
            },
        )
        self.trace = self.lease / "trace.jsonl"
        self.trace.write_text(
            '{"event":"step","step":77,"action_id":"SECRET_ACTION","latency_ms":10}\n'
        )
        (self.episode / "safety_guard.jsonl").write_text(
            '{"timestamp":123,"state":"active","reason":"SECRET_REASON"}\n'
        )

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def write_manifest(self):
        self.write(self.image / "manifest.json", self.manifest)

    def assert_reject(self, kind=""):
        output, failed = m.export_sections(self.root)
        self.assertTrue(failed)
        errors = [
            json.loads(line)
            for line in output.splitlines()
            if json.loads(line)["kind"] == "fixed_artifact_section_error"
        ]
        self.assertTrue(errors)
        if kind:
            self.assertIn(kind, [error["code"] for error in errors])

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_valid_one_pixel_verified_bounded_image_and_typed_metadata(self):
        output = m.export(self.root)
        self.assertNotIn("SECRET", output)
        rows = [json.loads(line) for line in output.splitlines()]
        images = [r for r in rows if r["kind"] == "reference_game_crop"]
        self.assertEqual(1, len(images))
        self.assertEqual((375, 487), (images[0]["width"], images[0]["height"]))
        self.assertEqual(120, images[0]["crop_xyxy"][1])
        self.assertLess(len(output), m.MAX_OUTPUT)
        self.assertTrue(all(len(line) <= 4096 for line in output.splitlines()))
        report = next(r for r in rows if r["kind"] == "report")
        self.assertEqual(["lease_expired"], report["known_codes"])
        self.assertEqual({"capture": 24.504}, report["observation_timing"]["stage_ms"])
        self.assertEqual(77, report["actions_executed"])
        self.assertTrue(
            all(
                not r["zip_digest_recomputed"]
                for r in rows
                if r["kind"] == "pinned_artifact"
            )
        )

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_pixel_tampering_rejected(self):
        p = self.image / "last-captured.bgra"
        p.write_bytes(b"\0" + p.read_bytes()[1:])
        self.assert_reject("raw_sha_mismatch")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_png_vs_bgra_mismatch_rejected(self):
        Image.new("RGB", (904, 648), (1, 2, 3)).save(self.image / "last-captured.png")
        self.assert_reject("png_raw_mismatch")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_png_metadata_rejected(self):
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        info.add_text("private", "SECRET_URL_TOKEN")
        Image.new("RGB", (904, 648), (201, 151, 101)).save(
            self.image / "last-captured.png", pnginfo=info
        )
        self.assert_reject("png_chunk_policy")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_manifest_path_traversal_rejected(self):
        self.manifest["frames"]["last_capture"]["png"] = "../../secrets.png"
        self.write_manifest()
        self.assert_reject("frame_names")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_browser_ui_crop_rejected(self):
        self.manifest["frames"]["last_capture"]["game_state"]["board_bbox"] = [
            10,
            20,
            375,
            375,
        ]
        self.write_manifest()
        self.assert_reject("board_crop_policy")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_missing_bbox_never_exports_whole_frame(self):
        self.manifest["frames"]["last_capture"]["game_state"]["board_bbox"] = None
        self.write_manifest()
        self.assert_reject("board_crop_missing")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_raw_limit_before_read(self):
        with (self.image / "last-captured.bgra").open("wb") as stream:
            stream.truncate(m.MAX_RAW + 1)
        self.assert_reject("file_bound")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_png_limit_before_decode(self):
        with (self.image / "last-captured.png").open("wb") as stream:
            stream.truncate(m.MAX_PNG + 1)
        self.assert_reject("file_bound")

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_symlink_component(self):
        raw = self.image / "last-captured.bgra"
        original = self.image / "original.bgra"
        raw.rename(original)
        raw.symlink_to(original)
        self.assert_reject("linked_path")

    def test_duplicate_and_nonfinite_json(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(m.Rejected):
                m.parse_json(raw)

    def test_trace_line_and_line_count_caps(self):
        for data, reason in (
            (b" " * (m.MAX_LINE + 1), "trace_line_bound"),
            (b"{}\n" * 5001, "trace_line_count"),
        ):
            with self.subTest(reason=reason):
                self.trace.write_bytes(data)
                self.assert_reject(reason)

    def test_only_last_eight_numeric_events(self):
        self.trace.write_text(
            "".join(
                json.dumps({"event": "step", "step": n, "private": "SECRET"}) + "\n"
                for n in range(20)
            )
        )
        rows = [json.loads(line) for line in m.export(self.root).splitlines()]
        self.assertEqual(
            list(range(12, 20)),
            [r["step"] for r in rows if r["kind"] == "campaign_trace"],
        )

    def test_huge_integer_not_converted_to_float(self):
        self.assertEqual({}, m.project({"timestamp": 10**1000}))

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_cli_reject_does_not_leak_path_or_error(self):
        (self.image / "manifest.json").unlink()
        out = StringIO()
        with (
            patch.object(sys, "argv", ["export", "--download-root", str(self.root)]),
            redirect_stdout(out),
        ):
            self.assertEqual(1, m.main())
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertNotIn(str(self.root), out.getvalue())
        self.assertNotIn("SECRET", out.getvalue())
        self.assertIn(
            {
                "kind": "fixed_artifact_section_error",
                "section": "image",
                "code": "missing_fixed_file",
            },
            rows,
        )
        self.assertTrue(any(row["kind"] == "report" for row in rows))
        self.assertIn(
            {"kind": "fixed_artifact_section_complete", "section": "lease"}, rows
        )

    @unittest.skipIf(Image is None, "optional Pillow is required for pixel evidence")
    def test_valid_image_survives_missing_lease_journal(self):
        (self.episode / "fail_safe/journal.json").unlink()
        output, failed = m.export_sections(self.root)
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(failed)
        self.assertEqual(1, sum(row["kind"] == "reference_game_crop" for row in rows))
        self.assertIn(
            {
                "kind": "fixed_artifact_section_error",
                "section": "lease",
                "code": "missing_fixed_file",
            },
            rows,
        )
        self.assertIn(
            {"kind": "fixed_artifact_section_complete", "section": "image"}, rows
        )

    def test_both_sections_missing_are_errors_not_success(self):
        (self.image / "manifest.json").unlink()
        (self.episode / "fail_safe/journal.json").unlink()
        output, failed = m.export_sections(self.root)
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(failed)
        self.assertEqual(
            2, sum(row["kind"] == "fixed_artifact_section_error" for row in rows)
        )
        self.assertFalse(
            any(row["kind"] == "fixed_artifact_section_complete" for row in rows)
        )

    def test_safety_allowed_emergency_and_cleanup_are_typed(self):
        self.assertEqual(
            {"allowed": True, "state": "emergency_stop", "code": "allowed"},
            m.project(
                {
                    "allowed": True,
                    "state": "emergency_stop",
                    "code": "allowed",
                    "reason": "SECRET",
                }
            ),
        )
        self.assertEqual(
            {"state": "safe_idle", "reason": "runtime_closed", "lease_expires_at": 0},
            m.project(
                {
                    "state": "safe_idle",
                    "reason": "runtime closed",
                    "lease_expires_at": 0,
                }
            ),
        )
        rows = [json.loads(line) for line in m.export(self.root).splitlines()]
        self.assertTrue(
            all(
                row["clock"] == "legacy_failsafe_wall_seconds"
                for row in rows
                if row["kind"] == "safety_trace"
            )
        )

    def test_invalid_optional_timing_retains_known_failure_and_counters(self):
        projected = m.project(
            {
                "steps": 77,
                "failure_detail": "lease_expired observation_timing={invalid}",
            }
        )
        self.assertEqual(77, projected["steps"])
        self.assertEqual(["lease_expired"], projected["known_codes"])
        self.assertEqual(
            "invalid_optional_json", projected["observation_timing_status"]
        )

    def test_schema_mismatch(self):
        self.write(self.lease / "report.json", {"schema": "wrong"})
        self.assert_reject("fixed_schema")


if __name__ == "__main__":
    unittest.main()
