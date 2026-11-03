"""固定バージョンのclang-tidyでNative C/C++ソースを解析するSecurity Profileの実行口（Issue #115）。

検査規則は native/.clang-tidy に置き、このスクリプトはバージョン照合、対象ファイル選択、
コンパイルフラグの付与、結果の集約だけを担う。ネットワークへは接続しない。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PINNED_CLANG_TIDY_VERSION = "22.1.8"
NATIVE_ROOT = Path("native")
WINDOWS_ONLY_SOURCES = frozenset({"native/tests/wgc_capture_test.cpp"})


@dataclass(frozen=True)
class ClangTidyTarget:
    path: Path
    compiler_arguments: tuple[str, ...]


def parse_llvm_version(version_output: str) -> str | None:
    """`clang-tidy --version` の出力から `LLVM version X.Y.Z` のバージョン文字列を取り出す。"""
    for line in version_output.splitlines():
        marker = "LLVM version "
        if marker in line:
            return line.split(marker, 1)[1].strip().split()[0]
    return None


def build_targets(repository_root: Path, *, windows: bool) -> list[ClangTidyTarget]:
    """解析対象のソースと各ソースへ渡すコンパイル引数を決定的な順序で返す。"""
    include_argument = "-I" + str(repository_root / NATIVE_ROOT / "include")
    targets: list[ClangTidyTarget] = []
    for directory, extra in (("src", ("-DKADOKA_RUNTIME_BUILD",)), ("tests", ())):
        for source in sorted((repository_root / NATIVE_ROOT / directory).glob("*")):
            relative = source.relative_to(repository_root).as_posix()
            if source.suffix not in {".c", ".cpp"}:
                continue
            if relative in WINDOWS_ONLY_SOURCES and not windows:
                continue
            standard = "-std=c11" if source.suffix == ".c" else "-std=c++20"
            targets.append(
                ClangTidyTarget(source, (standard, include_argument, *extra))
            )
    return targets


def run_clang_tidy(clang_tidy: str, targets: Sequence[ClangTidyTarget]) -> int:
    """各対象へclang-tidyを実行し、1件でも失敗があれば1を返す。"""
    failed: list[str] = []
    for target in targets:
        command = [
            clang_tidy,
            "--quiet",
            str(target.path),
            "--",
            *target.compiler_arguments,
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        output = (completed.stdout + completed.stderr).strip()
        print(f"== {target.path.as_posix()} (exit {completed.returncode})")
        if output:
            print(output)
        if completed.returncode != 0:
            failed.append(target.path.as_posix())
    if failed:
        print("clang-tidy findings in: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"clang-tidy {PINNED_CLANG_TIDY_VERSION}: {len(targets)} files, no findings")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="固定バージョンclang-tidyでNative Runtimeを解析する（Issue #115）"
    )
    parser.add_argument(
        "--clang-tidy", default="clang-tidy", help="clang-tidy 実行ファイル"
    )
    parser.add_argument(
        "--root", type=Path, default=Path("."), help="リポジトリのルート"
    )
    parser.add_argument("--expected-version", default=PINNED_CLANG_TIDY_VERSION)
    args = parser.parse_args(argv)

    try:
        version_output = subprocess.run(
            [args.clang_tidy, "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        print(
            f"native-clang-tidy: clang-tidy を実行できません: {error}", file=sys.stderr
        )
        return 2
    actual_version = parse_llvm_version(version_output)
    if actual_version != args.expected_version:
        print(
            f"native-clang-tidy: clang-tidy {args.expected_version} が必要ですが {actual_version} が見つかりました",
            file=sys.stderr,
        )
        return 2
    targets = build_targets(args.root.resolve(), windows=sys.platform == "win32")
    return run_clang_tidy(args.clang_tidy, targets)


if __name__ == "__main__":
    raise SystemExit(main())
