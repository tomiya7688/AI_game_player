from datetime import date
from pathlib import Path
import re
import unittest

from tools.pr_workflow_evidence import REQUIRED_WORKFLOW_PATHS
from tools.workflow_security_check import scan_workflow_path

WORKFLOW = Path(".github/workflows/windows-long-comparison.yml")
SECTION = re.compile(r"^[a-z_]+:", re.MULTILINE)


class WindowsLongComparisonWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_only_manual_and_weekly_triggers_and_never_push_or_pull_request(self):
        on_block = self.text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", on_block)
        self.assertIn("schedule:", on_block)
        for trigger in ("push", "pull_request", "pull_request_target", "workflow_run"):
            self.assertNotRegex(on_block, rf"^\s*{trigger}\s*:", trigger)

    def test_top_level_permissions_are_read_only(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r"(?im)^\s+[a-z-]+:\s*write\s*$")

    def test_not_a_required_workflow_and_not_added_to_the_merge_gate(self):
        self.assertNotIn(str(WORKFLOW).replace("\\", "/"), REQUIRED_WORKFLOW_PATHS)

    def test_run_jobs_use_the_trust_floor_and_upload_artifacts_even_on_failure(self):
        self.assertIn("uses: ./.github/actions/python-trust-floor", self.text)
        self.assertIn("timeout-minutes: 90", self.text)
        self.assertIn("fail-fast: false", self.text)
        self.assertRegex(
            self.text,
            r"(?s)upload-artifact@v4\n\s+if: always\(\)\n\s+with:\n\s+name: windows-long-block-",
        )
        self.assertIn("needs: [plan, block]", self.text)
        self.assertIn("if: always() && needs.plan.result == 'success'", self.text)

    def test_seed_count_and_other_inputs_are_validated_by_the_planner_not_the_shell(
        self,
    ):
        self.assertIn("tools/aggregate_2048_windows_long.py plan", self.text)
        self.assertIn("SEED_COUNT: ${{ inputs.seed_count || '8' }}", self.text)

    def test_native_runtime_is_built_once_and_shared_by_every_block(self):
        # Issue #850: blockごとのDLL再ビルドでnative_runtime_sha256が揃わなかった。
        self.assertEqual(1, self.text.count("cmake --build"))
        build_job = self.text.split("  build-native:\n", 1)[1].split("\n  block:\n", 1)[
            0
        ]
        block_job = self.text.split("\n  block:\n", 1)[1].split("\n  aggregate:\n", 1)[
            0
        ]
        self.assertIn("cmake --build", build_job)
        self.assertIn("name: windows-long-native-runtime", build_job)
        self.assertIn(
            "path: build/windows-long-native/kadoka_native_runtime.dll", build_job
        )
        self.assertIn(
            "Copy-Item $runtime.FullName build/windows-long-native/kadoka_native_runtime.dll",
            build_job,
        )
        self.assertIn("if-no-files-found: error", build_job)
        self.assertNotIn("cmake", block_job)
        self.assertIn("needs: [plan, build-native]", block_job)
        self.assertIn("download-artifact@v4", block_job)
        self.assertIn("name: windows-long-native-runtime", block_job)
        self.assertIn("KADOKA_NATIVE_RUNTIME=", block_job)

    def test_workflow_passes_the_repository_security_scan(self):
        self.assertEqual([], scan_workflow_path(WORKFLOW, date(2026, 11, 7)))


if __name__ == "__main__":
    unittest.main()
