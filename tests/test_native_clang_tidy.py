from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tools.native_clang_tidy import (
    PINNED_CLANG_TIDY_VERSION,
    ClangTidyTarget,
    build_targets,
    main,
    parse_llvm_version,
    run_clang_tidy,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class NativeClangTidyTests(unittest.TestCase):
    def test_parse_llvm_version_reads_version_line(self) -> None:
        output = "LLVM (http://llvm.org/):\n  LLVM version 22.1.8\n  Optimized build.\n"

        self.assertEqual(parse_llvm_version(output), "22.1.8")
        self.assertIsNone(parse_llvm_version("clang-tidy unknown"))

    def test_repository_config_pins_the_runner_version(self) -> None:
        config = (REPOSITORY_ROOT / "native" / ".clang-tidy").read_text(
            encoding="utf-8"
        )

        self.assertIn(f"clang-tidy=={PINNED_CLANG_TIDY_VERSION}", config)
        self.assertIn("WarningsAsErrors: '*'", config)

    def test_build_targets_excludes_windows_only_test_off_windows(self) -> None:
        linux = [
            target.path.relative_to(REPOSITORY_ROOT).as_posix()
            for target in build_targets(REPOSITORY_ROOT, windows=False)
        ]
        windows = [
            target.path.relative_to(REPOSITORY_ROOT).as_posix()
            for target in build_targets(REPOSITORY_ROOT, windows=True)
        ]

        self.assertIn("native/src/runtime_api.cpp", linux)
        self.assertIn("native/tests/c_abi_test.c", linux)
        self.assertNotIn("native/tests/wgc_capture_test.cpp", linux)
        self.assertIn("native/tests/wgc_capture_test.cpp", windows)
        self.assertEqual(
            linux,
            sorted(linux, key=lambda path: (not path.startswith("native/src/"), path)),
        )

    def test_build_targets_select_language_standard_and_build_define(self) -> None:
        targets = {
            target.path.name: target.compiler_arguments
            for target in build_targets(REPOSITORY_ROOT, windows=True)
        }

        self.assertEqual(targets["c_abi_test.c"][0], "-std=c11")
        self.assertEqual(targets["runtime_api.cpp"][0], "-std=c++20")
        self.assertIn("-DKADOKA_RUNTIME_BUILD", targets["runtime_api.cpp"])
        self.assertNotIn("-DKADOKA_RUNTIME_BUILD", targets["runtime_api_test.cpp"])

    def test_run_clang_tidy_fails_when_any_file_reports_findings(self) -> None:
        targets = [
            ClangTidyTarget(Path("a.cpp"), ("-std=c++20",)),
            ClangTidyTarget(Path("b.cpp"), ("-std=c++20",)),
        ]

        with mock.patch(
            "tools.native_clang_tidy.subprocess.run",
            side_effect=[_completed(), _completed(1, "b.cpp: error")],
        ):
            with mock.patch("builtins.print"):
                self.assertEqual(run_clang_tidy("clang-tidy", targets), 1)

    def test_main_rejects_unpinned_version_without_analyzing(self) -> None:
        with mock.patch(
            "tools.native_clang_tidy.subprocess.run",
            return_value=_completed(stdout="LLVM version 18.1.8\n"),
        ) as run:
            with mock.patch("builtins.print"):
                status = main(
                    ["--clang-tidy", "clang-tidy", "--root", str(REPOSITORY_ROOT)]
                )

        self.assertEqual(status, 2)
        self.assertEqual(run.call_count, 1)

    def test_main_reports_missing_executable(self) -> None:
        with mock.patch(
            "tools.native_clang_tidy.subprocess.run",
            side_effect=FileNotFoundError("clang-tidy"),
        ):
            with mock.patch("builtins.print"):
                self.assertEqual(main(["--clang-tidy", "missing-clang-tidy"]), 2)


if __name__ == "__main__":
    unittest.main()
