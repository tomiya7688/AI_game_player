import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import trust_gate, trust_inventory

REPO_ROOT = Path(__file__).resolve().parents[1]
FLOORS = {"pip": "26.2.2", "certifi": "2026.10.19", "uv": "0.13.4", "sigstore": "4.5.1"}
CLEAN_SHA = "a" * 64
INCIDENT_SHA = next(iter(trust_inventory.INCIDENT_ROOT_FINGERPRINTS))


def _match(sha256: str, source: str = "bundle") -> trust_inventory.TargetMatch:
    names = (
        ["emSign ECC Root CA - G3"]
        if sha256 == INCIDENT_SHA
        else ["emSign Root CA - G1"]
    )
    return trust_inventory.TargetMatch(
        source,
        0,
        sha256,
        ["emSign"],
        names,
        incident=trust_inventory.classify_incident(sha256, names),
    )


def _bundle(*matches: trust_inventory.TargetMatch, status: str = "inspected"):
    return trust_inventory.BundleResult(
        "/fixture/cacert.pem", status, "b" * 64, 120, list(matches)
    )


def _fresh_report(**changes) -> trust_inventory.EnvironmentReport:
    """An interpreter shaped like the CI runner after the trust floor (pip 26.2.2)."""
    details = {
        "python_version": "3.14.9",
        "packages": {"pip": "26.2.2", "certifi": None, "uv": None, "sigstore": None},
        "cli_paths": {"uv": None, "sigstore": None},
        "pip_vendored_certifi": {
            "version": "2026.10.19",
            "where": "/fixture/cacert.pem",
        },
        "pip_vendored_truststore": True,
        "pip_config": {"files": [], "settings": {}},
        "environment": {},
    }
    details.update(changes.pop("details", {}))
    details["pip_trust_source"] = trust_inventory.pip_trust_source(details)
    report = trust_inventory.EnvironmentReport(
        "fixture-python", "inspected", details, pip_bundle=_bundle(_match(CLEAN_SHA))
    )
    for name, value in changes.items():
        setattr(report, name, value)
    return report


class FloorFileTest(unittest.TestCase):
    def test_repository_floors_match_the_482_minimums(self):
        floors = trust_gate.load_floors(trust_gate.DEFAULT_CONSTRAINTS)
        self.assertEqual(floors, trust_inventory.MINIMUM_VERSIONS)

    def test_constraints_file_is_ascii_for_pip_locale_decoding(self):
        trust_gate.DEFAULT_CONSTRAINTS.read_bytes().decode("ascii")

    def test_rejects_missing_unparsable_and_duplicate_floors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.txt"
            cases = {
                "pip>=26.2.2\n": "missing floors",
                "pip==26.2.2\n": "not a 'name>=version' floor",
                "pip>=1\npip>=2\n": "duplicate floor",
            }
            for text, message in cases.items():
                with self.subTest(text=text):
                    path.write_text(text, encoding="ascii")
                    with self.assertRaisesRegex(trust_gate.ConstraintsError, message):
                        trust_gate.load_floors(path)
            with self.assertRaisesRegex(trust_gate.ConstraintsError, "cannot read"):
                trust_gate.load_floors(Path(directory) / "absent.txt")


