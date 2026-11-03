"""Windows bundleの静的配置検査と最小環境の構成を、合成したPE fileで確認します（Issue #149）。"""

from __future__ import annotations

import io
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import windows_bundle_check as check

SECTION_RVA = 0x1000
SECTION_OFFSET = 0x200


def build_pe(
    imports: list[str],
    delay_imports: list[str] | None = None,
    machine: int = check.MACHINE_AMD64,
    old_delay_format: bool = False,
) -> bytes:
    """Return a minimal PE32+ image whose import tables name `imports` and `delay_imports`."""
    delay_imports = delay_imports or []
    # VA-based delay descriptors predate PE32+; keep their VAs within 32 bits.
    image_base = 0x400000 if old_delay_format else 0x140000000
    import_size = 20 * (len(imports) + 1)
    delay_size = 32 * (len(delay_imports) + 1)
    names = b""
    name_rvas: list[int] = []
    for name in [*imports, *delay_imports]:
        name_rvas.append(SECTION_RVA + import_size + delay_size + len(names))
        names += name.encode("ascii") + b"\0"
    section = bytearray()
    for rva in name_rvas[: len(imports)]:
        section += struct.pack("<IIIII", 0, 0, 0, rva, 0)
    section += bytes(20)
    for rva in name_rvas[len(imports) :]:
        if old_delay_format:
            section += struct.pack("<IIIIIIII", 0, image_base + rva, 0, 0, 0, 0, 0, 0)
        else:
            section += struct.pack("<IIIIIIII", 1, rva, 0, 0, 0, 0, 0, 0)
    section += bytes(32)
    section += names
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<Q", optional, 24, image_base)
    struct.pack_into("<I", optional, 108, 16)
    if imports:
        struct.pack_into("<II", optional, 112 + 8 * 1, SECTION_RVA, import_size)
    if delay_imports:
        struct.pack_into(
            "<II", optional, 112 + 8 * 13, SECTION_RVA + import_size, delay_size
        )
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    header += b"PE\0\0" + struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, len(optional), 0)
    header += optional
    header += b".idata\0\0" + struct.pack(
        "<IIIIIIHHI",
        len(section),
        SECTION_RVA,
        len(section),
        SECTION_OFFSET,
        0,
        0,
        0,
        0,
        0,
    )
    header += bytes(SECTION_OFFSET - len(header))
    return bytes(header + section)


class PeImportReaderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_normal_and_delay_import_names_are_read(self):
        path = self.write(
            "a.dll", build_pe(["KERNEL32.dll", "MSVCP140.dll"], ["dxgi.dll"])
        )
        result = check.read_pe_imports(path)
        self.assertEqual(result.machine, check.MACHINE_AMD64)
        self.assertEqual(result.imports, ("KERNEL32.dll", "MSVCP140.dll"))
        self.assertEqual(result.delay_imports, ("dxgi.dll",))

    def test_old_va_based_delay_descriptor_is_read(self):
        path = self.write("a.dll", build_pe([], ["late.dll"], old_delay_format=True))
        self.assertEqual(check.read_pe_imports(path).delay_imports, ("late.dll",))

    def test_malformed_files_raise_value_error(self):
        valid = build_pe(["KERNEL32.dll"])
        cases = {
            "not-mz": b"ZZ" + valid[2:],
            "short": b"MZ",
            "no-signature": valid[:0x40] + b"XX\0\0" + valid[0x44:],
            "truncated": valid[:0x60],
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    check.read_pe_imports(self.write(f"{label}.dll", data))

    def test_import_table_with_zero_size_is_still_read(self):
        data = bytearray(build_pe(["MSVCP140.dll"]))
        size_offset = 0x40 + 4 + 20 + 112 + 8 * 1 + 4
        self.assertNotEqual(struct.unpack_from("<I", data, size_offset)[0], 0)
        struct.pack_into("<I", data, size_offset, 0)
        path = self.write("zero-size.dll", bytes(data))
        self.assertEqual(check.read_pe_imports(path).imports, ("MSVCP140.dll",))

    def test_non_ascii_name_and_unterminated_table_raise_value_error(self):
        non_ascii = build_pe(["KERNEL32.dll"]).replace(
            b"KERNEL32.dll", b"KERNEL\xff2.dll"
        )
        with self.assertRaises(ValueError):
            check.read_pe_imports(self.write("non-ascii.dll", non_ascii))
        path = self.write("long.dll", build_pe(["a.dll", "b.dll"], ["c.dll", "d.dll"]))
        with patch.object(check, "MAX_DESCRIPTORS", 1):
            with self.assertRaisesRegex(ValueError, "more than 1 entries"):
                check.read_pe_imports(path)

    def test_os_components_are_api_sets_and_listed_dlls_only(self):
        self.assertTrue(check.is_os_component("api-ms-win-crt-runtime-l1-1-0.dll"))
        self.assertTrue(check.is_os_component("KERNEL32.dll"))
        self.assertTrue(check.is_os_component("WINSPOOL.DRV"))
        for name in (
            "MSVCP140.dll",
            "VCRUNTIME140.dll",
            "vcruntime140_1.dll",
            "tcl90.dll",
        ):
            with self.subTest(name=name):
                self.assertFalse(check.is_os_component(name))


class BundleLayoutTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bundle = Path(self.directory.name) / "Kadoka"
        (self.bundle / check.ASSETS_DIR).mkdir(parents=True)
        (self.bundle / check.ASSETS_DIR / "icon.png").write_bytes(b"png")
        self.put("Kadoka.exe", ["KERNEL32.dll", "python314.dll"])
        self.put(
            "kadoka_native_runtime.dll",
            ["d3d11.dll", "api-ms-win-core-winrt-l1-1-0.dll"],
        )
        self.put(
            "_internal/python314.dll",
            ["VCRUNTIME140.dll", "api-ms-win-crt-heap-l1-1-0.dll"],
        )
        self.put("_internal/VCRUNTIME140.dll", ["KERNEL32.dll"])
        self.put("_internal/PIL/_imaging.pyd", ["python314.dll", "USER32.dll"])

    def tearDown(self):
        self.directory.cleanup()

    def put(self, relative: str, imports: list[str], **options) -> None:
        path = self.bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build_pe(imports, **options))

    def test_self_contained_bundle_passes(self):
        self.assertEqual(check.check_layout(self.bundle), [])

    def test_vc_runtime_missing_from_bundle_fails_even_if_the_host_has_it(self):
        self.put("kadoka_native_runtime.dll", ["MSVCP140.dll"])
        errors = check.check_layout(self.bundle)
        self.assertEqual(len(errors), 1)
        self.assertIn("kadoka_native_runtime.dll imports MSVCP140.dll", errors[0])
        self.assertIn("VC++ runtime", errors[0])

    def test_bundled_dependency_next_to_module_or_in_internal_resolves(self):
        self.put("kadoka_native_runtime.dll", ["MSVCP140.dll", "vcruntime140.dll"])
        self.put("msvcp140.dll", ["KERNEL32.dll"])
        self.assertEqual(check.check_layout(self.bundle), [])

    def test_dependency_only_in_an_unrelated_subdirectory_does_not_resolve(self):
        self.put("_internal/PIL/libwebp.dll", ["KERNEL32.dll"])
        self.put("kadoka_native_runtime.dll", ["libwebp.dll"])
        errors = check.check_layout(self.bundle)
        self.assertTrue(any("imports libwebp.dll" in error for error in errors), errors)

    def test_unknown_delay_load_fails(self):
        self.put("_internal/_wmi.pyd", ["python314.dll"], delay_imports=["mystery.dll"])
        errors = check.check_layout(self.bundle)
        module = Path("_internal") / "_wmi.pyd"
        self.assertEqual(
            errors,
            [
                f"{module} delay-loads mystery.dll, which is neither bundled nor an OS component"
            ],
        )

    def test_native_dll_outside_the_root_fails(self):
        (self.bundle / "kadoka_native_runtime.dll").rename(
            self.bundle / "_internal" / "kadoka_native_runtime.dll"
        )
        errors = check.check_layout(self.bundle)
        self.assertTrue(
            any("must be in the bundle root" in error for error in errors), errors
        )

    def test_missing_python_runtime_and_assets_fail(self):
        (self.bundle / "_internal" / "python314.dll").unlink()
        (self.bundle / check.ASSETS_DIR / "icon.png").unlink()
        errors = "\n".join(check.check_layout(self.bundle))
        self.assertIn("expected exactly one python3*.dll", errors)
        self.assertIn("bundled assets are missing or empty", errors)

    def test_non_x64_module_and_unreadable_module_fail(self):
        self.put("_internal/x86.dll", ["KERNEL32.dll"], machine=0x14C)
        (self.bundle / "_internal" / "broken.pyd").write_bytes(b"MZ")
        errors = "\n".join(check.check_layout(self.bundle))
        self.assertIn("machine 0x014c / optional header 0x20b is not x64 PE32+", errors)
        self.assertIn("broken.pyd: cannot read PE imports", errors)

    def test_amd64_machine_with_pe32_optional_header_fails(self):
        data = bytearray(build_pe(["KERNEL32.dll"]))
        struct.pack_into("<H", data, 0x40 + 4 + 20, 0x10B)
        (self.bundle / "_internal" / "mixed.dll").write_bytes(bytes(data))
        errors = " | ".join(check.check_layout(self.bundle))
        self.assertIn("mixed.dll: machine 0x8664 / optional header 0x10b", errors)

    def test_cli_output_survives_a_cp1252_console(self):
        streams = [io.TextIOWrapper(io.BytesIO(), encoding="cp1252") for _ in range(2)]
        missing = self.bundle.parent / "配布 確認"
        with patch("sys.stdout", streams[0]), patch("sys.stderr", streams[1]):
            self.assertEqual(check.main(["layout", str(missing)]), 1)
            streams[1].flush()
        self.assertIn(b"\\u914d\\u5e03", streams[1].buffer.getvalue())

    def test_cli_reports_errors_and_exit_code(self):
        self.put("kadoka_native_runtime.dll", ["MSVCP140.dll"])
        with patch("sys.stderr") as stderr:
            self.assertEqual(check.main(["layout", str(self.bundle)]), 1)
        self.assertIn(
            "BUNDLE LAYOUT ERROR",
            "".join(call.args[0] for call in stderr.write.call_args_list),
        )


