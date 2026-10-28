import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import trust_inventory


def _fake_der(
    common_name: str, organisation: str = "Example Org", tag: int = 0x13
) -> bytes:
    """Build bytes shaped like the name part of a certificate.

    The inventory matches organisation names in the raw DER and decodes commonName
    values, so a full certificate is not needed for these fixtures.
    """
    cn = common_name.encode("utf-16-be" if tag == 0x1E else "utf-8")
    org = organisation.encode()
    return (
        b"\x30\x82\x01\x00"
        + b"\x06\x03\x55\x04\x0a\x0c"
        + bytes([len(org)])
        + org
        + b"\x06\x03\x55\x04\x03"
        + bytes([tag, len(cn)])
        + cn
    )


def _pem(der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(lines)
        + "\n-----END CERTIFICATE-----\n"
    )


def _probe_printing(payload: dict) -> str:
    """A probe replacement that prints extra noise and then the marked JSON line."""
    line = trust_inventory.PROBE_MARKER + json.dumps(payload)
    return f"print('noise from sitecustomize')\nprint({line!r})\n"


class VersionCheckTest(unittest.TestCase):
    def test_classifies_against_the_482_minimums(self):
        cases = {
            ("pip", "26.2.2"): "ok (26.2.2 >= 26.2.2)",
            ("pip", "26.3"): "ok (26.3 >= 26.2.2)",
            ("pip", "24.0"): "outdated (24.0 < 26.2.2)",
            ("certifi", "2026.10.1"): "outdated (2026.10.1 < 2026.10.19)",
            ("certifi", "2026.10.19"): "ok (2026.10.19 >= 2026.10.19)",
            ("certifi", "2026.10.2"): "outdated (2026.10.2 < 2026.10.19)",
            ("uv", "0.13.4.0"): "ok (0.13.4.0 >= 0.13.4)",
            ("uv", "uv 0.13.4 (abc 2026-10-01)"): "ok (0.13.4 >= 0.13.4)",
            ("sigstore", "4.5.1.post1"): "ok (4.5.1 >= 4.5.1)",
            ("sigstore", None): "not installed",
        }
        for (name, installed), expected in cases.items():
            with self.subTest(name=name, installed=installed):
                self.assertEqual(
                    trust_inventory.check_version(name, installed), expected
                )

    def test_pre_release_and_unparsable_versions_are_unknown(self):
        self.assertTrue(
            trust_inventory.check_version("pip", "26.3rc1").startswith("unknown")
        )
        self.assertTrue(
            trust_inventory.check_version("sigstore", "error: Timeout").startswith(
                "unknown"
            )
        )


