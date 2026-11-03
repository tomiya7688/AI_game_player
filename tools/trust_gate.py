"""Fail when a Python interpreter or CA bundle misses the Issue #514 trust floor.

tools/trust_inventory.py (Issue #513) records what an environment trusts and never
fails on what it finds. This gate reuses that inspection and turns it into a
pass/fail verdict for CI, build and bootstrap scripts:

- pip and pip's vendored certifi must be at or above the floors in
  requirements/python-trust-constraints.txt (the same floors as parent #482).
- A standalone certifi, uv or sigstore may be absent; when present it must meet
  its floor. A uv/sigstore command found only on PATH cannot be versioned without
  executing it, so it fails as unverified.
- No inspected CA bundle (standalone certifi, pip's vendored certifi, --bundle
  files) and no trusted Windows store (ROOT, CA) may hold a certificate that the
  inventory classifies as part of the incident (GHSA-6q48-6c7w-9gw2). A match in
  the Windows "Disallowed" store is a distrust entry and does not fail.
- Anything that could not be inspected fails (fail closed): a probe error, an
  unreadable bundle, a Windows store entry that was not expanded, or a pip trust
  source that the inventory reports as "unknown".

Passing the gate is not proof that a TLS chain is rejected: the evidence limits
of tools/trust_inventory.py (name matching, no chain building, no live
connection, Windows on-demand root download) still apply.
"""

import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Allow `python tools/trust_gate.py` from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import trust_inventory  # noqa: E402

DEFAULT_CONSTRAINTS = (
    trust_inventory.REPO_ROOT / "requirements" / "python-trust-constraints.txt"
)
REQUIRED_FLOORS = ("pip", "certifi", "uv", "sigstore")
# Windows stores whose entries are trust anchors or intermediates; Disallowed is distrust.
TRUSTED_WINDOWS_STORES = ("ROOT", "CA")
_FLOOR_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][0-9.]*)$")
# Certificate names and paths come from inspected data; keep each printed report on one line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

EXIT_CODES_HELP = """exit codes:
  0  every inspected interpreter and bundle meets the trust floor
  1  at least one interpreter or bundle failed or could not be inspected
  2  the constraints file is missing, unreadable or lacks a required floor
"""


class ConstraintsError(ValueError):
    """The constraints file cannot provide the floors the gate needs."""


def load_floors(path: Path) -> dict[str, str]:
    """Read `name>=version` floors from a pip constraints file.

    Comments and blank lines are skipped. Any other line shape is rejected so a
    pin the gate cannot interpret (==, markers, URLs) is not silently ignored.
    Raises ConstraintsError when the file cannot be read, a line is not a floor,
    or one of REQUIRED_FLOORS is missing.
    """
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise ConstraintsError(f"cannot read {path}: {type(error).__name__}") from error
    floors: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        found = _FLOOR_LINE.match(line)
        if not found:
            raise ConstraintsError(f"{path}:{number}: not a 'name>=version' floor")
        name = found.group(1).lower().replace("_", "-")
        if name in floors:
            raise ConstraintsError(f"{path}:{number}: duplicate floor for {name}")
        floors[name] = found.group(2)
    missing = [name for name in REQUIRED_FLOORS if name not in floors]
    if missing:
        raise ConstraintsError(f"{path}: missing floors for {', '.join(missing)}")
    return floors


def _bundle_violations(
    what: str, bundle: trust_inventory.BundleResult | None
) -> list[str]:
    if bundle is None:
        return [f"{what}: CA bundle was not inspected"]
    if bundle.status != "inspected":
        return [f"{what}: {bundle.path}: {bundle.status}"]
    return [
        f"{what}: {bundle.path}: incident certificate {match.sha256} "
        f"({', '.join(match.common_names) or 'no commonName'}; {match.incident})"
        for match in bundle.matches or []
        if match.incident != trust_inventory.NO_INCIDENT_MATCH
    ]


def _require(name: str, installed: object, floors: dict[str, str]) -> list[str]:
    text = installed if isinstance(installed, str) else None
    verdict = trust_inventory.check_version(name, text, floors[name])
    return [] if verdict.startswith("ok") else [f"{name}: {verdict}"]


def _optional(
    name: str, installed: object, path: object, floors: dict[str, str]
) -> list[str]:
    if installed is None and path:
        return [
            f"{name}: command on PATH at {path} has no package version (not executed)"
        ]
    if installed is None:
        return []
    return _require(name, installed, floors)