class EvaluateTest(unittest.TestCase):
    def test_fresh_environment_passes(self):
        self.assertEqual(trust_gate.evaluate(_fresh_report(), FLOORS), [])

    def test_old_environment_fails_on_versions_and_incident_root(self):
        old = _fresh_report(
            details={
                "packages": {
                    "pip": "24.0",
                    "certifi": "2026.7.22",
                    "uv": None,
                    "sigstore": None,
                },
                "pip_vendored_certifi": {
                    "version": "2024.2.2",
                    "where": "/fixture/old.pem",
                },
            },
            certifi_bundle=_bundle(_match(INCIDENT_SHA)),
            pip_bundle=_bundle(_match(INCIDENT_SHA)),
        )
        text = "\n".join(trust_gate.evaluate(old, FLOORS))
        self.assertIn("pip: outdated (24.0 < 26.2.2)", text)
        self.assertIn("pip vendored certifi: outdated (2024.2.2 < 2026.10.19)", text)
        self.assertIn("certifi: outdated (2026.7.22 < 2026.10.19)", text)
        self.assertEqual(text.count(f"incident certificate {INCIDENT_SHA}"), 2)

    def test_incident_root_in_up_to_date_bundle_still_fails(self):
        # A version at the floor is not proof the incident root is gone.
        report = _fresh_report(pip_bundle=_bundle(_match(INCIDENT_SHA)))
        self.assertEqual(len(trust_gate.evaluate(report, FLOORS)), 1)

    def test_not_inspected_sources_fail_closed(self):
        cases = {
            "probe": _fresh_report(status="not inspected: probe exited 1"),
            "pip bundle": _fresh_report(
                pip_bundle=_bundle(status="not inspected: unreadable (OSError)")
            ),
            "no pip bundle": _fresh_report(pip_bundle=None),
            "no pip": _fresh_report(details={"packages": {"pip": None}}),
            "pre-release": _fresh_report(details={"packages": {"pip": "26.3b1"}}),
            "cert override": _fresh_report(
                details={"environment": {"SSL_CERT_FILE": "/x.pem"}}
            ),
            "store listing": _fresh_report(
                details={"windows_store_error": "unexpected"}
            ),
            "unexpanded root": _fresh_report(
                details={
                    "windows_store_errors": [
                        {"store": "ROOT", "error": "not expanded: pkcs_7_asn"}
                    ]
                }
            ),
        }
        for name, report in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(trust_gate.evaluate(report, FLOORS), [])

    def test_optional_tools_absent_pass_present_must_meet_floor(self):
        present = _fresh_report(
            details={"packages": {"pip": "26.2.2", "uv": "0.13.4", "sigstore": "4.5.1"}}
        )
        self.assertEqual(trust_gate.evaluate(present, FLOORS), [])
        old = _fresh_report(
            details={"packages": {"pip": "26.2.2", "uv": "0.13.3", "sigstore": "4.5.0"}}
        )
        self.assertEqual(len(trust_gate.evaluate(old, FLOORS)), 2)
        on_path = _fresh_report(details={"cli_paths": {"uv": "/usr/bin/uv"}})
        self.assertIn("not executed", trust_gate.evaluate(on_path, FLOORS)[0])

    def test_windows_disallowed_store_is_distrust_but_root_store_fails(self):
        disallowed = _fresh_report(
            windows_store_matches=[_match(INCIDENT_SHA, "windows:Disallowed")]
        )
        self.assertEqual(trust_gate.evaluate(disallowed, FLOORS), [])
        unexpanded = _fresh_report(
            details={
                "windows_store_errors": [
                    {"store": "Disallowed", "error": "not expanded"}
                ]
            }
        )
        self.assertEqual(trust_gate.evaluate(unexpanded, FLOORS), [])
        root = _fresh_report(
            windows_store_matches=[_match(INCIDENT_SHA, "windows:ROOT")]
        )
        self.assertIn("windows:ROOT", trust_gate.evaluate(root, FLOORS)[0])

    def test_bundle_reports_fail_only_on_incident_or_uninspected(self):
        clean = trust_inventory.EnvironmentReport(
            "os",
            "inspected",
            {"bundle_path": "/x"},
            certifi_bundle=_bundle(_match(CLEAN_SHA)),
        )
        self.assertEqual(trust_gate.evaluate(clean, FLOORS), [])
        bad = trust_inventory.EnvironmentReport(
            "os",
            "inspected",
            {"bundle_path": "/x"},
            certifi_bundle=_bundle(_match(INCIDENT_SHA)),
        )
        self.assertEqual(len(trust_gate.evaluate(bad, FLOORS)), 1)


class MainTest(unittest.TestCase):
    def _run(self, report, argv=()):
        output = io.StringIO()
        with (
            patch.object(trust_inventory, "inspect_interpreter", return_value=report),
            contextlib.redirect_stdout(output),
        ):
            code = trust_gate.main(["--python", "fixture", *argv])
        return code, output.getvalue()

    def test_exit_codes_and_single_line_output(self):
        code, text = self._run(_fresh_report())
        self.assertEqual(code, 0)
        self.assertIn("TRUST GATE PASS: fixture-python", text)
        hostile = _fresh_report(pip_bundle=_bundle(_match(INCIDENT_SHA)))
        hostile.pip_bundle.path = "/x\n::error::injected"
        code, text = self._run(hostile)
        self.assertEqual(code, 1)
        self.assertFalse(any(line.startswith("::") for line in text.splitlines()))

    def test_bad_constraints_exit_2_without_inspection(self):
        error = io.StringIO()
        with (
            patch.object(trust_inventory, "inspect_interpreter") as inspect,
            contextlib.redirect_stderr(error),
        ):
            code = trust_gate.main(["--constraints", str(REPO_ROOT / "absent.txt")])
        self.assertEqual(code, 2)
        inspect.assert_not_called()


class WorkflowWiringTest(unittest.TestCase):
    """Every CI job that runs pip install must pass the trust floor first (Issue #514)."""

    def test_pip_installs_follow_the_trust_floor_action(self):
        for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
            floor_seen = False
            for number, line in enumerate(
                workflow.read_text(encoding="utf-8").splitlines(), 1
            ):
                if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
                    floor_seen = False  # a new job starts
                if "uses: ./.github/actions/python-trust-floor" in line:
                    floor_seen = True
                if (
                    re.search(r"pip install", line)
                    and "-c requirements/python-trust-constraints.txt" not in line
                ):
                    with self.subTest(workflow=workflow.name, line=number):
                        self.assertTrue(floor_seen, line.strip())

    def test_new_venv_pip_is_upgraded_and_gated_before_use(self):
        text = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        upgrade = text.index(
            'test-game-suite/bin/python" -m pip install --disable-pip-version-check --upgrade'
        )
        gate = text.index(
            'trust_gate.py --python "$RUNNER_TEMP/test-game-suite/bin/python"'
        )
        install = text.index('test-game-suite/bin/python" -m pip install -r')
        self.assertLess(upgrade, gate)
        self.assertLess(gate, install)

    def test_bootstrap_and_build_scripts_run_the_gate_before_pip_or_pyinstaller(self):
        launch = (REPO_ROOT / "tools" / "test_games" / "launch.ps1").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            launch.index("tools/trust_gate.py"),
            launch.index("--python $gamePython install"),
        )
        self.assertIn("-c $trustConstraints", launch)
        build = (REPO_ROOT / "tools" / "build_windows_bundle.ps1").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            build.index("tools/trust_gate.py"), build.index("-m PyInstaller")
        )


if __name__ == "__main__":
    unittest.main()
