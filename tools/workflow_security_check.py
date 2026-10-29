from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

MISSING_WORKFLOW_PERMISSIONS = "WFS001"
OVERBROAD_WORKFLOW_PERMISSIONS = "WFS002"
DANGEROUS_WORKFLOW_TRIGGER = "WFS003"
UNSAFE_SHELL_INTERPOLATION = "WFS004"
PERSISTED_CHECKOUT_CREDENTIALS = "WFS005"
UNTRUSTED_WORKFLOW_ARTIFACT = "WFS006"
INHERITED_REUSABLE_WORKFLOW_SECRETS = "WFS007"

MINIMUM_WAIVER_REASON_LENGTH = 10
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})

PERMISSIONS_LINE_PATTERN = re.compile(r"^(?P<indent> *)permissions\s*:\s*(?P<value>.*)$")
OVERBROAD_PERMISSION_PATTERN = re.compile(
    r"(?:\b(?:read|write)-all\b|"
    r"\b[A-Za-z0-9_-]+\s*:\s*['\"]?write(?:['\"])?(?=\s|#|,|}|$))",
    re.IGNORECASE,
)
DANGEROUS_TRIGGER_PATTERN = re.compile(r"^\s*(?P<trigger>pull_request_target|workflow_run)\s*:", re.IGNORECASE)
INLINE_TRIGGER_PATTERN = re.compile(r"^on\s*:\s*(?P<value>.+)$", re.IGNORECASE)
RUN_LINE_PATTERN = re.compile(r"^(?P<indent> *)-?\s*run\s*:\s*(?P<value>.*)$")
BLOCK_SCALAR_PATTERN = re.compile(r"[|>](?:[+-]?\d)?[+-]?")
UNTRUSTED_SHELL_EXPRESSION_PATTERN = re.compile(
    r"\$\{\{\s*(?:"
    r"github\.(?:event(?:\.|\b)|head_ref\b|base_ref\b|ref\b)|"
    r"inputs(?:\.|\b)|needs\.[A-Za-z0-9_-]+\.outputs\.|steps\.[A-Za-z0-9_-]+\.outputs\."
    r")",
    re.IGNORECASE,
)
SECRET_SHELL_EXPRESSION_PATTERN = re.compile(r"\$\{\{\s*secrets\.[^}]+\}\}", re.IGNORECASE)
CHECKOUT_STEP_PATTERN = re.compile(
    r"^(?P<indent> *)-\s*uses\s*:\s*['\"]?actions/checkout(?:@[^'\"\s]+)?['\"]?\s*$",
    re.IGNORECASE,
)
PERSIST_CHECKOUT_CREDENTIALS_PATTERN = re.compile(
    r"^\s*persist-credentials\s*:\s*(?P<value>[^\s#]+)", re.IGNORECASE
)
DOWNLOAD_ARTIFACT_PATTERN = re.compile(r"actions/download-artifact(?:@|\s|$)", re.IGNORECASE)
INHERIT_SECRETS_PATTERN = re.compile(r"^\s*secrets\s*:\s*['\"]?inherit['\"]?\s*$", re.IGNORECASE)
WAIVER_PATTERN = re.compile(
    r"#\s*workflow-security:\s*waive\s+(?P<rule>WFS\d{3})\s+"
    r"until=(?P<expiry>\d{4}-\d{2}-\d{2})\s+reason=(?P<reason>.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WorkflowFinding:
    rule: str
    workflow_path: Path
    line_number: int
    message: str


def _split_yaml_comment(line: str) -> tuple[str, str | None]:
    quote: str | None = None
    escaped = False
    for position, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if quote is None and character in {"'", '"'}:
            quote = character
            continue
        if quote == character:
            quote = None
            continue
        if quote is None and character == "#" and (position == 0 or line[position - 1].isspace()):
            return line[:position], line[position + 1 :]
    return line, None


def _remove_yaml_comment(line: str) -> str:
    return _split_yaml_comment(line)[0]


def _has_valid_waiver(line: str, rule: str, current_date: date) -> bool:
    _, comment = _split_yaml_comment(line)
    if comment is None:
        return False
    waiver_match = WAIVER_PATTERN.search("#" + comment)
    if waiver_match is None or waiver_match.group("rule").upper() != rule:
        return False

    reason = waiver_match.group("reason").strip().strip("\"'")
    if len(reason) < MINIMUM_WAIVER_REASON_LENGTH:
        return False

    try:
        expiration_date = date.fromisoformat(waiver_match.group("expiry"))
    except ValueError:
        return False
    return expiration_date >= current_date


def _add_finding(
    findings: list[WorkflowFinding],
    workflow_path: Path,
    rule: str,
    line_number: int,
    message: str,
    source_line: str,
    current_date: date,
) -> None:
    if _has_valid_waiver(source_line, rule, current_date):
        return
    findings.append(WorkflowFinding(rule, workflow_path, line_number, message))


def _check_permissions(
    lines: list[str], workflow_path: Path, current_date: date, findings: list[WorkflowFinding]
) -> None:
    top_level_permissions_found = False
    active_permissions_indent: int | None = None

    for line_number, source_line in enumerate(lines, start=1):
        uncommented_line = _remove_yaml_comment(source_line)
        if not uncommented_line.strip():
            continue

        indentation = len(uncommented_line) - len(uncommented_line.lstrip(" "))
        if active_permissions_indent is not None and indentation <= active_permissions_indent:
            active_permissions_indent = None

        permission_match = PERMISSIONS_LINE_PATTERN.match(uncommented_line)
        permission_block_line = permission_match is not None
        if permission_match is not None:
            active_permissions_indent = indentation
            if indentation == 0:
                top_level_permissions_found = True
            permission_value = permission_match.group("value")
        elif active_permissions_indent is not None and indentation > active_permissions_indent:
            permission_value = uncommented_line.strip()
        else:
            permission_value = ""

        if permission_block_line or permission_value:
            if OVERBROAD_PERMISSION_PATTERN.search(permission_value):
                _add_finding(
                    findings,
                    workflow_path,
                    OVERBROAD_WORKFLOW_PERMISSIONS,
                    line_number,
                    "Workflow permission grants write access or all-scope access; keep token permissions minimal or add a time-limited waiver.",
                    source_line,
                    current_date,
                )

    if not top_level_permissions_found:
        first_line = lines[0] if lines else ""
        _add_finding(
            findings,
            workflow_path,
            MISSING_WORKFLOW_PERMISSIONS,
            1,
            "Workflow does not declare top-level permissions; set only the token scopes the workflow needs, or use an empty permission map.",
            first_line,
            current_date,
        )


def _find_dangerous_trigger(uncommented_line: str) -> str | None:
    trigger_match = DANGEROUS_TRIGGER_PATTERN.match(uncommented_line)
    if trigger_match is not None:
        return trigger_match.group("trigger").lower()

    inline_trigger_match = INLINE_TRIGGER_PATTERN.match(uncommented_line.strip())
    if inline_trigger_match is None:
        return None

    trigger_value = inline_trigger_match.group("value")
    trigger_names = {name.lower() for name in re.findall(r"\b(pull_request_target|workflow_run)\b", trigger_value, re.I)}
    if "pull_request_target" in trigger_names:
        return "pull_request_target"
    if "workflow_run" in trigger_names:
        return "workflow_run"
    return None


def _check_triggers_and_artifacts(
    lines: list[str], workflow_path: Path, current_date: date, findings: list[WorkflowFinding]
) -> None:
    workflow_run_line: int | None = None

    for line_number, source_line in enumerate(lines, start=1):
        uncommented_line = _remove_yaml_comment(source_line)
        trigger_name = _find_dangerous_trigger(uncommented_line)
        if trigger_name is None:
            continue
        if trigger_name == "workflow_run":
            workflow_run_line = line_number
            message = "workflow_run executes with repository permissions; verify the upstream run and its data before processing it."
        else:
            message = "pull_request_target executes in the base repository context; reject it unless a reviewed exception explains its trust boundary."
        _add_finding(
            findings,
            workflow_path,
            DANGEROUS_WORKFLOW_TRIGGER,
            line_number,
            message,
            source_line,
            current_date,
        )

    if workflow_run_line is None:
        return

    for line_number, source_line in enumerate(lines, start=1):
        if DOWNLOAD_ARTIFACT_PATTERN.search(_remove_yaml_comment(source_line)):
            _add_finding(
                findings,
                workflow_path,
                UNTRUSTED_WORKFLOW_ARTIFACT,
                line_number,
                "A workflow_run job downloads an artifact; validate artifact provenance and contents before using the data.",
                source_line,
                current_date,
            )


def _iter_run_script_lines(lines: list[str]) -> Iterator[tuple[int, str, str]]:
    for line_index, source_line in enumerate(lines):
        uncommented_line = _remove_yaml_comment(source_line)
        run_match = RUN_LINE_PATTERN.match(uncommented_line)
        if run_match is None:
            continue

        run_indentation = len(run_match.group("indent"))
        run_value = run_match.group("value").strip()
        if run_value and BLOCK_SCALAR_PATTERN.fullmatch(run_value) is None:
            yield line_index + 1, run_match.group("value"), source_line
            continue

        continuation_index = line_index + 1
        while continuation_index < len(lines):
            continuation_line = lines[continuation_index]
            if continuation_line.strip():
                continuation_indentation = len(continuation_line) - len(continuation_line.lstrip(" "))
                if continuation_indentation <= run_indentation:
                    break
                yield continuation_index + 1, continuation_line, continuation_line
            continuation_index += 1


def _check_shell_interpolation(
    lines: list[str], workflow_path: Path, current_date: date, findings: list[WorkflowFinding]
) -> None:
    for line_number, script_text, source_line in _iter_run_script_lines(lines):
        if UNTRUSTED_SHELL_EXPRESSION_PATTERN.search(script_text):
            _add_finding(
                findings,
                workflow_path,
                UNSAFE_SHELL_INTERPOLATION,
                line_number,
                "A run script interpolates event, input, or step output data directly; pass untrusted values through a validated environment variable instead.",
                source_line,
                current_date,
            )
        if SECRET_SHELL_EXPRESSION_PATTERN.search(script_text):
            _add_finding(
                findings,
                workflow_path,
                UNSAFE_SHELL_INTERPOLATION,
                line_number,
                "A run script interpolates a secret into generated shell source; pass the secret through the step environment instead.",
                source_line,
                current_date,
            )


def _check_checkout_credentials(
    lines: list[str], workflow_path: Path, current_date: date, findings: list[WorkflowFinding]
) -> None:
    for line_index, source_line in enumerate(lines):
        uncommented_line = _remove_yaml_comment(source_line)
        checkout_match = CHECKOUT_STEP_PATTERN.match(uncommented_line)
        if checkout_match is None:
            continue

        step_indentation = len(checkout_match.group("indent"))
        checkout_line_number = line_index + 1
        credential_value: str | None = None
        credential_line = source_line
        credential_line_number = checkout_line_number
        following_index = line_index + 1
        while following_index < len(lines):
            step_line = lines[following_index]
            uncommented_step_line = _remove_yaml_comment(step_line)
            if uncommented_step_line.strip():
                step_line_indentation = len(uncommented_step_line) - len(
                    uncommented_step_line.lstrip(" ")
                )
                if step_line_indentation <= step_indentation:
                    break
            credential_match = PERSIST_CHECKOUT_CREDENTIALS_PATTERN.match(uncommented_step_line)
            if credential_match is not None:
                credential_value = credential_match.group("value").strip("\"'").lower()
                credential_line = step_line
                credential_line_number = following_index + 1
            following_index += 1

        if credential_value != "false":
            finding_line_number = credential_line_number if credential_value == "true" else checkout_line_number
            _add_finding(
                findings,
                workflow_path,
                PERSISTED_CHECKOUT_CREDENTIALS,
                finding_line_number,
                "actions/checkout keeps its token in local Git configuration unless persist-credentials is false.",
                credential_line,
                current_date,
            )


def _check_reusable_workflow_secrets(
    lines: list[str], workflow_path: Path, current_date: date, findings: list[WorkflowFinding]
) -> None:
    for line_number, source_line in enumerate(lines, start=1):
        if INHERIT_SECRETS_PATTERN.match(_remove_yaml_comment(source_line)):
            _add_finding(
                findings,
                workflow_path,
                INHERITED_REUSABLE_WORKFLOW_SECRETS,
                line_number,
                "Reusable workflows should receive only the named secrets they need instead of inheriting every caller secret.",
                source_line,
                current_date,
            )


def scan_workflow_text(
    workflow_text: str,
    workflow_path: Path,
    current_date: date | None = None,
) -> list[WorkflowFinding]:
    validation_date = current_date or datetime.now(timezone.utc).date()
    source_lines = workflow_text.splitlines()
    findings: list[WorkflowFinding] = []

    _check_permissions(source_lines, workflow_path, validation_date, findings)
    _check_triggers_and_artifacts(source_lines, workflow_path, validation_date, findings)
    _check_shell_interpolation(source_lines, workflow_path, validation_date, findings)
    _check_checkout_credentials(source_lines, workflow_path, validation_date, findings)
    _check_reusable_workflow_secrets(source_lines, workflow_path, validation_date, findings)
    return findings


def scan_workflow_path(workflow_path: Path, current_date: date | None = None) -> list[WorkflowFinding]:
    if not workflow_path.exists():
        raise FileNotFoundError(f"workflow path does not exist: {workflow_path}")
    if workflow_path.is_file():
        if workflow_path.suffix.lower() not in WORKFLOW_SUFFIXES:
            return []
        return scan_workflow_text(workflow_path.read_text(encoding="utf-8"), workflow_path, current_date)

    workflow_files = sorted(
        path for path in workflow_path.rglob("*") if path.is_file() and path.suffix.lower() in WORKFLOW_SUFFIXES
    )
    if not workflow_files:
        raise FileNotFoundError(f"no .yml or .yaml workflow files found under: {workflow_path}")
    return [
        finding
        for workflow_file in workflow_files
        for finding in scan_workflow_text(
            workflow_file.read_text(encoding="utf-8"), workflow_file, current_date
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check GitHub Actions workflows for broad permissions and unsafe trust boundaries"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path(".github/workflows")],
        help="Workflow files or directories to inspect",
    )
    arguments = parser.parse_args()

    try:
        findings = [finding for path in arguments.paths for finding in scan_workflow_path(path)]
    except (OSError, UnicodeError) as error:
        print(f"WORKFLOW SECURITY CHECK ERROR: {error}", file=sys.stderr)
        return 2

    for finding in findings:
        print(f"E {finding.rule} {finding.workflow_path}:{finding.line_number} {finding.message}")
    if findings:
        print(f"WORKFLOW SECURITY CHECK FAILED findings={len(findings)}", file=sys.stderr)
        return 1
    print("WORKFLOW SECURITY CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
