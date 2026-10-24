from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from tools.workflow_security_check import (
    DANGEROUS_WORKFLOW_TRIGGER,
    INHERITED_REUSABLE_WORKFLOW_SECRETS,
    MISSING_WORKFLOW_PERMISSIONS,
    OVERBROAD_WORKFLOW_PERMISSIONS,
    PERSISTED_CHECKOUT_CREDENTIALS,
    UNTRUSTED_WORKFLOW_ARTIFACT,
    UNSAFE_SHELL_INTERPOLATION,
    scan_workflow_path,
    scan_workflow_text,
)

TEST_DATE = date(2026, 10, 16)


class WorkflowSecurityCheckTests(unittest.TestCase):
    def test_minimal_permissions_and_environment_based_input_pass(self) -> None:
        workflow_text = """name: tests
on: pull_request
permissions:
  contents: read
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - name: Print the issue title
        env:
          ISSUE_TITLE: ${{ github.event.issue.title }}
        run: echo "$ISSUE_TITLE"
"""

        findings = scan_workflow_text(workflow_text, Path("safe.yml"), TEST_DATE)

        self.assertEqual([], findings)

    def test_missing_and_overbroad_permission_mappings_are_reported(self) -> None:
        workflow_text = """name: broad-token
on: push
jobs:
  build:
    permissions: write-all
"""

        findings = scan_workflow_text(workflow_text, Path("broad.yml"), TEST_DATE)

        self.assertEqual(
            {MISSING_WORKFLOW_PERMISSIONS, OVERBROAD_WORKFLOW_PERMISSIONS},
            {finding.rule for finding in findings},
        )

    def test_write_permission_in_a_scope_is_reported(self) -> None:
        workflow_text = """name: publish
on: push
permissions:
  contents: "write"
"""

        findings = scan_workflow_text(workflow_text, Path("write.yml"), TEST_DATE)

        self.assertEqual([OVERBROAD_WORKFLOW_PERMISSIONS], [finding.rule for finding in findings])

    def test_pull_request_target_and_direct_event_shell_interpolation_are_reported(self) -> None:
        workflow_text = """name: unsafe-pr
on:
  pull_request_target:
permissions:
  contents: read
jobs:
  greet:
    steps:
      - run: |2-
          # A shell comment is still unsafe when untrusted text can insert a newline.
          echo "${{ github.event.pull_request.title }}"
"""

        findings = scan_workflow_text(workflow_text, Path("unsafe-pr.yml"), TEST_DATE)

        self.assertEqual(
            {DANGEROUS_WORKFLOW_TRIGGER, UNSAFE_SHELL_INTERPOLATION},
            {finding.rule for finding in findings},
        )

    def test_inline_workflow_run_and_direct_secret_shell_interpolation_are_reported(self) -> None:
        workflow_text = """name: unsafe-artifact
on: [push, workflow_run]
permissions:
  contents: read
jobs:
  publish:
    steps:
      - run: echo "${{ secrets.RELEASE_TOKEN }}" "# workflow-security: waive WFS004 until=2099-12-31 reason=not a real YAML comment"
"""

        findings = scan_workflow_text(workflow_text, Path("unsafe-artifact.yml"), TEST_DATE)

        self.assertEqual(
            {DANGEROUS_WORKFLOW_TRIGGER, UNSAFE_SHELL_INTERPOLATION},
            {finding.rule for finding in findings},
        )

    def test_workflow_run_artifact_download_checkout_and_secret_inheritance_are_reported(self) -> None:
        workflow_text = """name: consume-artifact
on:
  workflow_run:
permissions:
  contents: read
jobs:
  consume:
    uses: owner/repository/.github/workflows/reusable.yml@main
    secrets: inherit
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
"""

        findings = scan_workflow_text(workflow_text, Path("artifact.yml"), TEST_DATE)

        self.assertEqual(
            {
                DANGEROUS_WORKFLOW_TRIGGER,
                PERSISTED_CHECKOUT_CREDENTIALS,
                UNTRUSTED_WORKFLOW_ARTIFACT,
                INHERITED_REUSABLE_WORKFLOW_SECRETS,
            },
            {finding.rule for finding in findings},
        )

    def test_true_checkout_credential_persistence_is_reported_at_its_setting(self) -> None:
        workflow_text = """name: credentialed-checkout
on: push
permissions:
  contents: read
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: "true"
"""

        findings = scan_workflow_text(workflow_text, Path("credentialed.yml"), TEST_DATE)

        self.assertEqual([PERSISTED_CHECKOUT_CREDENTIALS], [finding.rule for finding in findings])
        self.assertEqual(10, findings[0].line_number)

    def test_valid_waiver_suppresses_only_the_named_permission_rule(self) -> None:
        workflow_text = """name: publish
on: push
permissions:
  contents: write # workflow-security: waive WFS002 until=2099-12-31 reason="release workflow publishes the packaged application"
"""

        findings = scan_workflow_text(workflow_text, Path("waived.yml"), TEST_DATE)

        self.assertEqual([], findings)

    def test_expired_waiver_does_not_suppress_a_permission_finding(self) -> None:
        workflow_text = """name: publish
on: push
permissions:
  contents: write # workflow-security: waive WFS002 until=2025-12-31 reason="release workflow publishes the packaged application"
"""

        findings = scan_workflow_text(workflow_text, Path("expired.yml"), TEST_DATE)

        self.assertEqual([OVERBROAD_WORKFLOW_PERMISSIONS], [finding.rule for finding in findings])

    def test_current_workflow_directory_has_no_security_findings(self) -> None:
        findings = scan_workflow_path(Path(".github/workflows"), TEST_DATE)
        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