class BundleScanTest(unittest.TestCase):
    def _bundle(self, directory: str, content: bytes) -> Path:
        path = Path(directory) / "cacert.pem"
        path.write_bytes(content)
        return path

    def test_reports_target_certificates_with_fingerprint_and_common_name(self):
        target = _fake_der("emSign Root CA - G1", "eMudhra Technologies Limited")
        other = _fake_der("Example Root CA")
        with tempfile.TemporaryDirectory() as directory:
            content = ("# comment\n" + _pem(other) + _pem(target)).encode()
            bundle = trust_inventory.inspect_bundle_file(
                self._bundle(directory, content)
            )
        self.assertEqual(bundle.status, "inspected")
        self.assertEqual(bundle.certificate_count, 2)
        self.assertEqual(bundle.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(len(bundle.matches), 1)
        match = bundle.matches[0]
        self.assertEqual(
            (match.index, match.sha256), (1, hashlib.sha256(target).hexdigest())
        )
        self.assertEqual(match.common_names, ["emSign Root CA - G1"])
        self.assertEqual(match.names, ["eMudhra", "emSign"])

    def test_utf16_names_match_without_case(self):
        der = b"\x06\x03\x55\x04\x0a\x1e\x0c" + "EMSIGN".encode("utf-16-be")
        self.assertEqual(
            trust_inventory.scan_der_certificates("t", [der])[0].names, ["emSign"]
        )

    def test_empty_certificate_block_is_invalid_and_relative_path_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty-block.pem"
            path.write_bytes(
                b"-----BEGIN CERTIFICATE-----\n\n-----END CERTIFICATE-----\n"
            )
            self.assertEqual(
                trust_inventory.inspect_bundle_file(path).status,
                "not inspected: invalid PEM/base64",
            )
        relative = trust_inventory.inspect_bundle_file(Path("cacert.pem"))
        self.assertEqual(relative.status, "not inspected: not an absolute path")

    def test_matches_case_insensitive_and_utf16_names(self):
        upper = _fake_der("EMSIGN ROOT", "EMUDHRA LTD")
        bmp = _fake_der("emSign BMP Root", "Example", tag=0x1E)
        matches = trust_inventory.scan_der_certificates("test", [upper, bmp])
        self.assertEqual(
            [m.names for m in matches], [["eMudhra", "emSign"], ["emSign"]]
        )
        self.assertEqual(matches[1].common_names, ["emSign BMP Root"])

    def test_truncated_common_name_is_not_reported(self):
        der = b"\x06\x03\x55\x04\x03\x13\x20emSign"
        self.assertEqual(trust_inventory.common_names(der), [])
        matches = trust_inventory.scan_der_certificates("t", [der])
        self.assertEqual([m.names for m in matches], [["emSign"]])

    def test_directory_is_not_read_as_a_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            result = trust_inventory.inspect_bundle_file(Path(directory))
        self.assertEqual(result.status, "not inspected: not a regular file")

    def test_clean_empty_invalid_and_unreadable_bundles_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = trust_inventory.inspect_bundle_file(
                self._bundle(directory, _pem(_fake_der("Example")).encode())
            )
            empty = trust_inventory.inspect_bundle_file(
                self._bundle(directory, b"# no certificates\n")
            )
            broken = b"-----BEGIN CERTIFICATE-----\n@@not base64@@\n-----END CERTIFICATE-----\n"
            invalid = trust_inventory.inspect_bundle_file(
                self._bundle(directory, broken)
            )
            missing = trust_inventory.inspect_bundle_file(
                Path(directory) / "absent.pem"
            )
        self.assertEqual((clean.status, clean.matches), ("inspected", []))
        self.assertEqual(
            (empty.status, empty.matches),
            ("not inspected: no PEM certificates found", None),
        )
        self.assertEqual(
            (invalid.status, invalid.matches),
            ("not inspected: invalid PEM/base64", None),
        )
        self.assertEqual(
            missing.status, "not inspected: unreadable (FileNotFoundError)"
        )
        self.assertIsNone(missing.matches)


class InterpreterProbeTest(unittest.TestCase):
    def test_inspects_the_running_interpreter_without_executing_tools(self):
        with patch("subprocess.run", wraps=__import__("subprocess").run) as run:
            report = trust_inventory.inspect_interpreter(sys.executable, "current")
        self.assertEqual(report.status, "inspected", report.details)
        self.assertEqual(run.call_count, 1)
        for key in (
            "python_version",
            "openssl_version",
            "packages",
            "module_files",
            "environment",
            "cli_paths",
        ):
            self.assertIn(key, report.details)
        self.assertEqual(
            set(report.details["environment"]),
            set(trust_inventory.CERTIFICATE_ENVIRONMENT_VARIABLES),
        )
        self.assertEqual(
            set(report.version_checks), {"pip", "certifi", "uv", "sigstore"}
        )

    def test_probe_runs_without_isolation_from_an_empty_temporary_directory(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            seen["cwd_entries"] = os.listdir(kwargs["cwd"])
            return subprocess.CompletedProcess(command, 1, "", "stopped by test")

        with patch.object(trust_inventory.subprocess, "run", fake_run):
            trust_inventory.inspect_interpreter("python-under-test")
        self.assertEqual(seen["command"][:2], ["python-under-test", "-c"])
        self.assertNotIn("-I", seen["command"])
        self.assertNotIn("-s", seen["command"])
        self.assertEqual(seen["cwd_entries"], [])
        self.assertNotIn("PYTHONNOUSERSITE", seen["kwargs"]["env"])
        self.assertEqual(seen["kwargs"]["encoding"], "utf-8")

    def test_probe_ignores_modules_in_the_callers_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "platform.py").write_text(
                "raise SystemExit('shadowed')\n", encoding="utf-8"
            )
            previous = os.getcwd()
            os.chdir(directory)
            try:
                report = trust_inventory.inspect_interpreter(sys.executable)
            finally:
                os.chdir(previous)
        self.assertEqual(report.status, "inspected", report.details)

    def test_records_certificate_environment_variables(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": "custom-bundle.pem"}):
            report = trust_inventory.inspect_interpreter(sys.executable)
        self.assertEqual(
            report.details["environment"]["SSL_CERT_FILE"], "custom-bundle.pem"
        )

    def test_missing_interpreter_timeout_and_bad_output_are_reported(self):
        missing = trust_inventory.inspect_interpreter(
            str(Path(tempfile.gettempdir()) / "no-such-python.exe")
        )
        self.assertTrue(missing.status.startswith("not inspected"), missing.status)
        cases = (
            ("raise SystemExit(3)", "probe exited 3"),
            ("print('hello')", "no JSON result"),
            ("print('TRUST_INVENTORY_JSON:[1, 2]')", "no JSON result"),
        )
        for script, expected in cases:
            with (
                self.subTest(expected),
                patch.object(trust_inventory, "PROBE_SCRIPT", script),
            ):
                self.assertIn(
                    expected, trust_inventory.inspect_interpreter(sys.executable).status
                )
        with (
            patch.object(trust_inventory, "PROBE_SCRIPT", "import time; time.sleep(5)"),
            patch.object(trust_inventory, "PROBE_TIMEOUT_SECONDS", 0.5),
        ):
            self.assertEqual(
                trust_inventory.inspect_interpreter(sys.executable).status,
                "not inspected: TimeoutExpired",
            )

    def test_classifies_tooling_and_scans_pip_bundle_and_windows_stores(self):
        target = _fake_der("emSign Root CA - C1", "eMudhra Technologies Limited")
        with tempfile.TemporaryDirectory() as directory:
            pip_bundle = Path(directory) / "pip-cacert.pem"
            pip_bundle.write_text(
                _pem(_fake_der("Example")) + _pem(target), encoding="ascii"
            )
            payload = {
                "packages": {
                    "pip": "23.0",
                    "certifi": None,
                    "uv": None,
                    "sigstore": "4.0.0",
                },
                "cli_paths": {"uv": "C:/bin/uv.exe", "sigstore": None},
                "certifi_where": None,
                "pip_vendored_certifi": {
                    "version": "2026.10.19",
                    "where": str(pip_bundle),
                },
                "windows_store": [
                    {
                        "store": "ROOT",
                        "der_b64": base64.b64encode(target).decode(),
                        "trust": True,
                    },
                    {
                        "store": "CA",
                        "der_b64": base64.b64encode(_fake_der("Other")).decode(),
                        "trust": True,
                    },
                    {"store": "Disallowed", "error": "OSError"},
                    {"store": "CA", "error": "not expanded: pkcs_7_asn"},
                    {"store": "ROOT", "der_b64": "@@bad@@"},
                ],
            }
            with patch.object(
                trust_inventory, "PROBE_SCRIPT", _probe_printing(payload)
            ):
                report = trust_inventory.inspect_interpreter(sys.executable)
        self.assertEqual(report.status, "inspected", report.details)
        self.assertEqual(report.version_checks["pip"], "outdated (23.0 < 26.2.2)")
        self.assertEqual(report.version_checks["certifi"], "not installed")
        self.assertTrue(
            report.version_checks["uv"].startswith("unknown (command on PATH")
        )
        self.assertEqual(report.version_checks["sigstore"], "outdated (4.0.0 < 4.5.1)")
        self.assertIsNone(report.certifi_bundle)
        self.assertEqual(
            [m.common_names for m in report.pip_bundle.matches],
            [["emSign Root CA - C1"]],
        )
        self.assertEqual(
            [m.source for m in report.windows_store_matches], ["windows:ROOT"]
        )
        self.assertIs(report.windows_store_matches[0].trust, True)
        self.assertEqual(
            report.details["windows_store_counts"],
            {"ROOT": 1, "CA": 1, "Disallowed": 0},
        )
        self.assertEqual(len(report.details["windows_store_errors"]), 3)
        self.assertIn(
            "not expanded: pkcs_7_asn", str(report.details["windows_store_errors"])
        )


class RepositoryScanTest(unittest.TestCase):
    def test_lists_tooling_lines_masks_credentials_and_reports_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yaml").write_text(
                "steps:\n  - uses: actions/setup-python@v5\n  - run: echo hi\n",
                encoding="utf-8",
            )
            (root / "requirements.txt").write_text(
                "--index-url https://user:hunter2@pypi.example/simple\ncertifi==2024.2.2\nrequests\n",
                encoding="utf-8",
            )
            (root / "setup.sh").write_text(
                "pip install x  # token=abc123\n" + "pip " + "x" * 300 + "\n",
                encoding="utf-8",
            )
            findings, unreadable = trust_inventory.scan_repository(root)
        texts = {(f["path"], f["line"]): f["text"] for f in findings}
        self.assertEqual(
            texts[(".github/workflows/ci.yaml", 2)], "- uses: actions/setup-python@v5"
        )
        self.assertEqual(texts[("requirements.txt", 2)], "certifi==2024.2.2")
        self.assertNotIn(("requirements.txt", 1), texts)
        self.assertEqual(texts[("setup.sh", 1)], "pip install x  # token=***")
        self.assertTrue(texts[("setup.sh", 2)].endswith("..."))
        self.assertLessEqual(
            len(texts[("setup.sh", 2)]), trust_inventory.MAX_LINE_LENGTH + 3
        )
        self.assertEqual(unreadable, [])

    def test_masks_prefixed_keys_options_bearer_and_at_signs(self):
        cases = {
            "env: GITHUB_TOKEN=ghp_abc pip": "env: GITHUB_TOKEN=*** pip",
            'TWINE_PASSWORD: "a b c" pip': "TWINE_PASSWORD: *** pip",
            "pip install --password hunter2 x": "pip install --password *** x",
            "curl -H 'Authorization: Bearer abc.def' pip": "curl -H 'Authorization: Bearer ***' pip",
            "pip install --index-url https://u:p@ss@host/s": "pip install --index-url https://***@host/s",
        }
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(trust_inventory._redact(raw), expected)

    def test_masks_credentials_in_urls(self):
        self.assertEqual(
            trust_inventory._redact(
                "pip install --index-url https://user:pw@host/simple certifi"
            ),
            "pip install --index-url https://***@host/simple certifi",
        )


class MainTest(unittest.TestCase):
    def test_exit_codes_and_report_files(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            missing = str(out / "no-such-python.exe")
            code = trust_inventory.main(
                [
                    "--python",
                    missing,
                    "--json",
                    str(out / "a" / "r.json"),
                    "--markdown",
                    str(out / "a" / "r.md"),
                ]
            )
            payload = json.loads((out / "a" / "r.json").read_text(encoding="utf-8"))
            markdown = (out / "a" / "r.md").read_text(encoding="utf-8")
            self.assertEqual(code, 1)
            self.assertTrue(
                payload["environments"][0]["status"].startswith("not inspected")
            )
            self.assertIn("Repository references to Python tooling", markdown)
            bundle = out / "ok.pem"
            bundle.write_text(
                _pem(_fake_der("emSign Root CA - G1", "eMudhra")), encoding="ascii"
            )
            self.assertEqual(
                trust_inventory.main(
                    [
                        "--python",
                        sys.executable,
                        "--bundle",
                        str(bundle),
                        "--markdown",
                        str(out / "ok.md"),
                    ]
                ),
                0,
            )
            blocked = out / "file-not-dir"
            blocked.write_text("x", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(
                    trust_inventory.main(
                        ["--bundle", str(bundle), "--json", str(blocked / "r.json")]
                    ),
                    2,
                )
            self.assertIn("cannot write report", err.getvalue())

    def test_bundle_only_run_does_not_inspect_an_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "b.pem"
            bundle.write_text(
                _pem(_fake_der("emSign Root CA - G1", "eMudhra")), encoding="ascii"
            )
            output = Path(directory) / "o.md"
            with patch.object(trust_inventory, "inspect_interpreter") as inspect:
                code = trust_inventory.main(
                    ["--bundle", str(bundle), "--markdown", str(output)]
                )
            text = output.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        inspect.assert_not_called()
        self.assertIn("CN: emSign Root CA - G1; matched: eMudhra, emSign", text)

    def test_unexpected_trust_values_are_dropped(self):
        self.assertIs(trust_inventory._trust_value(True), True)
        self.assertEqual(
            trust_inventory._trust_value(["1.3.6.1.5.5.7.3.1"]), ["1.3.6.1.5.5.7.3.1"]
        )
        self.assertIsNone(trust_inventory._trust_value({"x": 1}))
        self.assertIsNone(trust_inventory._trust_value([1, 2]))

    def test_help_documents_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            trust_inventory.main(["--help"])
        self.assertIn("this is an inventory, not a gate", out.getvalue())

    def test_json_report_masks_credentials(self):
        report = trust_inventory.EnvironmentReport(
            "label",
            "not inspected: probe exited 1",
            {
                "stderr": "GITHUB_TOKEN=secret123",
                "environment": {
                    "PIP_CERT": "https://u:pw@host/ca.pem",
                    "SSL_CERT_FILE": None,
                },
            },
        )
        redacted = trust_inventory._redacted_environment(report.to_dict())
        self.assertEqual(redacted["details"]["stderr"], "GITHUB_TOKEN=***")
        self.assertEqual(
            redacted["details"]["environment"]["PIP_CERT"], "https://***@host/ca.pem"
        )
        self.assertIsNone(redacted["details"]["environment"]["SSL_CERT_FILE"])
        self.assertEqual(report.details["stderr"], "GITHUB_TOKEN=secret123")

    def test_conflicting_report_paths_are_refused_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            bundle = folder / "b.pem"
            original = _pem(_fake_der("Example"))
            bundle.write_text(original, encoding="ascii")
            cases = (
                ["--bundle", str(bundle), "--json", str(bundle)],
                [
                    "--bundle",
                    str(bundle),
                    "--json",
                    str(folder / "r"),
                    "--markdown",
                    str(folder / "r"),
                ],
            )
            for argv in cases:
                with (
                    self.subTest(argv=argv),
                    contextlib.redirect_stderr(io.StringIO()) as err,
                ):
                    self.assertEqual(trust_inventory.main(argv), 2)
                    self.assertIn("TRUST INVENTORY ERROR", err.getvalue())
            self.assertEqual(bundle.read_text(encoding="ascii"), original)
            self.assertFalse((folder / "r").exists())

    def test_markdown_escapes_backticks_and_newlines(self):
        report = trust_inventory.EnvironmentReport(
            "label", "not inspected: probe exited 1", {"stderr": "a\nb`c"}
        )
        text = trust_inventory._markdown(
            [report], [{"path": "x", "line": 1, "text": "run `pip`"}], []
        )
        self.assertIn("- stderr: a b'c", text)
        self.assertIn("- x:1: `run 'pip'`", text)


if __name__ == "__main__":
    unittest.main()
