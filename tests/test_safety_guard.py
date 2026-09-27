import ctypes
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_game_player.safety_guard as safety_guard_module

from ai_game_player.action_executor import ActionExecutor
from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import (
    EmergencyStop,
    EmergencyStopMonitor,
    NativeSafetyValidator,
    SafetyGuard,
    SafetyGuardConfig,
    SafetyLog,
    TargetState,
    WindowsTargetProbe,
)


class FakeWindowUser32:
    def __init__(self, *, valid=True, foreground_handle=100, visible=True):
        self.valid = valid
        self.foreground_handle = foreground_handle
        self.visible = visible
        self.calls = []

    def IsWindow(self, _handle):
        self.calls.append("IsWindow")
        return self.valid

    def GetWindowThreadProcessId(self, _handle, pid_pointer):
        self.calls.append("GetWindowThreadProcessId")
        ctypes.cast(pid_pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 4321
        return 7

    def GetWindowRect(self, _handle, rect_pointer):
        self.calls.append("GetWindowRect")
        rect = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
        rect[:] = (100, 200, 900, 800)
        return 1

    def GetClientRect(self, _handle, rect_pointer):
        self.calls.append("GetClientRect")
        rect = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
        rect[:] = (0, 0, 784, 570)
        return 1

    def ClientToScreen(self, _handle, point_pointer):
        self.calls.append("ClientToScreen")
        point = ctypes.cast(point_pointer, ctypes.POINTER(ctypes.c_long * 2)).contents
        point[:] = (108, 230)
        return 1

    def IsWindowVisible(self, _handle):
        self.calls.append("IsWindowVisible")
        return self.visible

    def GetForegroundWindow(self):
        self.calls.append("GetForegroundWindow")
        return self.foreground_handle

class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTargetProbe:
    def __init__(self, states=None, error: Exception | None = None) -> None:
        self.states = list(states or [target_state()])
        self.error = error
        self.calls = 0

    def inspect(self, _handle: int) -> TargetState:
        self.calls += 1
        if self.error is not None:
            raise self.error
        index = min(self.calls - 1, len(self.states) - 1)
        return self.states[index]


def target_state(
    *,
    pid: int = 42,
    valid: bool = True,
    visible: bool = True,
    foreground: bool = True,
) -> TargetState:
    return TargetState(
        100,
        pid,
        valid,
        visible,
        foreground,
        640,
        480,
        8,
        30,
        624,
        442,
    )


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.released = False

    def execute(self, candidate):
        self.calls += 1
        return type(
            "Result",
            (),
            {"action_id": candidate.action_id, "executed": True, "mode": "fake", "detail": "ok"},
        )()

    def release_all(self) -> None:
        self.released = True


class SafetyGuardTest(unittest.TestCase):
    def test_native_safety_validator_loads_and_calls_the_ctypes_abi_contract(self):
        class FakeNativeFunction:
            def __init__(self):
                self.argtypes = None
                self.restype = None
                self.action = None

            def __call__(self, action_pointer, result_pointer):
                action = ctypes.cast(
                    action_pointer,
                    ctypes.POINTER(NativeSafetyValidator._Action),
                ).contents
                result = ctypes.cast(
                    result_pointer,
                    ctypes.POINTER(NativeSafetyValidator._Result),
                ).contents
                self.action = {
                    name: getattr(action, name)
                    for name, _field_type in NativeSafetyValidator._Action._fields_
                }
                result.allowed = 1
                result.reason_code = 17
                return 0

        fake_function = FakeNativeFunction()
        library = SimpleNamespace(kadoka_safety_validate_action=fake_function)
        configured_path = str(Path(__file__).resolve())

        with (
            patch.dict(safety_guard_module.os.environ, {"KADOKA_NATIVE_RUNTIME": configured_path}),
            patch.object(safety_guard_module.ctypes, "CDLL", return_value=library) as load_library,
        ):
            validator = NativeSafetyValidator.try_load()

        self.assertIsNotNone(validator)
        load_library.assert_called_once_with(configured_path)
        self.assertEqual(
            fake_function.argtypes,
            [
                ctypes.POINTER(NativeSafetyValidator._Action),
                ctypes.POINTER(NativeSafetyValidator._Result),
            ],
        )
        self.assertIs(fake_function.restype, ctypes.c_int32)

        allowed, reason = validator.validate(
            ActionCandidate("native-check", "key", "CTRL+F4", hold_seconds=0.25),
            width=640,
            height=480,
            max_hold_seconds=0.5,
        )

        self.assertEqual((allowed, reason), (True, 17))
        self.assertEqual(
            fake_function.action,
            {
                "struct_size": ctypes.sizeof(NativeSafetyValidator._Action),
                "kind": 3,
                "x": 0,
                "y": 0,
                "viewport_width": 640,
                "viewport_height": 480,
                "hold_seconds": 0.25,
                "max_hold_seconds": 0.5,
                "key_code": 0x73,
                "modifiers": 1,
            },
        )

    def test_windows_target_probe_reads_pid_visibility_and_client_geometry_without_os_calls(self):
        user32 = FakeWindowUser32()

        with (
            patch.object(safety_guard_module.os, "name", "nt"),
            patch.object(safety_guard_module.ctypes, "windll", SimpleNamespace(user32=user32), create=True),
        ):
            target = WindowsTargetProbe().inspect(100)

        self.assertTrue(target.valid)
        self.assertEqual(target.pid, 4321)
        self.assertTrue(target.visible)
        self.assertTrue(target.foreground)
        self.assertEqual((target.window_width, target.window_height), (800, 600))
        self.assertEqual((target.client_offset_x, target.client_offset_y), (8, 30))
        self.assertEqual((target.client_width, target.client_height), (784, 570))
        self.assertNotIn("SetCursorPos", user32.calls)
        self.assertNotIn("mouse_event", user32.calls)
        self.assertNotIn("PostMessageW", user32.calls)

    def test_windows_target_probe_reports_hidden_background_window(self):
        user32 = FakeWindowUser32(visible=False, foreground_handle=101)

        with (
            patch.object(safety_guard_module.os, "name", "nt"),
            patch.object(safety_guard_module.ctypes, "windll", SimpleNamespace(user32=user32), create=True),
        ):
            target = WindowsTargetProbe().inspect(100)

        self.assertTrue(target.valid)
        self.assertFalse(target.visible)
        self.assertFalse(target.foreground)
    def test_windows_target_probe_stops_after_invalid_handle(self):
        user32 = FakeWindowUser32(valid=False)

        with (
            patch.object(safety_guard_module.os, "name", "nt"),
            patch.object(safety_guard_module.ctypes, "windll", SimpleNamespace(user32=user32), create=True),
        ):
            target = WindowsTargetProbe().inspect(100)

        self.assertFalse(target.valid)
        self.assertEqual(target.pid, 0)
        self.assertEqual(user32.calls, ["IsWindow"])
    def make_guard(self, **config_overrides):
        clock = FakeClock()
        config = SafetyGuardConfig(**config_overrides)
        guard = SafetyGuard(
            config,
            window_handle=100,
            target_probe=FakeTargetProbe(),
            emergency_stop=EmergencyStop(),
            clock=clock,
            native_validator=None,
        )
        return guard, clock

    def test_valid_target_client_click_is_allowed_and_logged(self):
        log = SafetyLog()
        guard = SafetyGuard(
            SafetyGuardConfig(),
            window_handle=100,
            target_probe=FakeTargetProbe(),
            emergency_stop=EmergencyStop(),
            log=log,
            native_validator=None,
        )
        decision = guard.check(ActionCandidate("go", "click", "GO", 100, 100))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "allowed")
        self.assertEqual(decision.target_pid, 42)
        self.assertEqual(log.events()[-1].action_id, "go")

    def test_invalid_coordinate_and_non_client_coordinate_fail_closed(self):
        guard, _ = self.make_guard()
        outside_window = guard.check(ActionCandidate("bad", "click", "BAD", 900, 10))
        self.assertFalse(outside_window.allowed)
        self.assertEqual(outside_window.code, "invalid_coordinate")

        title_bar_guard, _ = self.make_guard()
        title_bar = title_bar_guard.check(ActionCandidate("title", "click", "TITLE", 100, 10))
        self.assertFalse(title_bar.allowed)
        self.assertEqual(title_bar.code, "outside_client_area")

    def test_target_exit_and_restart_are_detected_by_window_and_pid_validation(self):
        stop = EmergencyStop()
        exit_probe = FakeTargetProbe([target_state(), target_state(valid=False, visible=False)])
        guard = SafetyGuard(
            SafetyGuardConfig(),
            window_handle=100,
            target_probe=exit_probe,
            emergency_stop=stop,
            native_validator=None,
        )
        self.assertTrue(guard.check(ActionCandidate("go", "click", "GO", 100, 100)).allowed)
        exited = guard.check(ActionCandidate("go2", "click", "GO", 100, 100))
        self.assertFalse(exited.allowed)
        self.assertEqual(exited.code, "target_invalid")

        restart_probe = FakeTargetProbe([target_state(pid=42), target_state(pid=99)])
        restart_guard = SafetyGuard(
            SafetyGuardConfig(),
            window_handle=100,
            target_probe=restart_probe,
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        self.assertTrue(restart_guard.check(ActionCandidate("go", "click", "GO", 100, 100)).allowed)
        restarted = restart_guard.check(ActionCandidate("go2", "click", "GO", 100, 100))
        self.assertFalse(restarted.allowed)
        self.assertEqual(restarted.code, "target_pid_changed")

    def test_foreground_validation_blocks_global_input_to_background_target(self):
        guard = SafetyGuard(
            SafetyGuardConfig(require_foreground=True),
            window_handle=100,
            target_probe=FakeTargetProbe([target_state(foreground=False)]),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        decision = guard.check(ActionCandidate("go", "click", "GO", 100, 100))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "target_not_foreground")

    def test_alt_tab_system_keys_and_f12_are_denied(self):
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        for label in ("ALT+TAB", "ALT+F4", "CTRL+ALT+DELETE", "WIN", "F12"):
            with self.subTest(label=label):
                decision = guard.check(ActionCandidate(label, "key", label))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, "denied_key")

    def test_key_hold_has_hard_maximum(self):
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False, max_hold_seconds=0.5),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        decision = guard.check(ActionCandidate("hold", "key", "A", hold_seconds=0.75))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "hold_timeout")

    def test_1000_click_burst_is_blocked_before_all_inputs_are_allowed(self):
        clock = FakeClock()
        guard = SafetyGuard(
            SafetyGuardConfig(
                require_target=False,
                require_foreground=False,
                max_actions_per_second=20,
                max_burst_actions=1000,
                max_same_action_repeats=1000,
                max_cycle_repeats=1000,
            ),
            emergency_stop=EmergencyStop(),
            clock=clock,
            native_validator=None,
        )
        allowed = 0
        blocked = None
        for index in range(1000):
            decision = guard.check(ActionCandidate(f"click-{index}", "click", "CLICK", 10, 10))
            if decision.allowed:
                allowed += 1
            else:
                blocked = decision
                break
        self.assertLess(allowed, 1000)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.code, "rate_limit")

    def test_repeat_cycle_and_no_progress_limits_are_separate(self):
        clock = FakeClock()
        cycle_guard = SafetyGuard(
            SafetyGuardConfig(
                require_target=False,
                require_foreground=False,
                max_actions_per_second=100,
                max_burst_actions=100,
                max_same_action_repeats=100,
                max_cycle_repeats=3,
            ),
            emergency_stop=EmergencyStop(),
            clock=clock,
            native_validator=None,
        )
        decision = None
        for action_id in ("a", "b", "a", "b", "a", "b"):
            decision = cycle_guard.check(ActionCandidate(action_id, "wait", "0"))
        self.assertIsNotNone(decision)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "cycle_limit")

        no_progress = SafetyGuard(
            SafetyGuardConfig(
                require_target=False,
                require_foreground=False,
                max_actions_per_second=100,
                no_progress_repeat_limit=2,
            ),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        self.assertTrue(no_progress.check(ActionCandidate("same", "wait", "0")).allowed)
        no_progress.record_progress(False)
        no_progress.record_progress(False)
        stalled = no_progress.check(ActionCandidate("same", "wait", "0"))
        self.assertFalse(stalled.allowed)
        self.assertEqual(stalled.code, "no_progress")

    def test_guard_exception_is_block_not_allow(self):
        guard = SafetyGuard(
            SafetyGuardConfig(),
            window_handle=100,
            target_probe=FakeTargetProbe(error=RuntimeError("probe exploded")),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        decision = guard.check(ActionCandidate("go", "click", "GO", 100, 100))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "guard_exception")
        self.assertIn("probe exploded", decision.reason)

    def test_emergency_stop_monitor_blocks_executor_until_explicit_rearm(self):
        stop = EmergencyStop()
        monitor = EmergencyStopMonitor(stop, poller=lambda: True, interval_seconds=0.01)
        monitor.start()
        deadline = time.monotonic() + 1.0
        while not stop.is_triggered() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(stop.is_triggered())

        fake = FakeExecutor()
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False),
            emergency_stop=stop,
            native_validator=None,
        )
        executor = ActionExecutor(False, fake, safety_guard=guard)
        with self.assertRaisesRegex(RuntimeError, "emergency_stop"):
            executor.execute(ActionCandidate("go", "wait", "0"))
        self.assertEqual(fake.calls, 0)

        executor.rearm_safety()
        result = executor.execute(ActionCandidate("go", "wait", "0"))
        self.assertTrue(result.executed)
        self.assertEqual(fake.calls, 1)
        monitor.stop()

    def test_trigger_emergency_stop_releases_executor_input_state(self):
        fake = FakeExecutor()
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False),
            emergency_stop=EmergencyStop(),
            native_validator=None,
        )
        executor = ActionExecutor(False, fake, safety_guard=guard)
        executor.trigger_emergency_stop("user stop")
        self.assertTrue(fake.released)
        with self.assertRaises(RuntimeError):
            executor.execute(ActionCandidate("go", "wait", "0"))

    def test_safety_log_is_jsonl_and_config_round_trips(self):
        config = SafetyGuardConfig(max_hold_seconds=0.25, require_target=False, require_foreground=False)
        restored = SafetyGuardConfig.from_dict(config.to_dict())
        self.assertEqual(restored, config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safety.jsonl"
            guard = SafetyGuard(
                config,
                emergency_stop=EmergencyStop(),
                log=SafetyLog(path),
                native_validator=None,
            )
            guard.check(ActionCandidate("go", "wait", "0"))
            entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["action_id"], "go")
        self.assertTrue(entry["allowed"])
        self.assertEqual(entry["code"], "allowed")


if __name__ == "__main__":
    unittest.main()
