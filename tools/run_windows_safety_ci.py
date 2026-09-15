from __future__ import annotations

import argparse
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


def expect_blocked(executor: ActionExecutor, candidate: ActionCandidate, expected: str) -> str:
    try:
        executor.execute(candidate)
    except RuntimeError as exc:
        message = str(exc)
        if expected not in message:
            raise AssertionError(f"expected block marker {expected!r}, got: {message}") from exc
        return message
    raise AssertionError(f"action unexpectedly executed: {candidate.action_id}")


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
        "emergency_blocked": False,
        "inflight_interrupted": False,
        "rearm_executed": False,
        "invalid_coordinate_blocked": False,
        "target_loss_blocked": False,
    }
    try:
        handle = find_window(TITLE)
        target = WindowsTargetProbe().inspect(handle)
        if not target.valid or not target.visible or target.pid <= 0:
            raise RuntimeError(f"invalid safety CI target: {target}")
        wait_for_step(state_file, 0)

        emergency_stop = EmergencyStop()
        executor = ActionExecutor(
            dry_run=False,
            window_handle=handle,
            input_mode="window_message",
            emergency_stop=emergency_stop,
            safety_config=SafetyGuardConfig(
                require_target=True,
                require_foreground=False,
                max_actions_per_second=100,
                max_burst_actions=200,
            ),
            safety_log_path=log_path,
        )

        first_button = ActionCandidate(
            "advance-1",
            "click",
            "Advance",
            target.client_offset_x + 170,
            target.client_offset_y + 130,
        )

        executor.trigger_emergency_stop("safety CI pre-input stop")
        expect_blocked(executor, first_button, "emergency_stop")
        time.sleep(0.1)
        if read_step(state_file) != 0:
            raise AssertionError("target changed while Emergency Stop was active")
        report["emergency_blocked"] = True

        executor.rearm_safety()
        timer = threading.Timer(0.10, lambda: executor.trigger_emergency_stop("safety CI in-flight stop"))
        timer.start()
        started = time.monotonic()
        try:
            executor.execute(ActionCandidate("interruptible-wait", "wait", "2.0"))
        except RuntimeError as exc:
            if "emergency stop" not in str(exc).lower():
                raise
        else:
            raise AssertionError("in-flight wait was not interrupted by Emergency Stop")
        finally:
            timer.join(timeout=1.0)
        elapsed = time.monotonic() - started
        if elapsed >= 1.5:
            raise AssertionError(f"Emergency Stop interruption was too slow: {elapsed:.3f}s")
        if read_step(state_file) != 0:
            raise AssertionError("target changed during interrupted action")
        report["inflight_interrupted"] = True
        report["interrupt_latency_seconds"] = round(elapsed, 4)

        executor.rearm_safety()
        result = executor.execute(first_button)
        if not result.executed:
            raise AssertionError("re-armed live input did not execute")
        wait_for_step(state_file, 1)
        report["rearm_executed"] = True

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
        report["invalid_coordinate_blocked"] = True

        process.terminate()
        process.wait(timeout=5)
        executor.rearm_safety()
        expect_blocked(executor, first_button, "target_invalid")
        report["target_loss_blocked"] = True

        events = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        if len(events) < 4:
            raise AssertionError(f"expected SafetyGuard audit events, found {len(events)}")
        report["audit_event_count"] = len(events)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
