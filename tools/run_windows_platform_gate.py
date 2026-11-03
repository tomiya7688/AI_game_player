"""Windows platform gate（Issue #94）: Windows境界のテストを実入力センチネル下で実行する。

GitHub ActionsのWindows runnerで、target/window検証・座標変換・入力backend・capture・ABI・
bundle self-check等のテストを1プロセスで実行する。user32の入力APIは
tools/windows_input_sentinel.pyで監視し、テスト自身が作った窓以外への入力、またはグローバル入力が
1回でも試みられたらテスト結果に関わらず失敗する。Windows以外では実行を拒否する。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
import time
import unittest
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT / "src", ROOT, ROOT / "tests"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from tools.windows_input_sentinel import (  # noqa: E402
    GuardedFunction,
    InputSentinel,
    InputSentinelViolation,
    windows_owner_process_id_of,
)

# Windows固有の分岐を持つ、または実Win32 API／自作テスト窓を使うテスト。子プロセスから入力を
# 送るテスト（live-target smoke等）はセンチネルが及ばないため、別jobのまま含めない。
GATE_TEST_MODULES = (
    "test_windows_input_sentinel",
    "test_windows_abi",
    "test_window_selector",
    "test_windows_input",
    "test_windows_capture_integration",
    "test_screen_capture",
    "test_capture_region",
    "test_runtime_target_boundary",
    "test_planning_window_snapshot",
    "test_planning_input_admission",
    "test_safety_guard",
    "test_action_executor",
    "test_planning_file_reader",
    "test_windows_bundle_check",
    "test_bundle_self_check",
    "test_captured_source",
    "test_windows_planning_frame_freshness",
)
# lease/TTL/hold/release-allの状態遷移（Fail-safe Runtime）は全モジュールを対象にする。
GATE_TEST_GLOBS = ("test_fail_safe*.py",)
# Windowsでだけ実行される検証。skipされたらgateが空振りしているため失敗にする。
REQUIRED_WINDOWS_TESTS = (
    "test_windows_input_sentinel.WindowsUser32HookTest.test_real_user32_input_is_blocked_before_dispatch",
    "test_windows_abi.WindowsAbiTest.test_native_libraries_accept_prototypes_without_sending_input",
    "test_planning_file_reader.PlanningFileReaderTests.test_windows_atomic_replace_with_reader_held_open_20_times",
)
DEFAULT_REPORT = ROOT / "build" / "windows-platform-gate" / "report.json"


def gate_test_modules(tests_root: Path = ROOT / "tests") -> list[str]:
    """固定モジュールとglob一致モジュールを重複なく、安定した順序で返す。"""
    modules = list(GATE_TEST_MODULES)
    for pattern in GATE_TEST_GLOBS:
        for path in sorted(tests_root.glob(pattern)):
            if path.stem not in modules:
                modules.append(path.stem)
    return modules


class _RecordingResult(unittest.TextTestResult):
    """実行・skipしたテストIDを記録し、遮断記録へ実行中テストIDを添えるTextTestResult。"""

    sentinel: InputSentinel | None = None

    def __init__(self, *arguments: object, **keywords: object) -> None:
        super().__init__(*arguments, **keywords)  # type: ignore[arg-type]
        self.executed_ids: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.executed_ids.append(test.id())
        if self.sentinel is not None:
            self.sentinel.context = test.id()
        super().startTest(test)


def _absent_window_handle(user32: object) -> int:
    """IsWindowが偽を返すHWND値を返す。見つからなければ0（HWND 0も遮断対象）。"""
    is_window = getattr(user32, "IsWindow")
    for candidate in range(0x7FFF0000, 0x7FFF0100, 4):
        if not is_window(candidate):
            return candidate
    return 0


def _self_check(sentinel: InputSentinel) -> list[str]:
    """install後のuser32取得経路が監視下にあり、遮断が実呼出し前に起きることを確かめる。"""
    problems: list[str] = []
    fresh_user32 = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    cached_user32 = getattr(ctypes, "windll").user32
    for label, library in (("WinDLL", fresh_user32), ("windll", cached_user32)):
        for name in ("keybd_event", "SendInput", "PostMessageW", "SetCursorPos"):
            if not isinstance(getattr(library, name), GuardedFunction):
                problems.append(f"{label}.user32.{name} is not guarded")
    if problems:
        # 監視されていない関数を呼ぶと実入力になるため、ここで中断する。
        return problems
    # 判定に誤りがあって実関数まで届いても無害なよう、存在しないHWNDとVK 0を使う。
    absent_window = _absent_window_handle(fresh_user32)
    for call in (
        lambda: fresh_user32.keybd_event(0, 0, 0, 0),
        lambda: cached_user32.PostMessageW(absent_window, 0x0100, 0, 0),
    ):
        try:
            call()
        except InputSentinelViolation:
            continue
        except Exception as error:  # 想定外の失敗もgate失敗として報告する。
            problems.append(f"self-check call failed unexpectedly: {error!r}")
            continue
        problems.append("a guarded call reached user32 without being blocked")
    if len(sentinel.blocked) != 2:
        problems.append(
            f"self-check expected 2 blocked calls, recorded {len(sentinel.blocked)}"
        )
    sentinel.blocked.clear()
    sentinel.allowed.clear()
    return problems


def run_gate(report_path: Path, verbosity: int = 2) -> int:
    """センチネル下で対象テストを実行し、JSON reportを書いて終了コードを返す（0=合格）。"""
    if os.name != "nt":
        print("Windows platform gate runs only on Windows", file=sys.stderr)
        return 2
    # install前にimport済みのモジュールは差し替え前の関数参照を保持し得るため、先にimportさせない。
    preloaded = sorted(
        name for name in sys.modules if name.split(".")[0] == "ai_game_player"
    )
    sentinel = InputSentinel(windows_owner_process_id_of())
    sentinel.install()
    started = time.monotonic()
    try:
        self_check_problems = [
            f"imported before the sentinel: {name}" for name in preloaded
        ] or _self_check(sentinel)
        result: _RecordingResult | None = None
        if not self_check_problems:
            suite = unittest.defaultTestLoader.loadTestsFromNames(gate_test_modules())
            _RecordingResult.sentinel = sentinel
            runner = unittest.TextTestRunner(
                verbosity=verbosity, resultclass=_RecordingResult
            )
            result = runner.run(suite)  # type: ignore[assignment]
    finally:
        sentinel.uninstall()

    skipped = (
        []
        if result is None
        else [{"test": case.id(), "reason": reason} for case, reason in result.skipped]
    )
    skipped_ids = {entry["test"] for entry in skipped}
    executed = set() if result is None else set(result.executed_ids)
    missing_required = [
        test
        for test in REQUIRED_WINDOWS_TESTS
        if test not in executed or test in skipped_ids
    ]
    passed = (
        not self_check_problems
        and result is not None
        and result.wasSuccessful()
        and not sentinel.blocked
        and not missing_required
    )
    report = {
        "schema": "windows-platform-gate/v1",
        "passed": passed,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "modules": gate_test_modules(),
        "self_check_problems": self_check_problems,
        "tests_run": 0 if result is None else result.testsRun,
        "failures": []
        if result is None
        else [case.id() for case, _ in result.failures],
        "errors": [] if result is None else [case.id() for case, _ in result.errors],
        "skipped": skipped,
        "required_windows_tests_missing_or_skipped": missing_required,
        "blocked_input_calls": [asdict(event) for event in sentinel.blocked],
        "allowed_owned_window_input_messages": len(sentinel.allowed),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: report[key] for key in ("passed", "tests_run", "self_check_problems")}
        )
    )
    for event in sentinel.blocked:
        print(f"BLOCKED real-input attempt: {asdict(event)}", file=sys.stderr)
    for test in missing_required:
        print(f"REQUIRED Windows test did not run: {test}", file=sys.stderr)
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--verbosity", type=int, default=2)
    arguments = parser.parse_args(argv)
    return run_gate(arguments.report, arguments.verbosity)


if __name__ == "__main__":
    raise SystemExit(main())
