from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from tools.python_security_check import (
    EXPIRED_SUPPRESSION,
    SCAN_ERROR,
    STALE_SUPPRESSION,
    ProfileError,
    evaluate_bandit,
    evaluate_pip_audit,
    load_profile,
    main,
    parse_profile,
)

TODAY = date(2026, 11, 4)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _profile(**overrides: Any) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "schema_version": 1,
        "bandit": {
            "scan_paths": ["src"],
            "min_severity": "MEDIUM",
            "min_confidence": "MEDIUM",
        },
        "sensitive_modules": {
            "paths": ["src/pkg/*_provider.py"],
            "extra_blocking_rules": ["B603", "B110"],
        },
        "max_suppression_days": 366,
        "dependency_audit": {"mode": "advisory", "issue": "#90"},
        "suppressions": [],
    }
    profile.update(overrides)
    return profile


def _suppression(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "tool": "bandit",
        "rule": "B310",
        "path": "src/pkg/net.py",
        "match": "urlopen(request",
        "reason": "endpoint is fixed to a validated https host",
        "expires": "2027-01-31",
        "issue": "#114",
    }
    entry.update(overrides)
    return entry


def _result(
    path: str, rule: str, severity: str, line: int = 2, confidence: str = "HIGH"
) -> dict[str, Any]:
    return {
        "filename": path,
        "test_id": rule,
        "issue_severity": severity,
        "issue_confidence": confidence,
        "issue_text": f"{rule} finding",
        "line_number": line,
        "line_range": [line],
    }


def _bandit(
    results: list[dict[str, Any]], errors: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "results": results,
        "errors": errors or [],
        "metrics": {"_totals": {}, "src/pkg/net.py": {}},
    }


class PythonSecurityProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "src/pkg").mkdir(parents=True)
        (self.root / "src/pkg/net.py").write_text(
            "from urllib.request import urlopen\nwith urlopen(request, timeout=5) as response:\n    pass\n",
            encoding="utf-8",
        )
        (self.root / "src/pkg/ollama_provider.py").write_text(
            "import subprocess\nsubprocess.Popen(command)\n", encoding="utf-8"
        )
        (self.root / "src/pkg/plain.py").write_text(
            "import subprocess\nsubprocess.Popen(command)\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _evaluate(self, results: list[dict[str, Any]], **profile_overrides: Any):
        profile = parse_profile(_profile(**profile_overrides), TODAY)
        return evaluate_bandit(_bandit(results), profile, self.root, TODAY)

    def test_threshold_finding_blocks_and_low_finding_is_advisory(self) -> None:
        report = self._evaluate(
            [
                _result("src/pkg/net.py", "B310", "MEDIUM"),
                _result("src/pkg/plain.py", "B603", "LOW"),
            ]
        )

        self.assertEqual(["bandit:B310"], [finding.code for finding in report.blocking])
        self.assertFalse(report.as_dict()["passed"])

    def test_low_confidence_finding_does_not_block(self) -> None:
        report = self._evaluate(
            [_result("src/pkg/net.py", "B310", "HIGH", confidence="LOW")]
        )

        self.assertEqual([], report.blocking)

    def test_sensitive_module_keeps_confidence_floor(self) -> None:
        report = self._evaluate(
            [_result("src/pkg/ollama_provider.py", "B603", "LOW", confidence="LOW")]
        )

        self.assertEqual([], report.blocking)

    def test_sensitive_module_blocks_listed_low_severity_rule(self) -> None:
        report = self._evaluate([_result("src/pkg/ollama_provider.py", "B603", "LOW")])

        self.assertEqual(
            ["src/pkg/ollama_provider.py"],
            [finding.path for finding in report.blocking],
        )
        self.assertIn("security-sensitive module", report.blocking[0].message)

    def test_windows_style_bandit_path_is_normalized(self) -> None:
        report = self._evaluate(
            [_result("src\\pkg\\ollama_provider.py", "B603", "LOW")]
        )

        self.assertEqual("src/pkg/ollama_provider.py", report.blocking[0].path)

    def test_valid_suppression_requires_matching_rule_path_and_source_text(
        self,
    ) -> None:
        report = self._evaluate(
            [_result("src/pkg/net.py", "B310", "MEDIUM")], suppressions=[_suppression()]
        )

        self.assertEqual([], report.blocking)
        self.assertEqual("bandit:B310:src/pkg/net.py", report.findings[0].suppressed_by)

    def test_suppression_with_other_source_text_does_not_hide_finding(self) -> None:
        report = self._evaluate(
            [_result("src/pkg/net.py", "B310", "MEDIUM")],
            suppressions=[_suppression(match="urlopen(other")],
        )

        self.assertEqual(
            {"bandit:B310", STALE_SUPPRESSION},
            {finding.code for finding in report.blocking},
        )

    def test_expired_suppression_blocks_even_if_finding_matches(self) -> None:
        report = self._evaluate(
            [_result("src/pkg/net.py", "B310", "MEDIUM")],
            suppressions=[_suppression(expires="2026-11-03")],
        )

        self.assertEqual(
            {"bandit:B310", EXPIRED_SUPPRESSION},
            {finding.code for finding in report.blocking},
        )

    def test_unused_suppression_is_reported_as_stale(self) -> None:
        report = self._evaluate([], suppressions=[_suppression()])

        self.assertEqual(
            [STALE_SUPPRESSION], [finding.code for finding in report.blocking]
        )

    def test_bandit_scan_errors_block(self) -> None:
        profile = parse_profile(_profile(), TODAY)
        report = evaluate_bandit(
            _bandit([], [{"filename": "src/pkg/bad.py", "reason": "syntax error"}]),
            profile,
            self.root,
            TODAY,
        )

        self.assertEqual([SCAN_ERROR], [finding.code for finding in report.blocking])

    def test_suppression_covers_only_its_declared_count(self) -> None:
        findings = [
            _result("src/pkg/net.py", "B310", "MEDIUM"),
            _result("src/pkg/net.py", "B310", "MEDIUM"),
        ]

        single = self._evaluate(findings, suppressions=[_suppression()])
        double = self._evaluate(findings, suppressions=[_suppression(count=2)])
        excess = self._evaluate(findings[:1], suppressions=[_suppression(count=2)])

        self.assertEqual(["bandit:B310"], [finding.code for finding in single.blocking])
        self.assertEqual([], double.blocking)
        self.assertEqual(
            [STALE_SUPPRESSION], [finding.code for finding in excess.blocking]
        )

    def test_unknown_level_and_outside_path_fail_closed(self) -> None:
        report = self._evaluate(
            [
                _result("src/pkg/plain.py", "B603", "UNDEFINED"),
                _result("../elsewhere/plain.py", "B603", "LOW"),
            ]
        )

        self.assertEqual(2, len(report.blocking))

    def test_report_without_scanned_files_blocks(self) -> None:
        profile = parse_profile(_profile(), TODAY)

        report = evaluate_bandit({"results": []}, profile, self.root, TODAY)

        self.assertEqual([SCAN_ERROR], [finding.code for finding in report.blocking])

    def test_suppression_requires_reason_expiry_issue_and_bounded_lifetime(
        self,
    ) -> None:
        invalid_entries = [
            _suppression(reason="short"),
            _suppression(expires="next year"),
            _suppression(expires="2028-01-01"),
            _suppression(issue="114"),
            _suppression(issue="#todo"),
            _suppression(match=""),
            _suppression(tool="semgrep"),
            _suppression(count=0),
        ]
        for entry in invalid_entries:
            with self.subTest(entry=entry), self.assertRaises(ProfileError):
                parse_profile(_profile(suppressions=[entry]), TODAY)

    def test_profile_rejects_unknown_schema_and_thresholds(self) -> None:
        for raw in (
            _profile(schema_version=2),
            _profile(
                bandit={
                    "scan_paths": ["src"],
                    "min_severity": "CRITICAL",
                    "min_confidence": "LOW",
                }
            ),
            _profile(dependency_audit={"mode": "ignore", "issue": "#90"}),
            _profile(dependency_audit={"mode": "advisory"}),
            _profile(
                bandit={
                    "scan_paths": [],
                    "min_severity": "MEDIUM",
                    "min_confidence": "MEDIUM",
                }
            ),
            _profile(
                bandit={
                    "scan_paths": ["../outside"],
                    "min_severity": "MEDIUM",
                    "min_confidence": "MEDIUM",
                }
            ),
            _profile(max_suppression_days=367),
            _profile(max_suppression_days=0),
        ):
            with self.subTest(raw=raw), self.assertRaises(ProfileError):
                parse_profile(raw, TODAY)

    def test_pip_audit_is_advisory_and_supports_alias_suppression(self) -> None:
        raw = {
            "dependencies": [
                {
                    "name": "Pillow",
                    "version": "1.0",
                    "vulns": [
                        {"id": "PYSEC-1", "aliases": ["CVE-1"], "fix_versions": ["2.0"]}
                    ],
                },
                {
                    "name": "idna",
                    "version": "3.0",
                    "vulns": [{"id": "GHSA-x", "fix_versions": []}],
                },
            ]
        }
        entry = _suppression(tool="pip-audit", rule="CVE-1", package="pillow", match="")
        del entry["path"]
        profile = parse_profile(_profile(suppressions=[entry]), TODAY)

        report = evaluate_pip_audit(raw, profile, TODAY)

        self.assertEqual([], report.blocking)
        self.assertEqual(
            ["pip-audit:PYSEC-1", "pip-audit:GHSA-x"],
            [finding.code for finding in report.findings],
        )
        self.assertEqual("pip-audit:CVE-1:pillow", report.findings[0].suppressed_by)
        self.assertIsNone(report.findings[1].suppressed_by)

    def test_pip_audit_blocking_mode_blocks_unsuppressed_vulnerability(self) -> None:
        profile = parse_profile(
            _profile(dependency_audit={"mode": "blocking", "issue": "#90"}), TODAY
        )
        raw = {
            "dependencies": [
                {"name": "idna", "version": "3.0", "vulns": [{"id": "GHSA-x"}]}
            ]
        }

        report = evaluate_pip_audit(raw, profile, TODAY)

        self.assertEqual(
            ["pip-audit:GHSA-x"], [finding.code for finding in report.blocking]
        )

    def test_cli_writes_report_and_returns_failure_exit_code(self) -> None:
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
        bandit_path = self.root / "bandit.json"
        bandit_path.write_text(
            json.dumps(_bandit([_result("src/pkg/net.py", "B310", "HIGH")])),
            encoding="utf-8",
        )
        output = self.root / "out/report.json"

        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                [
                    "--bandit-report",
                    str(bandit_path),
                    "--profile",
                    str(profile_path),
                    "--root",
                    str(self.root),
                    "--output",
                    str(output),
                    "--today",
                    TODAY.isoformat(),
                ]
            )

        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["passed"])

    def test_cli_rejects_malformed_report_fields_with_exit_code_two(self) -> None:
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
        bandit_path = self.root / "bandit.json"
        result = _result("src/pkg/net.py", "B310", "HIGH")
        result["line_number"] = "two"
        bandit_path.write_text(json.dumps(_bandit([result])), encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = main(
                [
                    "--bandit-report",
                    str(bandit_path),
                    "--profile",
                    str(profile_path),
                    "--root",
                    str(self.root),
                    "--output",
                    str(self.root / "out.json"),
                ]
            )

        self.assertEqual(2, exit_code)

    def test_cli_reports_profile_errors_with_exit_code_two(self) -> None:
        profile_path = self.root / "profile.json"
        profile_path.write_text("{", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = main(
                ["--bandit-report", "missing.json", "--profile", str(profile_path)]
            )

        self.assertEqual(2, exit_code)


class RepositorySecurityProfileTests(unittest.TestCase):
    def test_repository_profile_is_valid_and_suppressions_target_existing_files(
        self,
    ) -> None:
        profile = load_profile(
            REPOSITORY_ROOT / "config/python_security_profile.json", TODAY
        )

        self.assertEqual("advisory", profile.dependency_audit_mode)
        for suppression in profile.suppressions:
            with self.subTest(suppression=suppression.label()):
                source = (REPOSITORY_ROOT / suppression.target).read_text(
                    encoding="utf-8"
                )
                self.assertIn(suppression.match, source)

    def test_security_tools_are_pinned_and_wired_into_ci(self) -> None:
        requirements = (
            REPOSITORY_ROOT / "tools/python_security_requirements.txt"
        ).read_text(encoding="utf-8")
        pins = [
            line
            for line in requirements.splitlines()
            if line and not line.startswith("#")
        ]
        workflow = (
            REPOSITORY_ROOT / ".github/workflows/python-security-ci.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual({"bandit", "pip-audit"}, {pin.split("==")[0] for pin in pins})
        self.assertTrue(all("==" in pin for pin in pins))
        self.assertIn("pull_request:", workflow)
        self.assertIn("tools/python_security_check.py --run-bandit", workflow)
        self.assertIn("--pip-audit-report", workflow)


if __name__ == "__main__":
    unittest.main()
