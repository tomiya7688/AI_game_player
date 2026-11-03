"""配布bundleが同梱・信頼するCAの検査が、incident rootと検査できないCA資料を不合格にすることを確認します（Issue #515）。"""

from __future__ import annotations

import base64
import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.test_trust_inventory import EMSIGN_ECC_G3_PEM, _fake_der, _pem
from tools import windows_bundle_check as check

ROOT = Path(__file__).resolve().parents[1]
CLEAN_PEM = _pem(_fake_der("Example Root CA")) + _pem(
    _fake_der("emSign Root CA - G1", "eMudhra Technologies Limited")
)
INCIDENT_DER = base64.b64decode(
    "".join(EMSIGN_ECC_G3_PEM.strip().splitlines()[1:-1]), validate=True
)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class CarriedTrustTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bundle = Path(self.directory.name) / "Kadoka"
        (self.bundle / "_internal").mkdir(parents=True)
        (self.bundle / "Kadoka.exe").write_bytes(b"MZ" + bytes(64))
        (self.bundle / "_internal" / "python314.dll").write_bytes(b"MZ" + bytes(64))

    def tearDown(self):
        self.directory.cleanup()

    def put(self, relative: str, data: bytes) -> None:
        path = self.bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def errors(self) -> str:
        return " | ".join(check.check_carried_trust(self.bundle)[1])

    def test_bundle_without_ca_material_passes_with_nothing_carried(self):
        self.assertEqual(check.check_carried_trust(self.bundle), ([], []))

    def test_clean_certifi_bundle_is_listed_and_passes(self):
        self.put("_internal/certifi/cacert.pem", CLEAN_PEM.encode("ascii"))
        found, errors = check.check_carried_trust(self.bundle)
        self.assertEqual(errors, [])
        self.assertEqual(len(found), 1)
        self.assertIn("_internal/certifi/cacert.pem: 2 certificates sha256=", found[0])

    def test_incident_root_fails_wherever_it_is_carried(self):
        incident = (CLEAN_PEM + EMSIGN_ECC_G3_PEM).encode("ascii")
        for relative in (
            "_internal/certifi/cacert.pem",
            "_internal/pip/_vendor/certifi/cacert.pem",
            # A stale or renamed file without a certificate suffix is found by its marker.
            "_internal/leftover-from-old-build.dat",
        ):
            with self.subTest(relative=relative):
                self.put(relative, incident)
                self.assertIn(
                    f"{relative}: incident certificate 86a1ecba", self.errors()
                )
                (self.bundle / relative).unlink()

    def test_cli_fails_on_an_incident_and_passes_a_clean_bundle(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(check.main(["trust", str(self.bundle)]), 0)
        self.assertIn("BUNDLE TRUST: 0 CA files carried", output.getvalue())
        self.assertIn("BUNDLE TRUST OK", output.getvalue())
        self.put("_internal/certifi/cacert.pem", EMSIGN_ECC_G3_PEM.encode("ascii"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(check.main(["trust", str(self.bundle)]), 1)
        self.assertIn("BUNDLE TRUST ERROR:", output.getvalue())
        self.assertIn("incident root (GHSA-6q48-6c7w-9gw2)", output.getvalue())

    def test_zip_members_are_checked_including_a_zip_appended_to_a_dll(self):
        archive = _zip_bytes({"certifi/cacert.pem": EMSIGN_ECC_G3_PEM.encode()})
        self.put("_internal/base_library.zip", archive)
        self.put("_internal/tcl90.dll", b"MZ" + bytes(200) + archive)
        errors = self.errors()
        self.assertIn("_internal/base_library.zip!certifi/cacert.pem: incident", errors)
        self.assertIn("_internal/tcl90.dll!certifi/cacert.pem: incident", errors)

    def test_clean_zip_member_is_listed(self):
        self.put(
            "_internal/base_library.zip",
            _zip_bytes({"a.pyc": b"code", "ca/roots.crt": CLEAN_PEM.encode()}),
        )
        found, errors = check.check_carried_trust(self.bundle)
        self.assertEqual(errors, [])
        self.assertEqual(len(found), 1)
        self.assertIn("base_library.zip!ca/roots.crt: 2 certificates", found[0])

    def test_ca_material_that_cannot_be_inspected_fails_closed(self):
        cases = {
            "_internal/roots.der": (
                INCIDENT_DER,
                "looks like CA material but holds no PEM certificate",
            ),
            "_internal/bad.pem": (
                b"-----BEGIN CERTIFICATE-----\n@@@@\n-----END CERTIFICATE-----\n",
                "invalid PEM/base64",
            ),
            "_internal/trusted.pem": (
                b"-----BEGIN TRUSTED CERTIFICATE-----\nAAAA\n"
                b"-----END TRUSTED CERTIFICATE-----\n",
                "unsupported PEM block -----BEGIN TRUSTED CERTIFICATE-----",
            ),
            "_internal/roots.p7b": (
                b"-----BEGIN PKCS7-----\nAAAA\n-----END PKCS7-----\n",
                "unsupported PEM block -----BEGIN PKCS7-----",
            ),
        }
        for relative, (data, text) in cases.items():
            with self.subTest(relative=relative):
                self.put(relative, data)
                self.assertIn(f"{relative}: {text}", self.errors())
                (self.bundle / relative).unlink()

    def test_marker_split_across_read_chunks_is_found(self):
        data = b"x" * 10 + EMSIGN_ECC_G3_PEM.encode()
        self.put("_internal/blob.bin", data)
        with patch.object(check, "SCAN_CHUNK_BYTES", 16):
            self.assertIn("_internal/blob.bin: incident certificate", self.errors())

    def test_oversized_ca_material_and_zip_member_fail(self):
        self.put("_internal/huge.pem", CLEAN_PEM.encode())
        self.put("_internal/base_library.zip", _zip_bytes({"big.bin": b"y" * 200}))
        with patch.object(check.trust_inventory, "MAX_BUNDLE_BYTES", 100):
            errors = self.errors()
        self.assertIn("_internal/huge.pem: CA material too large to inspect", errors)
        self.assertIn("base_library.zip!big.bin: member too large to inspect", errors)

    def test_missing_bundle_fails(self):
        self.assertIn(
            "bundle directory not found",
            " ".join(check.check_carried_trust(self.bundle / "missing")[1]),
        )


class TrustReportTest(unittest.TestCase):
    def report(self, *certificates: bytes, **paths: object) -> dict[str, object]:
        verify = {
            "cafile": None,
            "capath": None,
            "openssl_cafile": r"C:\Program Files\Common Files\SSL\cert.pem",
        }
        verify.update(paths)
        return {
            "default_verify_paths": verify,
            "modules": {"certifi": None, "pip": None, "truststore": None},
            "context_ca_certs": [
                base64.b64encode(der).decode("ascii") for der in certificates
            ],
        }

    def test_clean_default_context_passes_and_is_summarised(self):
        lines, errors = check.trust_report_errors(
            self.report(_fake_der("Example Root CA"), _fake_der("emSign Root CA - G1"))
        )
        self.assertEqual(errors, [])
        self.assertIn(
            "frozen default context: 2 CA certificates, 1 naming eMudhra/emSign, 0 incident",
            lines,
        )
        self.assertTrue(any("openssl_cafile=" in line for line in lines))
        self.assertTrue(any("'certifi': None" in line for line in lines))

    def test_incident_root_in_the_default_context_fails(self):
        lines, errors = check.trust_report_errors(self.report(INCIDENT_DER))
        self.assertIn("1 incident", lines[0])
        self.assertEqual(len(errors), 1)
        self.assertIn(
            "frozen default context: incident certificate 86a1ecba", errors[0]
        )

    def test_malformed_trust_reports_fail(self):
        cases = [
            (None, "has no trust object"),
            # A self-check whose trust item raised leaves the trust object empty.
            ({}, "context_ca_certs is not a list"),
            ({"context_ca_certs": "x"}, "context_ca_certs is not a list"),
            ({**self.report(), "context_ca_certs": ["@@"]}, "[0] is not base64"),
            ({"context_ca_certs": []}, "default_verify_paths is not an object"),
        ]
        for trust, text in cases:
            with self.subTest(text=text):
                self.assertIn(text, " | ".join(check.trust_report_errors(trust)[1]))

    def test_windows_store_entries_are_required_on_windows_and_checked(self):
        clean = base64.b64encode(_fake_der("Example Root CA")).decode("ascii")
        incident = base64.b64encode(INCIDENT_DER).decode("ascii")
        good = {
            **self.report(),
            "platform": "win32",
            "windows_store_certs": [
                {"store": "ROOT", "trust": True, "der": clean},
                {"store": "CA", "trust": ["1.3.6.1.5.5.7.3.1"], "der": clean},
            ],
        }
        lines, errors = check.trust_report_errors(good)
        self.assertEqual(errors, [])
        self.assertIn(
            "windows:ROOT store (read by the frozen _ssl): 1 certificates, 0 incident",
            lines,
        )
        cases = [
            ({**good, "windows_store_certs": None}, "no ROOT store entry on Windows"),
            ({**good, "windows_store_certs": []}, "no ROOT store entry on Windows"),
            ({**good, "windows_store_certs": [{"store": "ROOT"}]}, "[0] is malformed"),
            (
                {
                    **good,
                    "windows_store_certs": [
                        {"store": "ROOT", "trust": True, "der": incident}
                    ],
                },
                "windows:ROOT: incident certificate 86a1ecba",
            ),
        ]
        for trust, text in cases:
            with self.subTest(text=text):
                self.assertIn(text, " | ".join(check.trust_report_errors(trust)[1]))

    def test_existing_default_cafile_and_capath_on_the_host_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            cafile = Path(directory) / "cert.pem"
            cafile.write_text(EMSIGN_ECC_G3_PEM, encoding="ascii")
            capath = Path(directory) / "certs"
            capath.mkdir()
            (capath / "1a2b3c4d.0").write_text(CLEAN_PEM, encoding="ascii")
            (capath / "README").write_text("not a certificate", encoding="ascii")
            lines, errors = check.trust_report_errors(
                self.report(cafile=str(cafile), capath=str(capath))
            )
        joined = " | ".join(errors)
        self.assertRegex(
            joined, r"default cafile: [^|]*cert\.pem: incident certificate 86a1ecba"
        )
        self.assertIn("README: not inspected: no PEM certificates found", joined)
        self.assertTrue(any("1a2b3c4d.0: 2 certificates" in line for line in lines))

    def test_reported_default_paths_that_cannot_be_read_now_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone"
            errors = " | ".join(
                check.trust_report_errors(
                    self.report(cafile=str(missing / "cert.pem"), capath=str(missing))
                )[1]
            )
        self.assertRegex(errors, r"default cafile: [^|]*cert\.pem: not inspected")
        self.assertIn("reported but not a readable directory", errors)

    def test_launch_requires_the_trust_self_check_item(self):
        self.assertIn("trust", check.REQUIRED_SELF_CHECKS)


class BuildScriptTest(unittest.TestCase):
    def test_build_starts_clean_and_gates_the_carried_trust(self):
        script = (ROOT / "tools" / "build_windows_bundle.ps1").read_text(
            encoding="utf-8"
        )
        clean = script.index("Remove-Item -LiteralPath $Stale -Recurse -Force")
        for name in ("$PyInstallerWork", "$DistRoot", "$SpecRoot"):
            self.assertIn(name, script[script.index("foreach ($Stale") : clean])
        pyinstaller = script.index("-m PyInstaller")
        trust = script.index("tools/windows_bundle_check.py trust $AppDir")
        self.assertLess(clean, pyinstaller)
        self.assertLess(pyinstaller, trust)
        self.assertIn("throw", script[trust : trust + 200])


if __name__ == "__main__":
    unittest.main()
