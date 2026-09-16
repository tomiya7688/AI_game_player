from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ai_game_player.action_executor import ActionExecutor
from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import EmergencyStop, SafetyGuardConfig, WindowsTargetProbe
from ai_game_player.window_selector import WindowsWindowSelector


TITLE = "Kadoka Safety CI Target"


def read_step(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return -1
    return int(value.get("step", -1)) if isinstance(value, dict) else -1


def wait_for_step(path: Path, expected: int, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if read_step(path) == expected:
            return
        time.sleep(0.02)
    raise RuntimeError(f"sample state did not reach step {expected}; current={read_step(path)}")


def find_window(title: str, timeout_seconds: float = 15.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    selector = WindowsWindowSelector()
    while time.monotonic() < deadline:
        for window in selector.list_windows():
            if window.title == title:
                return window.handle
        time.sleep(0.05)
    raise RuntimeError(f"safety CI target window not found: {title}")


def ensure_foreground_window(handle: int, timeout_seconds: float = 5.0) -> None:
    """Recover deterministic focus on hosted Windows runners before global-input validation."""
    user32 = ctypes.windll.user32
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if int(user32.GetForegroundWindow()) == int(handle):
            return
        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
        if int(user32.GetForegroundWindow()) == int(handle):
            return
        rect = (ctypes.c_long * 4)()
        if user32.GetWindowRect(handle, ctypes.byref(rect)):
            # Clicking the non-client title bar is a test-harness-only way to restore focus;
            # it does not invoke the game control under test.
            user32.SetCursorPos(int(rect[0]) + 80, int(rect[1]) + 8)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.05)
    raise RuntimeError("safety CI target could not be focused for global input validation")


def expect_blocked(executor: ActionExecutor, candidate: ActionCandidate, expected: str) -> str:
    try:
        executor.execute(candidate)
    except RuntimeError as exc:
        message = str(exc)
        if expected.lower() not in message.lower():
            raise AssertionError(f"expected block marker {expected!r}, got: {message}") from exc
        return message
    raise AssertionError(f"action unexpectedly executed: {candidate.action_id}")


def expect_inflight_stop(
    executor: ActionExecutor,
    candidate: ActionCandidate,
    reason: str,
    delay_seconds: float,
    max_latency_seconds: float,
) -> float:
    executor.rearm_safety()
    executor.record_observation()
    timer = threading.Timer(delay_seconds, lambda: executor.trigger_emergency_stop(reason))
    timer.start()
    started = time.monotonic()
    try:
        executor.execute(candidate)
    except RuntimeError as exc:
        if "emergency stop" not in str(exc).lower():
            raise
    else:
        raise AssertionError(f"in-flight state was not interrupted: {candidate.action_id}")
    finally:
        timer.join(timeout=1.0)
    elapsed = time.monotonic() - started
    if elapsed >= max_latency_seconds:
        raise AssertionError(
            f"Emergency Stop interruption was too slow for {candidate.action_id}: {elapsed:.3f}s"
        )
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows SafetyGuard / Emergency Stop acceptance smoke")
    parser.add_argument("--state-file", type=Path, default=Path("build/safety-ci/sample_state.json"))
    parser.add_argument("--log", type=Path, default=Path("build/safety-ci/safety_guard.jsonl"))
    args = parser.parse_args()

    if os.name != "nt":
        raise RuntimeError("Windows safety acceptance requires Windows")

    root = Path(__file__).resolve().parents[1]
    sample_script = root / "tools" / "e2e_sample_game.py"
    state_file = args.state_file.resolve()
    log_path = args.log.resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (state_file, log_path):
        if path.exists():
            path.unlink()

    process = subprocess.Popen(
        [
            sys.executable,
            str(sample_script),
            "--state-file",
            str(state_file),
            "--steps",
            "3",
            "--title",
            TITLE,
        ],
        cwd=root,
    )

    report: dict[str, object] = {
        "idle_or_between_actions": False,
        "recognition_or_decision_busy": False,
        "wait_inflight": False,
        "key_hold_inflight": False,
        "double_click_inflight": False,
        "rearm_then_live_input": False,
        "fresh_observation_required": False,
        "invalid_coordinate": False,
        "target_loss": False,
    }
    executor: ActionExecutor | None = None
    try:
        handle = find_window(TITLE)
        ensure_foreground_window(handle)
        target = WindowsTargetProbe().inspect(handle)
        if not target.valid or not target.visible or target.pid <= 0:
            raise RuntimeError(f"invalid safety CI target: {target}")
        if not target.foreground:
            raise RuntimeError("safety CI target must be foreground for global input validation")
        wait_for_step(state_file, 0)

        emergency_stop = EmergencyStop()
        executor = ActionExecutor(
            dry_run=False,
            window_handle=handle,
            input_mode="mouse",
            emergency_stop=emergency_stop,
            safety_config=SafetyGuardConfig(
                require_target=True,
                require_foreground=True,
                max_actions_per_second=100,
                max_burst_actions=200,
                max_hold_seconds=2.0,
            ),
            safety_log_path=log_path,
        )

        advance_button = ActionCandidate(
            "advance-1",
            "click",
            "Advance",
            target.client_offset_x + 170,
            target.client_offset_y + 130,
        )
        noop_x = target.client_offset_x + 20
        noop_y = target.client_offset_y + 20

        executor.trigger_emergency_stop("safety CI idle stop")
        expect_blocked(executor, advance_button, "emergency_stop")
        time.sleep(0.05)
        if read_step(state_file) != 0:
            raise AssertionError("target changed while idle Emergency Stop was active")
        report["idle_or_between_actions"] = True

        executor.rearm_safety()
        executor.record_observation()
        worker_started = threading.Event()
        worker_release = threading.Event()

        def simulated_busy_upstream() -> None:
            worker_started.set()
            worker_release.wait(timeout=2.0)

        worker = threading.Thread(target=simulated_busy_upstream, name="safety-ci-busy-upstream")
        worker.start()
        if not worker_started.wait(timeout=1.0):
            raise AssertionError("simulated recognition/decision worker did not start")
        executor.trigger_emergency_stop("safety CI upstream-busy stop")
        if not emergency_stop.is_triggered():
            raise AssertionError("Emergency Stop did not latch while upstream worker was busy")
        expect_blocked(executor, advance_button, "emergency_stop")
        if not worker.is_alive():
            raise AssertionError("upstream worker unexpectedly ended before independent stop check")
        worker_release.set()
        worker.join(timeout=1.0)
        if worker.is_alive():
            raise AssertionError("simulated upstream worker did not finish")
        report["recognition_or_decision_busy"] = True

        wait_latency = expect_inflight_stop(
            executor,
            ActionCandidate("interruptible-wait", "wait", "2.0"),
            "safety CI wait stop",
            0.10,
            1.0,
        )
        if read_step(state_file) != 0:
            raise AssertionError("target changed during interrupted wait")
        report["wait_inflight"] = True
        report["wait_interrupt_latency_seconds"] = round(wait_latency, 4)

        key_latency = expect_inflight_stop(
            executor,
            ActionCandidate("interruptible-key-hold", "key", "SPACE", hold_seconds=1.0),
            "safety CI key-hold stop",
            0.10,
            1.0,
        )
        if executor.live_executor is None or getattr(executor.live_executor, "_held_keys", set()):
            raise AssertionError("held key state remained after Emergency Stop")
        if read_step(state_file) != 0:
            raise AssertionError("target changed during interrupted key hold")
        report["key_hold_inflight"] = True
        report["key_hold_interrupt_latency_seconds"] = round(key_latency, 4)

        double_click_latency = expect_inflight_stop(
            executor,
            ActionCandidate("interruptible-double-click", "double_click", "No-op", noop_x, noop_y),
            "safety CI double-click stop",
            0.02,
            0.5,
        )
        if read_step(state_file) != 0:
            raise AssertionError("no-op double-click changed sample state")
        report["double_click_inflight"] = True
        report["double_click_interrupt_latency_seconds"] = round(double_click_latency, 4)

        executor.rearm_safety()
        expect_blocked(executor, advance_button, "observation_stale")
        report["fresh_observation_required"] = True
        executor.rearm_safety()
        executor.record_observation()
        result = executor.execute(advance_button)
        if not result.executed:
            raise AssertionError("re-armed live input did not execute")
        wait_for_step(state_file, 1)
        report["rearm_then_live_input"] = True

        invalid = ActionCandidate(
            "outside-client",
            "click",
            "Outside",
            target.window_width + 50,
            target.window_height + 50,
        )
        expect_blocked(executor, invalid, "invalid_coordinate")
        if read_step(state_file) != 1:
            raise AssertionError("invalid-coordinate action changed target state")
        report["invalid_coordinate"] = True

        process.terminate()
        process.wait(timeout=5)
        expect_blocked(executor, advance_button, "target_invalid")
        report["target_loss"] = True

        events = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        if len(events) < 9:
            raise AssertionError(f"expected SafetyGuard audit events, found {len(events)}")
        report["audit_event_count"] = len(events)
        report["all_implemented_states_stoppable"] = all(
            bool(report[key])
            for key in (
                "idle_or_between_actions",
                "recognition_or_decision_busy",
                "wait_inflight",
                "key_hold_inflight",
                "double_click_inflight",
                "rearm_then_live_input",
                "fresh_observation_required",
                "invalid_coordinate",
                "target_loss",
            )
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if executor is not None:
            executor.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
