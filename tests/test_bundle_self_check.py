"""配布bundle自己検査の各項目が、bundle外の部品や読込失敗を不合格にすることを確認します（Issue #149）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_game_player import __main__ as entry
from ai_game_player import bundle_self_check as self_check
from ai_game_player.runtime.native import NativeLoadResult, NativeLoadStatus

ROOT = Path(__file__).resolve().parents[1]


class BundleSelfCheckTest(unittest.TestCase):
    def test_development_interpreter_is_not_a_frozen_bundle(self):
        with patch.object(sys, "frozen", False, create=True):
            result = self_check._check_frozen()
        self.assertFalse(result.ok)

    def test_frozen_requires_meipass(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(ROOT), create=True),
        ):
            self.assertTrue(self_check._check_frozen().ok)

    def test_import_paths_outside_bundle_are_listed(self):
        with patch.object(sys, "path", [str(ROOT / "src"), ""]):
            self.assertTrue(self_check._check_import_paths(ROOT).ok)
        outside = str(Path(tempfile.gettempdir()) / "developer-site-packages")
        with patch.object(sys, "path", [str(ROOT / "src"), outside]):
            result = self_check._check_import_paths(ROOT)
        self.assertFalse(result.ok)
        self.assertIn("developer-site-packages", result.detail)

    def test_empty_sys_path_entry_is_the_working_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(sys, "path", [""]),
            patch.object(Path, "cwd", return_value=Path(directory)),
        ):
            result = self_check._check_import_paths(ROOT)
        self.assertFalse(result.ok)
        self.assertIn("''=", result.detail)

    def test_extension_modules_must_come_from_the_bundle(self):
        module = MagicMock(
            __file__=str(ROOT / "src" / "_tkinter.pyd"), __name__="_tkinter"
        )
        self.assertIsNone(self_check._extension_outside(module, ROOT))
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn(
                "_tkinter loaded from",
                self_check._extension_outside(module, Path(directory).resolve()),
            )
        builtin = MagicMock(spec=["__name__"], __name__="_ssl")
        self.assertIn("None", self_check._extension_outside(builtin, ROOT))

    def test_package_outside_bundle_root_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, "path", []):
            result = self_check._check_import_paths(Path(directory).resolve())
        self.assertFalse(result.ok)
        self.assertIn("ai_game_player=", result.detail)

    def test_native_runtime_must_be_available_next_to_the_executable(self):
        runtime = MagicMock()
        bundle = ROOT / "bundle"
        cases = [
            (
                NativeLoadResult(NativeLoadStatus.UNAVAILABLE, reason="not found"),
                False,
                "unavailable",
            ),
            (
                NativeLoadResult(
                    NativeLoadStatus.AVAILABLE, runtime, ROOT / "elsewhere" / "k.dll"
                ),
                False,
                "outside bundle root",
            ),
            (
                NativeLoadResult(
                    NativeLoadStatus.AVAILABLE,
                    runtime,
                    bundle / "kadoka_native_runtime.dll",
                ),
                True,
                "kadoka_native_runtime.dll",
            ),
        ]
        for loaded, ok, text in cases:
            with (
                self.subTest(text=text),
                patch(
                    "ai_game_player.runtime.native.discover_native_runtime",
                    return_value=loaded,
                ),
            ):
                result = self_check._check_native_runtime(bundle)
            self.assertEqual(result.ok, ok)
            self.assertIn(text, result.detail)
        self.assertEqual(runtime.close.call_count, 2)

    def test_native_safety_must_load_the_bundle_dll(self):
        bundle = ROOT / "bundle"
        with patch(
            "ai_game_player.safety_guard.NativeSafetyValidator.try_load",
            return_value=None,
        ):
            self.assertFalse(self_check._check_native_safety(bundle).ok)
        validator = MagicMock()
        validator.library._name = "kadoka_native_runtime.dll"
        with patch(
            "ai_game_player.safety_guard.NativeSafetyValidator.try_load",
            return_value=validator,
        ):
            self.assertFalse(self_check._check_native_safety(bundle).ok)
        validator.library._name = str(
            bundle / "_internal" / "kadoka_native_runtime.dll"
        )
        with patch(
            "ai_game_player.safety_guard.NativeSafetyValidator.try_load",
            return_value=validator,
        ):
            self.assertFalse(self_check._check_native_safety(bundle).ok)
        validator.library._name = str(ROOT / "dev" / "kadoka_native_runtime.dll")
        with patch(
            "ai_game_player.safety_guard.NativeSafetyValidator.try_load",
            return_value=validator,
        ):
            self.assertFalse(self_check._check_native_safety(bundle).ok)
        validator.library._name = str(bundle / "kadoka_native_runtime.dll")
        with patch(
            "ai_game_player.safety_guard.NativeSafetyValidator.try_load",
            return_value=validator,
        ):
            self.assertTrue(self_check._check_native_safety(bundle).ok)

    def test_assets_inside_the_source_tree_pass_when_the_tree_is_the_root(self):
        self.assertTrue(self_check._check_assets(ROOT).ok)
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(self_check._check_assets(Path(directory).resolve()).ok)

    def test_exception_becomes_a_failed_item(self):
        def broken():
            raise OSError("tcl90.dll missing")

        result = self_check._run_guarded("tk", broken)
        self.assertEqual((result.name, result.ok), ("tk", False))
        self.assertIn("OSError: tcl90.dll missing", result.detail)

    def test_report_is_written_and_any_failure_returns_one(self):
        results = [
            self_check.BundleCheckResult("frozen", True, "ok"),
            self_check.BundleCheckResult("native_runtime", False, "load_failed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch.object(
                self_check, "collect_bundle_checks", return_value=results
            ):
                code = self_check.run_bundle_self_check(report)
            value = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(value["schema"], self_check.SCHEMA)
        self.assertFalse(value["ok"])
        self.assertEqual(
            [item["name"] for item in value["checks"]], ["frozen", "native_runtime"]
        )

    def test_all_items_run_even_when_one_raises(self):
        with patch.object(
            self_check, "_check_frozen", side_effect=RuntimeError("boom")
        ):
            results = self_check.collect_bundle_checks(ROOT)
        self.assertEqual(
            [result.name for result in results],
            [
                "frozen",
                "import_paths",
                "native_runtime",
                "native_safety",
                "assets",
                "tk",
                "ssl",
            ],
        )
        self.assertFalse(results[0].ok)

    def test_entrypoint_requires_a_report_path(self):
        with patch.object(sys, "argv", ["Kadoka.exe", "--bundle-self-check"]):
            with self.assertRaises(SystemExit):
                entry.main()

    def test_entrypoint_dispatches_to_the_self_check(self):
        with (
            patch.object(sys, "argv", ["Kadoka.exe", "--bundle-self-check", "r.json"]),
            patch(
                "ai_game_player.bundle_self_check.run_bundle_self_check", return_value=0
            ) as run,
        ):
            self.assertEqual(entry.main(), 0)
        run.assert_called_once_with(Path("r.json"))


if __name__ == "__main__":
    unittest.main()
