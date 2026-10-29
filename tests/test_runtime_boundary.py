"""配置manifestが不正な契約や重複配置を許可しないことを確認します。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.runtime_boundary_check import _native_contract, check_boundary

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config/runtime_boundary.json"


class RuntimeBoundaryCheckTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def assert_manifest_rejected(self, value, expected_message):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_boundary.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            errors = check_boundary(ROOT, path)
        self.assertTrue(errors)
        self.assertIn(expected_message, "\n".join(errors))

    def test_repository_runtime_boundary_is_consistent(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools/runtime_boundary_check.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("RUNTIME BOUNDARY OK", completed.stdout)

    def test_contract_version_drift_and_non_integer_are_rejected(self):
        for version in (999, True, "1", [1]):
            with self.subTest(version=version):
                value = copy.deepcopy(self.manifest)
                value["contract_version"] = version
                self.assert_manifest_rejected(value, "contract version mismatch")

    def test_duplicate_capability_names_are_rejected(self):
        self.manifest["capabilities"].append("capture")
        self.assert_manifest_rejected(self.manifest, "duplicate capabilities")

    def test_one_capability_cannot_have_two_differently_named_placements(self):
        duplicate = copy.deepcopy(self.manifest["components"][0])
        duplicate["name"] = "second_capture"
        self.manifest["components"].append(duplicate)
        self.assert_manifest_rejected(
            self.manifest, "duplicate capability placement: capture"
        )

    def test_missing_or_unknown_capability_is_rejected(self):
        for capabilities in (
            ["capture"],
            ["capture", "input", "safety", "fast_cv", "unknown"],
        ):
            with self.subTest(capabilities=capabilities):
                self.manifest["capabilities"] = capabilities
                self.assert_manifest_rejected(self.manifest, "capability mismatch")

    def test_fine_grained_and_unknown_units_are_rejected(self):
        for unit in ("pixel", "token", "candidate", "arbitrary"):
            with self.subTest(unit=unit):
                value = copy.deepcopy(self.manifest)
                value["coarse_boundary_units"].append(unit)
                self.assert_manifest_rejected(value, "unapproved or fine-grained")

    def test_malformed_nested_values_return_errors_instead_of_crashing(self):
        cases = [
            ("languages", [], "languages must be an object"),
            ("components", {}, "components must be"),
            ("capabilities", [None], "invalid string"),
            ("distribution", {}, "self_contained"),
        ]
        for key, invalid_value, message in cases:
            with self.subTest(key=key):
                value = copy.deepcopy(self.manifest)
                value[key] = invalid_value
                self.assert_manifest_rejected(value, message)
        self.manifest["languages"]["cpp"]["decision"] = []
        self.assert_manifest_rejected(self.manifest, "adoption decision")
        self.manifest["components"][0]["boundary_unit"] = {}
        self.assert_manifest_rejected(self.manifest, "unapproved boundary unit")

    def test_distribution_entrypoint_cannot_escape_repository(self):
        self.manifest["distribution"]["windows_bundle_entrypoint"] = "../outside.ps1"
        self.assert_manifest_rejected(self.manifest, "inside the repository")

    def test_invalid_json_encoding_and_missing_manifest_return_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for data in (b"{", b"\xff", b"null"):
                with self.subTest(data=data):
                    path.write_bytes(data)
                    self.assertTrue(check_boundary(ROOT, path))
            path.unlink()
            self.assertTrue(check_boundary(ROOT, path))

    def test_invalid_component_name_keeps_index_in_related_errors(self):
        self.manifest["components"][0]["name"] = None
        self.manifest["components"][0]["preferred_language"] = "unknown"
        self.assert_manifest_rejected(
            self.manifest, "components[0] uses an unevaluated language"
        )

    def test_native_contract_accepts_c_integer_literal_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_api.h"
            for literal in ("1", "1u", "1U"):
                with self.subTest(literal=literal):
                    path.write_text(
                        f"#define KADOKA_RUNTIME_ABI_VERSION {literal}\n#define KADOKA_CAP_CAPTURE (1 << 0)\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(_native_contract(path), (1, {"capture"}))

    def test_read_errors_identify_the_failed_input_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text("{", encoding="utf-8")
            self.assertIn(str(manifest), "\n".join(check_boundary(root, manifest)))
            manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
            for relative in (
                "src/ai_game_player/runtime/contracts.py",
                "native/include/kadoka/runtime_api.h",
            ):
                path = root / relative
                self.assertIn(str(path), "\n".join(check_boundary(root, manifest)))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / relative).read_bytes())
