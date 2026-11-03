"""Python Security Profile gate (Issue #114).

Bandit's JSON report is evaluated against ``config/python_security_profile.json``:
findings at or above the profile thresholds block, security-sensitive modules
also block on an explicit list of lower-severity rules, and every suppression
must carry a reason and an expiry date. A pip-audit JSON report can be
evaluated as an advisory dependency audit connected to Issue #90.

The evaluation itself never accesses the network; only the optional
``--run-bandit`` mode starts Bandit as a local subprocess.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

PROFILE_SCHEMA_VERSION = 1
DEFAULT_PROFILE = Path("config/python_security_profile.json")
DEFAULT_OUTPUT = Path("build/python-security/report.json")
SEVERITY_ORDER = {"UNDEFINED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
KNOWN_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
TOOLS = frozenset({"bandit", "pip-audit"})
DEPENDENCY_AUDIT_MODES = frozenset({"advisory", "blocking"})
MINIMUM_REASON_LENGTH = 10
# Hard ceiling: a profile cannot raise the suppression lifetime above one review year.
MAXIMUM_SUPPRESSION_DAYS = 366
ISSUE_REFERENCE = re.compile(r"^#[1-9][0-9]*$")

EXPIRED_SUPPRESSION = "PSP901"
STALE_SUPPRESSION = "PSP902"
SCAN_ERROR = "PSP903"


class ProfileError(ValueError):
    """The security profile or a tool report cannot be trusted as input."""


@dataclass(frozen=True)
class Suppression:
    tool: str
    rule: str
    target: str
    match: str
    reason: str
    expires: date
    issue: str
    count: int = 1

    def label(self) -> str:
        return f"{self.tool}:{self.rule}:{self.target}"


@dataclass(frozen=True)
class SecurityProfile:
    scan_paths: tuple[str, ...]
    min_severity: str
    min_confidence: str
    sensitive_paths: tuple[str, ...]
    sensitive_rules: frozenset[str]
    max_suppression_days: int
    dependency_audit_mode: str
    suppressions: tuple[Suppression, ...]


@dataclass
class Finding:
    code: str
    path: str
    line: int
    message: str
    severity: str = "HIGH"
    blocking: bool = True
    suppressed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "blocking": self.blocking,
            "suppressed_by": self.suppressed_by,
            "message": self.message,
        }


@dataclass
class EvaluationReport:
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    # Bandit counts files; pip-audit counts dependencies, reported under a separate key.
    scanned_unit: str = "files"

    @property
    def blocking(self) -> list[Finding]:
        return [
            finding
            for finding in self.findings
            if finding.blocking and finding.suppressed_by is None
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "passed": not self.blocking,
            f"scanned_{self.scanned_unit}": self.scanned_files,
            "blocking_findings": len(self.blocking),
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _require(mapping: dict[str, Any], key: str, kind: type, context: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise ProfileError(f"{context}.{key} must be {kind.__name__}")
    return value


def _string_list(
    mapping: dict[str, Any], key: str, context: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    values = _require(mapping, key, list, context)
    if (not values and not allow_empty) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ProfileError(
            f"{context}.{key} must be a non-empty list of non-empty strings"
        )
    return tuple(value.replace("\\", "/") for value in values)


def _scan_paths(mapping: dict[str, Any]) -> tuple[str, ...]:
    paths = _string_list(mapping, "scan_paths", "bandit", allow_empty=False)
    for path in paths:
        if (
            PurePosixPath(path).is_absolute()
            or ":" in path
            or ".." in PurePosixPath(path).parts
            # Bandit receives these as argv; a leading "-" would be parsed as an option.
            or path.startswith("-")
        ):
            raise ProfileError("bandit.scan_paths must be repository-relative paths")
    return paths


def _level(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _require(mapping, key, str, context).upper()
    if value not in SEVERITY_ORDER or value == "UNDEFINED":
        raise ProfileError(f"{context}.{key} must be LOW, MEDIUM, or HIGH")
    return value


def _parse_suppression(raw: Any, index: int, today: date, max_days: int) -> Suppression:
    context = f"suppressions[{index}]"
    if not isinstance(raw, dict):
        raise ProfileError(f"{context} must be an object")
    tool = _require(raw, "tool", str, context)
    if tool not in TOOLS:
        raise ProfileError(f"{context}.tool must be one of {sorted(TOOLS)}")
    target_key = "package" if tool == "pip-audit" else "path"
    target = _require(raw, target_key, str, context).strip()
    rule = _require(raw, "rule", str, context).strip()
    reason = _require(raw, "reason", str, context).strip()
    issue = _require(raw, "issue", str, context).strip()
    if not target or not rule or not ISSUE_REFERENCE.match(issue):
        raise ProfileError(
            f"{context} needs a rule, a {target_key}, and an Issue reference such as #114"
        )
    if len(reason) < MINIMUM_REASON_LENGTH:
        raise ProfileError(f"{context}.reason must explain the accepted risk")
    try:
        expires = date.fromisoformat(_require(raw, "expires", str, context))
    except ValueError as error:
        raise ProfileError(f"{context}.expires must be YYYY-MM-DD") from error
    if (expires - today).days > max_days:
        raise ProfileError(
            f"{context}.expires exceeds the {max_days}-day suppression limit"
        )
    match = raw.get("match", "")
    if not isinstance(match, str):
        raise ProfileError(f"{context}.match must be a string")
    if tool == "bandit" and not match.strip():
        raise ProfileError(f"{context}.match must name the flagged source text")
    count = raw.get("count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ProfileError(f"{context}.count must be a positive integer")
    target = target.replace("\\", "/")
    if tool == "bandit" and _outside_root(target):
        raise ProfileError(f"{context}.path must be a repository-relative path")
    return Suppression(tool, rule, target, match, reason, expires, issue, count)


def parse_profile(raw: Any, today: date) -> SecurityProfile:
    if not isinstance(raw, dict) or raw.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ProfileError(f"profile schema_version must be {PROFILE_SCHEMA_VERSION}")
    bandit = _require(raw, "bandit", dict, "profile")
    sensitive = _require(raw, "sensitive_modules", dict, "profile")
    dependency_audit = _require(raw, "dependency_audit", dict, "profile")
    mode = _require(dependency_audit, "mode", str, "dependency_audit")
    if mode not in DEPENDENCY_AUDIT_MODES:
        raise ProfileError(
            f"dependency_audit.mode must be one of {sorted(DEPENDENCY_AUDIT_MODES)}"
        )
    max_days = _require(raw, "max_suppression_days", int, "profile")
    if not 0 < max_days <= MAXIMUM_SUPPRESSION_DAYS:
        raise ProfileError(
            f"profile.max_suppression_days must be between 1 and {MAXIMUM_SUPPRESSION_DAYS}"
        )
    audit_issue = _require(dependency_audit, "issue", str, "dependency_audit")
    if not ISSUE_REFERENCE.match(audit_issue):
        raise ProfileError(
            "dependency_audit.issue must be an Issue reference such as #90"
        )
    raw_suppressions = _require(raw, "suppressions", list, "profile")
    suppressions = tuple(
        _parse_suppression(entry, index, today, max_days)
        for index, entry in enumerate(raw_suppressions)
    )
    return SecurityProfile(
        scan_paths=_scan_paths(bandit),
        min_severity=_level(bandit, "min_severity", "bandit"),
        min_confidence=_level(bandit, "min_confidence", "bandit"),
        sensitive_paths=_string_list(sensitive, "paths", "sensitive_modules"),
        sensitive_rules=frozenset(
            _string_list(sensitive, "extra_blocking_rules", "sensitive_modules")
        ),
        max_suppression_days=max_days,
        dependency_audit_mode=mode,
        suppressions=suppressions,
    )


def load_profile(path: Path, today: date) -> SecurityProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read profile {path}: {error}") from error
    return parse_profile(raw, today)


def _outside_root(path: str) -> bool:
    posix = PurePosixPath(path)
    return posix.is_absolute() or ".." in posix.parts or ":" in path


def _distribution_name(name: str) -> str:
    # PEP 503 normalisation, so "Foo_Bar", "foo.bar" and "foo-bar" are the same package.
    return re.sub(r"[-_.]+", "-", name).lower()


def _relative_path(filename: str, root: Path) -> str:
    candidate = Path(filename.replace("\\", "/"))
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            pass
    text = candidate.as_posix()
    return text[2:] if text.startswith("./") else text


def is_sensitive(path: str, profile: SecurityProfile) -> bool:
    posix = PurePosixPath(path)
    return any(
        posix.match(pattern) or fnmatch.fnmatchcase(path, pattern)
        for pattern in profile.sensitive_paths
    )


def _source_lines(root: Path, path: str, line_range: list[int]) -> str:
    try:
        lines = (root / path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    return "\n".join(
        lines[number - 1] for number in line_range if 0 < number <= len(lines)
    )


def _active_suppressions(
    profile: SecurityProfile, tool: str, today: date, report: EvaluationReport
) -> list[Suppression]:
    active: list[Suppression] = []
    for suppression in profile.suppressions:
        if suppression.tool != tool:
            continue
        if suppression.expires < today:
            report.findings.append(
                Finding(
                    EXPIRED_SUPPRESSION,
                    suppression.target,
                    0,
                    f"suppression {suppression.label()} expired on {suppression.expires.isoformat()}; "
                    "fix the finding or renew it with a new review",
                )
            )
            continue
        active.append(suppression)
    return active


def _claim(
    suppression: Suppression, used: dict[Suppression, int], finding: Finding
) -> bool:
    # One entry covers at most `count` findings, so a new identical call site still blocks.
    if used.get(suppression, 0) >= suppression.count:
        return False
    used[suppression] = used.get(suppression, 0) + 1
    finding.suppressed_by = suppression.label()
    return True


def _report_stale(
    active: list[Suppression], used: dict[Suppression, int], report: EvaluationReport
) -> None:
    for suppression in active:
        matched = used.get(suppression, 0)
        if matched < suppression.count:
            report.findings.append(
                Finding(
                    STALE_SUPPRESSION,
                    suppression.target,
                    0,
                    f"suppression {suppression.label()} matched {matched} of "
                    f"{suppression.count} findings; update or remove it",
                )
            )


def evaluate_bandit(
    raw: Any, profile: SecurityProfile, root: Path, today: date
) -> EvaluationReport:
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise ProfileError("Bandit report must be a JSON object with a results list")
    report = EvaluationReport()
    metrics = raw.get("metrics")
    if isinstance(metrics, dict):
        # Count only metrics keys that name real files under the root, not arbitrary keys.
        for key in metrics:
            if key == "_totals":
                continue
            relative = _relative_path(str(key), root)
            if not _outside_root(relative) and (root / relative).is_file():
                report.scanned_files += 1
    if report.scanned_files == 0:
        report.findings.append(
            Finding(SCAN_ERROR, ".", 0, "Bandit report lists no scanned files")
        )
    for error in raw.get("errors") or []:
        filename = error.get("filename", "?") if isinstance(error, dict) else "?"
        reason = (
            error.get("reason", "unknown error")
            if isinstance(error, dict)
            else str(error)
        )
        report.findings.append(
            Finding(
                SCAN_ERROR,
                _relative_path(str(filename), root),
                0,
                f"Bandit could not scan the file: {reason}",
            )
        )
    active = _active_suppressions(profile, "bandit", today, report)
    used: dict[Suppression, int] = {}
    minimum_severity = SEVERITY_ORDER[profile.min_severity]
    minimum_confidence = SEVERITY_ORDER[profile.min_confidence]
    for result in raw["results"]:
        if not isinstance(result, dict):
            raise ProfileError("Bandit results must be objects")
        path = _relative_path(str(result.get("filename", "")), root)
        rule = str(result.get("test_id", ""))
        severity = str(result.get("issue_severity", "UNDEFINED")).upper()
        confidence = str(result.get("issue_confidence", "UNDEFINED")).upper()
        line = int(result.get("line_number") or 0)
        # Unknown levels or paths outside the root cannot be judged by the profile: fail closed.
        unjudgeable = (
            severity not in KNOWN_LEVELS
            or confidence not in KNOWN_LEVELS
            or _outside_root(path)
        )
        meets_threshold = unjudgeable or (
            SEVERITY_ORDER[severity] >= minimum_severity
            and SEVERITY_ORDER[confidence] >= minimum_confidence
        )
        # Sensitive modules lower the severity floor for listed rules, not the confidence floor.
        sensitive_rule = (
            rule in profile.sensitive_rules
            and is_sensitive(path, profile)
            and SEVERITY_ORDER.get(confidence, 0) >= minimum_confidence
        )
        finding = Finding(
            f"bandit:{rule}",
            path,
            line,
            f"{result.get('issue_text', '')} (severity={severity}, confidence={confidence}"
            + (", security-sensitive module" if sensitive_rule else "")
            + ")",
            severity=severity,
            blocking=meets_threshold or sensitive_rule,
        )
        # An unjudgeable finding can never be suppressed.
        if finding.blocking and not unjudgeable:
            line_range = result.get("line_range")
            numbers = (
                [int(number) for number in line_range]
                if isinstance(line_range, list)
                else [line]
            )
            source = _source_lines(root, path, numbers)
            for suppression in active:
                if (
                    suppression.rule == rule
                    and suppression.target == path
                    and suppression.match in source
                    and _claim(suppression, used, finding)
                ):
                    break
        report.findings.append(finding)
    _report_stale(active, used, report)
    return report


def evaluate_pip_audit(
    raw: Any, profile: SecurityProfile, today: date
) -> EvaluationReport:
    if not isinstance(raw, dict) or not isinstance(raw.get("dependencies"), list):
        raise ProfileError(
            "pip-audit report must be a JSON object with a dependencies list"
        )
    report = EvaluationReport(scanned_unit="dependencies")
    if not raw["dependencies"]:
        report.findings.append(
            Finding(
                SCAN_ERROR, ".", 0, "pip-audit report lists no audited dependencies"
            )
        )
    active = _active_suppressions(profile, "pip-audit", today, report)
    used: dict[Suppression, int] = {}
    blocking = profile.dependency_audit_mode == "blocking"
    for dependency in raw["dependencies"]:
        if not isinstance(dependency, dict):
            raise ProfileError("pip-audit dependencies must be objects")
        report.scanned_files += 1
        name = dependency.get("name")
        vulns = dependency.get("vulns")
        if not isinstance(name, str) or not name or not isinstance(vulns, list):
            raise ProfileError("pip-audit dependencies need a name and a vulns list")
        name = _distribution_name(name)
        version = str(dependency.get("version", ""))
        for vulnerability in vulns:
            if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
                raise ProfileError("pip-audit vulnerabilities need an id")
            aliases = vulnerability.get("aliases") or []
            if not isinstance(aliases, list):
                raise ProfileError("pip-audit aliases must be a list")
            identifiers = {str(vulnerability.get("id", ""))} | {
                str(alias) for alias in aliases
            }
            fixes = (
                ", ".join(str(fix) for fix in vulnerability.get("fix_versions") or [])
                or "none"
            )
            finding = Finding(
                f"pip-audit:{vulnerability.get('id', '?')}",
                f"{name}=={version}",
                0,
                f"known vulnerability; fix versions: {fixes}",
                blocking=blocking,
            )
            for suppression in active:
                if (
                    _distribution_name(suppression.target) == name
                    and suppression.rule in identifiers
                    and _claim(suppression, used, finding)
                ):
                    break
            report.findings.append(finding)
    _report_stale(active, used, report)
    return report


def run_bandit(profile: SecurityProfile, root: Path) -> Any:
    # Bandit reads ".bandit" files under its targets, which could narrow the scan outside the profile.
    overrides = [root / ".bandit"] if (root / ".bandit").exists() else []
    for scan_path in profile.scan_paths:
        overrides.extend((root / scan_path).rglob(".bandit"))
    if overrides:
        raise ProfileError(f"{overrides[0]} would override the profile; remove it")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "bandit.json"
        command = [
            sys.executable,
            # Isolated mode: a bandit.py in the checkout or PYTHONPATH cannot shadow the scanner.
            "-I",
            "-m",
            "bandit",
            "--recursive",
            "--format",
            "json",
            "--output",
            str(output),
            "--exit-zero",
            "--quiet",
            # Inline "# nosec" has no reason or expiry; the profile owns every suppression.
            "--ignore-nosec",
            *profile.scan_paths,
        ]
        # Fixed argv without a shell; Bandit is the pinned module from the security requirements.
        completed = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            command, cwd=root, check=False
        )
        if completed.returncode != 0 or not output.exists():
            raise ProfileError(f"Bandit failed with exit code {completed.returncode}")
        return json.loads(output.read_text(encoding="utf-8"))


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read {label} {path}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the Python Security Profile (Issue #114)"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--run-bandit",
        action="store_true",
        help="run the installed Bandit on profile scan paths",
    )
    source.add_argument(
        "--bandit-report", type=Path, help="evaluate an existing Bandit JSON report"
    )
    source.add_argument(
        "--pip-audit-report", type=Path, help="evaluate a pip-audit JSON report"
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="override the current date (YYYY-MM-DD)",
    )
    arguments = parser.parse_args(argv)
    today = arguments.today or date.today()
    try:
        profile = load_profile(arguments.profile, today)
        if arguments.pip_audit_report is not None:
            label = "PYTHON DEPENDENCY AUDIT"
            report = evaluate_pip_audit(
                _read_json(arguments.pip_audit_report, "pip-audit report"),
                profile,
                today,
            )
        else:
            label = "PYTHON SECURITY CHECK"
            raw = (
                run_bandit(profile, arguments.root)
                if arguments.run_bandit
                else _read_json(arguments.bandit_report, "Bandit report")
            )
            report = evaluate_bandit(raw, profile, arguments.root, today)
    except (ProfileError, TypeError, ValueError, AttributeError) as error:
        # A malformed report field is an unreadable report, not a passing scan.
        print(f"PYTHON SECURITY PROFILE ERROR: {error}", file=sys.stderr)
        return 2

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for finding in report.findings:
        if finding.suppressed_by is not None:
            print(
                f"S {finding.code} {finding.path}:{finding.line} suppressed by {finding.suppressed_by}"
            )
        elif finding.blocking:
            print(f"E {finding.code} {finding.path}:{finding.line} {finding.message}")
    advisory = [
        finding
        for finding in report.findings
        if not finding.blocking and finding.suppressed_by is None
    ]
    if arguments.pip_audit_report is not None:
        for finding in advisory:
            print(f"W {finding.code} {finding.path} {finding.message}")
    if report.blocking:
        print(f"{label} FAILED blocking={len(report.blocking)}", file=sys.stderr)
        return 1
    print(
        f"{label} OK scanned_{report.scanned_unit}={report.scanned_files} "
        f"advisory={len(advisory)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
