import unittest
from pathlib import Path

from tools.run_windows_platform_gate import (
    GATE_TEST_MODULES,
    REQUIRED_WINDOWS_TESTS,
    gate_test_modules,
)

ROOT = Path(__file__).resolve().parents[1]


class WindowsPlatformGateContractTest(unittest.TestCase):
    def test_every_gate_module_exists_and_is_listed_once(self):
        modules = gate_test_modules()

        self.assertEqual(len(modules), len(set(modules)))
        for module in modules:
            self.assertTrue((ROOT / "tests" / f"{module}.py").is_file(), module)

    def test_fail_safe_state_transition_modules_are_included_by_glob(self):
        modules = gate_test_modules()

        self.assertIn("test_fail_safe_runtime", modules)
        self.assertTrue(set(GATE_TEST_MODULES) <= set(modules))

    def test_required_windows_tests_belong_to_gate_modules(self):
        modules = set(gate_test_modules())

        for test_id in REQUIRED_WINDOWS_TESTS:
            self.assertIn(test_id.split(".", 1)[0], modules, test_id)

    def test_live_input_smokes_stay_outside_the_in_process_sentinel(self):
        # 子プロセスやグローバル入力を使うlive smokeはセンチネルが及ばないため別jobで扱う。
        modules = set(gate_test_modules())

        for module in ("test_reference_game_2048_launcher", "test_2048_campaign"):
            self.assertNotIn(module, modules)

    def test_safety_ci_runs_the_gate_on_windows_and_uploads_the_report(self):
        workflow = (ROOT / ".github" / "workflows" / "safety-ci.yml").read_text(
            encoding="utf-8"
        )
        job = workflow.split("  windows-platform-gate:", 1)[1]

        self.assertIn("name: Windows platform gate (no live input)", job)
        self.assertIn("runs-on: windows-latest", job)
        self.assertIn("python tools/run_windows_platform_gate.py", job)
        self.assertIn("path: build/windows-platform-gate", job)
        self.assertNotIn("KADOKA_RUN_2048_INTEGRATION", job)


if __name__ == "__main__":
    unittest.main()
