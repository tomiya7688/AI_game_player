from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.native_security_check import (
    AllowlistEntry,
    apply_allowlist,
    load_allowlist,
    main,
    run_check,
    scan_source,
    strip_comments_and_literals,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class NativeSecurityCheckTests(unittest.TestCase):
    def test_banned_string_api_is_high_severity(self) -> None:
        findings = scan_source(
            "native/src/a.cpp",
            "void f(char* d, const char* s) {\n    strcpy(d, s);\n}\n",
        )

        self.assertEqual(
            [(f.rule, f.severity, f.line_number, f.function) for f in findings],
            [("NSC001", "high", 2, "strcpy")],
        )

    def test_std_and_global_qualified_calls_are_detected(self) -> None:
        findings = scan_source(
            "a.cpp", "std::memcpy(a, b, n);\n::system(cmd);\nstd::sprintf(buf, fmt);\n"
        )

        self.assertEqual(
            [f.function for f in findings], ["memcpy", "system", "sprintf"]
        )
        self.assertEqual([f.rule for f in findings], ["NSC005", "NSC002", "NSC001"])

    def test_member_namespace_and_identifier_substrings_are_ignored(self) -> None:
        source = (
            "buffer.free();\n"
            "pool->system(x);\n"
            "kadoka::memcpy(a, b, n);\n"
            "my_strcpy(a, b);\n"
            "strcpy_s(a, n, b);\n"
            "int memset_count = 0;\n"
        )

        self.assertEqual(scan_source("a.cpp", source), [])

    def test_comments_and_literals_do_not_produce_findings(self) -> None:
        source = (
            "// strcpy(a, b);\n"
            "/* sprintf(a,\n   b); */\n"
            'const char* text = "gets(x)";\n'
            "char quote = '\"'; int n = 1'000;\n"
            'auto raw = R"tag(system("x"))tag";\n'
            "alloca(16);\n"
        )

        findings = scan_source("a.cpp", source)

        self.assertEqual(
            [(f.function, f.line_number) for f in findings], [("alloca", 7)]
        )

    def test_strip_preserves_line_numbers(self) -> None:
        source = '/* a\nb */ x;\n"s\\"t";\n'

        self.assertEqual(
            strip_comments_and_literals(source).count("\n"), source.count("\n")
        )

    def test_allowlist_removes_only_exact_matches_and_reports_stale_entries(
        self,
    ) -> None:
        findings = scan_source(
            "native/src/a.cpp", "std::memcpy(a, b, n);\nstd::memcpy(c, d, n);\n"
        )
        entries = [
            AllowlistEntry(
                "native/src/a.cpp",
                "NSC005",
                "memcpy",
                "std::memcpy(a, b, n);",
                "bounds checked above",
            ),
            AllowlistEntry(
                "native/src/a.cpp",
                "NSC005",
                "memcpy",
                "std::memcpy(x, y, n);",
                "no longer present",
            ),
        ]

        remaining, stale = apply_allowlist(findings, entries)

        self.assertEqual([f.line_number for f in remaining], [2])
        self.assertEqual([entry.code for entry in stale], ["std::memcpy(x, y, n);"])

    def test_allowlist_rejects_missing_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allow.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": [
                            {
                                "path": "a.cpp",
                                "rule": "NSC005",
                                "function": "memcpy",
                                "code": "x",
                                "reason": "short",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_allowlist(path)

    def test_fail_on_high_reports_medium_findings_as_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "native").mkdir()
            (root / "native" / "a.cpp").write_text(
                "int v = atoi(s);\nstrcat(a, b);\n", encoding="utf-8"
            )

            blocking, advisory, stale = run_check([root / "native"], root, None, "high")

        self.assertEqual([f.function for f in blocking], ["strcat"])
        self.assertEqual([f.function for f in advisory], ["atoi"])
        self.assertEqual(stale, [])

    def test_main_returns_failure_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "native").mkdir()
            (root / "native" / "a.c").write_text(
                "void f(void) { char b[4]; gets(b); }\n", encoding="utf-8"
            )
            report = root / "report.json"

            with contextlib.redirect_stderr(io.StringIO()):
                status = main(
                    [
                        str(root / "native"),
                        "--root",
                        str(root),
                        "--no-allowlist",
                        "--output",
                        str(report),
                    ]
                )
            document = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(document["blocking"][0]["rule"], "NSC001")
        self.assertEqual(document["blocking"][0]["path"], "native/a.c")

    def test_call_split_across_lines_is_detected_on_the_name_line(self) -> None:
        findings = scan_source("a.cpp", "int x;\nstrcpy\n    (d, s);\n")

        self.assertEqual(
            [(f.function, f.line_number, f.code) for f in findings],
            [("strcpy", 2, "strcpy")],
        )

    def test_crlf_line_continuation_inside_string_stays_a_literal(self) -> None:
        source = 'const char* t = "a\\\r\nsystem(x)";\r\nmemset(p, 0, n);\r\n'

        findings = scan_source("a.cpp", source)

        self.assertEqual(
            [(f.function, f.line_number) for f in findings], [("memset", 3)]
        )

    def test_each_allowlist_entry_permits_only_one_finding(self) -> None:
        findings = scan_source(
            "native/a.cpp", "std::memcpy(a, b, n);\nstd::memcpy(a, b, n);\n"
        )
        entry = AllowlistEntry(
            "native/a.cpp",
            "NSC005",
            "memcpy",
            "std::memcpy(a, b, n);",
            "bounds checked above",
        )

        single_remaining, _ = apply_allowlist(findings, [entry])
        double_remaining, double_stale = apply_allowlist(findings, [entry, entry])

        self.assertEqual([f.line_number for f in single_remaining], [2])
        self.assertEqual((double_remaining, double_stale), ([], []))

    def test_allowlist_paths_are_normalized_to_relative_posix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allow.json"
            entry = {
                "rule": "NSC005",
                "function": "memcpy",
                "code": "x",
                "reason": "bounds checked above",
            }
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": [
                            {**entry, "path": "./native/src/a.cpp"},
                            {**entry, "path": "native\\src\\b.cpp"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            entries = load_allowlist(path)

        self.assertEqual(
            [item.path for item in entries], ["native/src/a.cpp", "native/src/b.cpp"]
        )

    def test_empty_source_set_is_an_error_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaises(ValueError):
                run_check([root], root, None, "medium")

    def test_repository_native_sources_have_no_blocking_findings(self) -> None:
        blocking, advisory, stale = run_check(
            [REPOSITORY_ROOT / "native"],
            REPOSITORY_ROOT,
            REPOSITORY_ROOT / "config" / "native_security_allowlist.json",
            "medium",
        )

        self.assertEqual(blocking, [])
        self.assertEqual(advisory, [])
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