class MinimalEnvironmentTest(unittest.TestCase):
    def test_developer_variables_are_dropped_and_profile_dirs_are_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            developer = {
                "PATH": r"C:\Python314;C:\tools",
                "PYTHONPATH": "src",
                "PYTHONHOME": r"C:\Python314",
                "KADOKA_NATIVE_RUNTIME": r"C:\dev\kadoka_native_runtime.dll",
                "SystemRoot": r"C:\Windows",
            }
            with patch.dict(os.environ, developer, clear=True):
                environment = check.minimal_environment(workdir)
            self.assertFalse(
                [
                    key
                    for key in environment
                    if key.upper().startswith(("PYTHON", "KADOKA"))
                ]
            )
            system = Path(r"C:\Windows")
            expected = [system / "System32", system, system / "System32" / "Wbem"]
            self.assertEqual(environment["PATH"], ";".join(map(str, expected)))
            for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE"):
                with self.subTest(key=key):
                    self.assertTrue(Path(environment[key]).is_relative_to(workdir))
                    self.assertTrue(Path(environment[key]).is_dir())

    def test_report_structure_and_identity_are_validated(self):
        relocated = Path(tempfile.gettempdir()) / "relocated"
        good = {
            "schema": check.SELF_CHECK_SCHEMA,
            "ok": True,
            "executable": str(relocated.resolve() / "Kadoka.exe"),
            "checks": [
                {"name": name, "ok": True, "detail": "x"}
                for name in check.REQUIRED_SELF_CHECKS
            ],
        }
        self.assertEqual(check.report_errors(good, 0, relocated), [])
        cases = [
            ([], 0, "not an object with a checks list"),
            ({**good, "checks": None}, 0, "not an object with a checks list"),
            ({**good, "checks": []}, 0, "missing required checks"),
            (
                {**good, "checks": good["checks"][:-1]},
                0,
                "missing required checks: ['trust']",
            ),
            ({**good, "checks": ["x"]}, 0, "item 0 is malformed"),
            (
                {**good, "checks": [{"name": "tk", "ok": False, "detail": "d"}]},
                0,
                "self-check tk: d",
            ),
            ({**good, "schema": "other"}, 0, "schema is 'other'"),
            ({**good, "ok": "yes"}, 0, "with ok='yes'"),
            (good, 1, "result 1"),
            ({**good, "executable": r"C:\dev\Kadoka.exe"}, 0, "not the relocated copy"),
        ]
        for report, code, text in cases:
            with self.subTest(text=text):
                self.assertIn(
                    text, " | ".join(check.report_errors(report, code, relocated))
                )

    def test_launch_refuses_a_non_empty_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            (workdir / "left-over.txt").write_text("x", encoding="utf-8")
            errors = check.launch_relocated(workdir, workdir)
        self.assertEqual(len(errors), 1)
        self.assertIn("must be new or empty", errors[0])


if __name__ == "__main__":
    unittest.main()
