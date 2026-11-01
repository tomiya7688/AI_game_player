"""目標配置を現在のABI実装・release許可と取り違えない検査。"""

import copy
import json
from pathlib import Path
import unittest
import tempfile

from tools.runtime_boundary_check import check_boundary
from tools.runtime_target_check import check_target_architecture


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTargetBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "config/runtime_boundary.json").read_text())

    def test_target_is_planned_and_does_not_replace_current_abi(self):
        self.assertEqual(check_target_architecture(self.manifest), [])
        self.assertEqual(self.manifest["contract_version"], 1)
        self.assertEqual(
            set(self.manifest["capabilities"]),
            {"capture", "input", "safety", "fast_cv"},
        )
        self.assertEqual(
            next(c for c in self.manifest["components"] if c["name"] == "ui_tooling")[
                "current_languages"
            ],
            ["python"],
        )
        self.assertNotIn(
            "rust",
            {
                language
                for c in self.manifest["components"]
                for language in c["current_languages"]
            },
        )

    def test_legacy_v1_manifest_without_target_remains_valid(self):
        del self.manifest["target_architecture"]
        self.assertEqual(check_target_architecture(self.manifest), [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(self.manifest))
            self.assertEqual(check_boundary(ROOT, path), [])

    def test_existing_boundary_checker_rejects_embedded_webview_target(self):
        self.manifest["target_architecture"]["gui"]["embedded_webview"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(self.manifest))
            self.assertIn("embedded_webview", "\n".join(check_boundary(ROOT, path)))

    def test_implementation_or_release_success_cannot_be_claimed_by_target(self):
        for section in (None, "distribution"):
            for status in ("implemented", "validated", True):
                value = copy.deepcopy(self.manifest)
                target = value["target_architecture"]
                (target if section is None else target[section])["status"] = status
                self.assertTrue(check_target_architecture(value))

    def test_webview_python_dependency_and_ui_owned_stop_are_rejected(self):
        mutations = [
            ("gui", "embedded_webview", True),
            ("gui", "framework", "qt_webengine"),
            ("resident_core", "language", "python"),
            ("python_worker", "required_for_core", True),
            ("safety", "authority", "gui"),
            ("safety", "stop_bypasses_work_queue", False),
            ("compatibility", "existing_windows_e2e", False),
        ]
        for section, key, invalid in mutations:
            with self.subTest(section=section, key=key):
                value = copy.deepcopy(self.manifest)
                value["target_architecture"][section][key] = invalid
                self.assertTrue(check_target_architecture(value))

    def test_license_gates_cannot_be_dropped_or_replaced_with_shell_smoke(self):
        for gates in (
            [149, 228],
            [149, 228, 664],
            [149, 228, 228],
            [149, 228, True],
            [{}, 228, 663],
            None,
        ):
            value = copy.deepcopy(self.manifest)
            value["target_architecture"]["distribution"]["gate_issues"] = gates
            self.assertTrue(check_target_architecture(value))

    def test_malformed_target_does_not_crash_existing_boundary_ci(self):
        for invalid in (None, [], "planned", {"schema": []}):
            value = copy.deepcopy(self.manifest)
            value["target_architecture"] = invalid
            self.assertTrue(check_target_architecture(value))
        for section in ("gui", "resident_core", "bridge", "safety", "distribution"):
            value = copy.deepcopy(self.manifest)
            value["target_architecture"][section] = []
            self.assertTrue(check_target_architecture(value))