def _windows_violations(report: trust_inventory.EnvironmentReport) -> list[str]:
    details = report.details
    violations = []
    if "windows_store_error" in details:
        violations.append(f"windows store: {details['windows_store_error']}")
    for entry in details.get("windows_store_errors") or []:
        entry = entry if isinstance(entry, dict) else {}
        store = entry.get("store", "?")
        if store in TRUSTED_WINDOWS_STORES:
            violations.append(
                f"windows:{store}: entry not inspected ({entry.get('error')})"
            )
    for match in report.windows_store_matches or []:
        store = match.source.split(":", 1)[-1]
        if (
            store in TRUSTED_WINDOWS_STORES
            and match.incident != trust_inventory.NO_INCIDENT_MATCH
        ):
            violations.append(
                f"{match.source}: incident certificate {match.sha256} ({match.incident})"
            )
    return violations


def evaluate(
    report: trust_inventory.EnvironmentReport, floors: dict[str, str]
) -> list[str]:
    """Return every reason the inspected environment misses the trust floor (empty = pass).

    A --bundle report only carries `certifi_bundle`; an interpreter report carries
    the probe details. An interpreter without pip fails, because the gate is meant
    for the interpreter whose pip downloads packages.
    """
    if report.status != "inspected":
        return [f"not inspected: {report.status}"]
    details = report.details
    if "bundle_path" in details:
        return _bundle_violations("bundle", report.certifi_bundle)
    packages = (
        details.get("packages") if isinstance(details.get("packages"), dict) else {}
    )
    cli_paths = (
        details.get("cli_paths") if isinstance(details.get("cli_paths"), dict) else {}
    )
    violations = _require("pip", packages.get("pip"), floors)
    vendored = details.get("pip_vendored_certifi")
    vendored = vendored if isinstance(vendored, dict) else {}
    violations += [
        f"pip vendored {reason}"
        for reason in _require("certifi", vendored.get("version"), floors)
    ]
    violations += _bundle_violations("pip vendored certifi", report.pip_bundle)
    if packages.get("certifi") is not None:
        violations += _require("certifi", packages.get("certifi"), floors)
        violations += _bundle_violations("certifi", report.certifi_bundle)
    for tool in ("uv", "sigstore"):
        violations += _optional(tool, packages.get(tool), cli_paths.get(tool), floors)
    source = str(details.get("pip_trust_source", "not recorded"))
    if source.startswith(("unknown", "pip not installed", "not recorded")):
        violations.append(f"pip trust source: {source}")
    violations += _windows_violations(report)
    return violations


def _evidence(report: trust_inventory.EnvironmentReport) -> str:
    details = report.details
    if "bundle_path" in details:
        bundle = report.certifi_bundle
        return f"bundle sha256={bundle.sha256 if bundle else None}"
    packages = (
        details.get("packages") if isinstance(details.get("packages"), dict) else {}
    )
    vendored = details.get("pip_vendored_certifi")
    vendored = vendored if isinstance(vendored, dict) else {}
    pip_bundle = report.pip_bundle
    return (
        f"python={details.get('python_version')} pip={packages.get('pip')} "
        f"pip-certifi={vendored.get('version')} "
        f"pip-certifi-sha256={pip_bundle.sha256 if pip_bundle else None} "
        f"certifi={packages.get('certifi')} trust={details.get('pip_trust_source')}"
    )


def _one_line(text: str) -> str:
    return _CONTROL.sub("?", text)


def main(argv: list[str] | None = None) -> int:
    """Inspect the requested interpreters and bundles and return EXIT_CODES_HELP's code."""
    parser = argparse.ArgumentParser(
        description="Python trust floor gate (Issue #514).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--python",
        action="append",
        default=[],
        help="interpreter to check (repeatable; default: this interpreter)",
    )
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        type=Path,
        help="CA bundle file to check",
    )
    parser.add_argument(
        "--constraints",
        type=Path,
        default=DEFAULT_CONSTRAINTS,
        help="pip constraints file holding the floors",
    )
    args = parser.parse_args(argv)
    try:
        floors = load_floors(args.constraints)
    except ConstraintsError as error:
        print(f"TRUST GATE ERROR: {error}", file=sys.stderr)
        return 2
    interpreters = args.python or [sys.executable]
    reports = [trust_inventory.inspect_interpreter(python) for python in interpreters]
    reports.extend(trust_inventory.inspect_bundle(path) for path in args.bundle)
    failed = False
    for report in reports:
        violations = evaluate(report, floors)
        verdict = "FAIL" if violations else "PASS"
        print(_one_line(f"TRUST GATE {verdict}: {report.label}: {_evidence(report)}"))
        for violation in violations:
            print(_one_line(f"  - {violation}"))
        failed = failed or bool(violations)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
