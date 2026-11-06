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

    def test_workflow_passes_the_repository_security_scan(self):
        self.assertEqual([], scan_workflow_path(WORKFLOW, date(2026, 11, 7)))


if __name__ == "__main__":
    unittest.main()
