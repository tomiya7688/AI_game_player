from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from ai_game_player.models import ActionCandidate


class SafetyState(str, Enum):
    ACTIVE = "active"
    EMERGENCY_STOP = "emergency_stop"


@dataclass(frozen=True)
class SafetyGuardConfig:
    max_actions_per_second: int = 30
    rate_window_seconds: float = 1.0
    max_burst_actions: int = 120
    burst_window_seconds: float = 5.0
    max_same_action_repeats: int = 100
    max_cycle_repeats: int = 25
    max_session_actions: int = 10000
    max_session_seconds: float = 7200.0
    max_hold_seconds: float = 1.0
    max_wait_seconds: float = 5.0
    no_progress_repeat_limit: int = 25
    require_target: bool = True
    require_foreground: bool = True

    def __post_init__(self) -> None:
        positive_ints = (
            self.max_actions_per_second,
            self.max_burst_actions,
            self.max_same_action_repeats,
            self.max_cycle_repeats,
            self.max_session_actions,
            self.no_progress_repeat_limit,
        )
        if any(value < 1 for value in positive_ints):
            raise ValueError("SafetyGuard integer limits must be positive")
        positive_floats = (
            self.rate_window_seconds,
            self.burst_window_seconds,
            self.max_session_seconds,
            self.max_hold_seconds,
            self.max_wait_seconds,
        )
        if any(value <= 0 for value in positive_floats):
            raise ValueError("SafetyGuard time limits must be positive")
        if self.burst_window_seconds < self.rate_window_seconds:
            raise ValueError("burst window must not be shorter than rate window")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SafetyGuardConfig":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: item for key, item in value.items() if key in allowed})

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TargetState:
    handle: int
    pid: int
    valid: bool
    visible: bool
    foreground: bool
    window_width: int
    window_height: int
    client_offset_x: int
    client_offset_y: int
    client_width: int
    client_height: int

    def contains_client_point(self, x: int, y: int) -> bool:
        return (
            self.client_offset_x <= x < self.client_offset_x + self.client_width
            and self.client_offset_y <= y < self.client_offset_y + self.client_height
        )


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    code: str
    reason: str
    state: SafetyState
    target_pid: int | None = None


@dataclass(frozen=True)
class SafetyEvent:
    timestamp: float
    action_id: str
    allowed: bool
    code: str
    reason: str
    state: str
    target_pid: int | None

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class SafetyLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._events: list[SafetyEvent] = []
        self._lock = threading.Lock()

    def append(self, event: SafetyEvent) -> None:
        with self._lock:
            self._events.append(event)
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def events(self) -> list[SafetyEvent]:
        with self._lock:
            return list(self._events)


class EmergencyStop:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()

    @property
    def state(self) -> SafetyState:
        return SafetyState.EMERGENCY_STOP if self._event.is_set() else SafetyState.ACTIVE

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def is_triggered(self) -> bool:
        return self._event.is_set()

    def trigger(self, reason: str = "emergency stop requested") -> None:
        with self._lock:
            self._reason = reason
            self._event.set()

    def rearm(self) -> None:
        with self._lock:
            self._reason = ""
            self._event.clear()


class EmergencyStopMonitor:
    """Background F12 monitor independent from recognition/decision execution."""

    def __init__(
        self,
        emergency_stop: EmergencyStop,
        poller: Callable[[], bool] | None = None,
        interval_seconds: float = 0.05,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("monitor interval must be positive")
        self.emergency_stop = emergency_stop
        self.poller = poller or self._windows_f12_pressed
        self.interval_seconds = interval_seconds
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._run, name="kadoka-emergency-stop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.2, self.interval_seconds * 4))

    def poll_once(self) -> bool:
        try:
            pressed = bool(self.poller())
        except Exception:
            self.emergency_stop.trigger("emergency stop monitor failed")
            return True
        if pressed:
            self.emergency_stop.trigger("F12 emergency stop")
        return pressed

    def _run(self) -> None:
        while not self._shutdown.wait(self.interval_seconds):
            if self.poll_once():
                return

    @staticmethod
    def _windows_f12_pressed() -> bool:
        if os.name != "nt":
            return False
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x7B) & 0x8000)


class WindowsTargetProbe:
    def inspect(self, window_handle: int) -> TargetState:
        if os.name != "nt":
            raise RuntimeError("Windows target validation requires Windows")
        user32 = ctypes.windll.user32
        handle = int(window_handle)
        if not user32.IsWindow(handle):
            return TargetState(handle, 0, False, False, False, 0, 0, 0, 0, 0, 0)

        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        window_rect = (ctypes.c_long * 4)()
        client_rect = (ctypes.c_long * 4)()
        if not user32.GetWindowRect(handle, ctypes.byref(window_rect)):
            raise RuntimeError("GetWindowRect failed")
        if not user32.GetClientRect(handle, ctypes.byref(client_rect)):
            raise RuntimeError("GetClientRect failed")
        point = (ctypes.c_long * 2)(0, 0)
        if not user32.ClientToScreen(handle, ctypes.byref(point)):
            raise RuntimeError("ClientToScreen failed")
        left, top, right, bottom = (int(value) for value in window_rect)
        return TargetState(
            handle,
            int(pid.value),
            True,
            bool(user32.IsWindowVisible(handle)),
            int(user32.GetForegroundWindow()) == handle,
            right - left,
            bottom - top,
            int(point[0]) - left,
            int(point[1]) - top,
            int(client_rect[2] - client_rect[0]),
            int(client_rect[3] - client_rect[1]),
        )


class NativeSafetyValidator:
    _KIND = {"click": 1, "double_click": 2, "key": 3, "wait": 4}
    _MODIFIER = {"CTRL": 1, "ALT": 2, "SHIFT": 4, "WIN": 8}
    _KEY = {
        "TAB": 0x09,
        "ENTER": 0x0D,
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "SPACE": 0x20,
        "DELETE": 0x2E,
        "F4": 0x73,
        "F12": 0x7B,
        "LWIN": 0x5B,
        "RWIN": 0x5C,
    }

    class _Action(ctypes.Structure):
        _fields_ = [
            ("struct_size", ctypes.c_uint32),
            ("kind", ctypes.c_uint32),
            ("x", ctypes.c_int32),
            ("y", ctypes.c_int32),
            ("viewport_width", ctypes.c_int32),
            ("viewport_height", ctypes.c_int32),
            ("hold_seconds", ctypes.c_double),
            ("max_hold_seconds", ctypes.c_double),
            ("key_code", ctypes.c_uint32),
            ("modifiers", ctypes.c_uint32),
        ]

    class _Result(ctypes.Structure):
        _fields_ = [
            ("struct_size", ctypes.c_uint32),
            ("allowed", ctypes.c_int32),
            ("reason_code", ctypes.c_uint32),
        ]

    def __init__(self, library: object) -> None:
        self.library = library
        function = library.kadoka_safety_validate_action
        function.argtypes = [ctypes.POINTER(self._Action), ctypes.POINTER(self._Result)]
        function.restype = ctypes.c_int32
        self._function = function

    @classmethod
    def try_load(cls) -> "NativeSafetyValidator | None":
        candidates: list[Path] = []
        configured = os.environ.get("KADOKA_NATIVE_RUNTIME")
        if configured:
            candidates.append(Path(configured))
        executable_dir = Path(sys.executable).resolve().parent
        for name in ("kadoka_native_runtime.dll", "libkadoka_native_runtime.so", "libkadoka_native_runtime.dylib"):
            candidates.append(executable_dir / name)
        for path in candidates:
            if not path.exists():
                continue
            try:
                return cls(ctypes.CDLL(str(path)))
            except OSError:
                continue
        return None

    def validate(self, candidate: ActionCandidate, width: int, height: int, max_hold_seconds: float) -> tuple[bool, int]:
        key_code, modifiers = self._key_descriptor(candidate.label)
        action = self._Action(
            ctypes.sizeof(self._Action),
            self._KIND.get(candidate.kind, 0),
            int(candidate.x or 0),
            int(candidate.y or 0),
            int(width),
            int(height),
            float(candidate.hold_seconds or 0.0),
            float(max_hold_seconds),
            key_code,
            modifiers,
        )
        result = self._Result(ctypes.sizeof(self._Result), 0, 0)
        if self._function(ctypes.byref(action), ctypes.byref(result)) != 0:
            raise RuntimeError("native safety validator call failed")
        return bool(result.allowed), int(result.reason_code)

    @classmethod
    def _key_descriptor(cls, label: str) -> tuple[int, int]:
        tokens = [token for token in label.upper().replace(" ", "+").split("+") if token]
        modifiers = 0
        key_name = tokens[-1] if tokens else ""
        for token in tokens[:-1]:
            normalized = "WIN" if token in {"WINDOWS", "LWIN", "RWIN"} else token
            modifiers |= cls._MODIFIER.get(normalized, 0)
        if key_name in {"WINDOWS", "WIN"}:
            key_name = "LWIN"
        if key_name in cls._KEY:
            return cls._KEY[key_name], modifiers
        if len(key_name) == 1:
            return ord(key_name), modifiers
        return 0, modifiers


class SafetyGuard:
    SUPPORTED_KINDS = frozenset({"click", "double_click", "key", "wait"})
    DENIED_KEY_LABELS = frozenset(
        {"ALT+TAB", "ALT+F4", "CTRL+ALT+DELETE", "WIN", "WINDOWS", "LWIN", "RWIN", "F12"}
    )

    def __init__(
        self,
        config: SafetyGuardConfig | None = None,
        *,
        window_handle: int | None = None,
        target_probe: object | None = None,
        emergency_stop: EmergencyStop | None = None,
        log: SafetyLog | None = None,
        clock: Callable[[], float] = time.monotonic,
        native_validator: NativeSafetyValidator | None = None,
    ) -> None:
        self.config = config or SafetyGuardConfig()
        self.window_handle = window_handle
        self.target_probe = target_probe or WindowsTargetProbe()
        self.emergency_stop = emergency_stop or default_emergency_stop()
        self.log = log or SafetyLog()
        self.clock = clock
        self.native_validator = native_validator if native_validator is not None else NativeSafetyValidator.try_load()
        self._history: deque[tuple[float, str]] = deque()
        self._session_started = self.clock()
        self._session_actions = 0
        self._bound_pid: int | None = None
        self._no_progress_count = 0
        self._lock = threading.Lock()

    def check(self, candidate: ActionCandidate) -> SafetyDecision:
        with self._lock:
            try:
                decision = self._check_locked(candidate)
            except Exception as exc:
                decision = SafetyDecision(
                    False,
                    "guard_exception",
                    f"SafetyGuard failed closed: {exc}",
                    self.emergency_stop.state,
                    self._bound_pid,
                )
            self._log(candidate, decision)
            return decision

    def record_progress(self, state_changed: bool) -> None:
        with self._lock:
            self._no_progress_count = 0 if state_changed else self._no_progress_count + 1

    def trigger_emergency_stop(self, reason: str = "emergency stop requested") -> None:
        self.emergency_stop.trigger(reason)

    def rearm(self) -> None:
        with self._lock:
            self.emergency_stop.rearm()
            self._history.clear()
            self._session_started = self.clock()
            self._session_actions = 0
            self._no_progress_count = 0
            self._bound_pid = None

    def _check_locked(self, candidate: ActionCandidate) -> SafetyDecision:
        now = self.clock()
        if self.emergency_stop.is_triggered():
            return self._block("emergency_stop", self.emergency_stop.reason or "emergency stop is active")
        if now - self._session_started > self.config.max_session_seconds:
            return self._block("session_time_limit", "session time limit exceeded")
        if self._session_actions >= self.config.max_session_actions:
            return self._block("session_action_limit", "session action limit exceeded")
        if candidate.kind not in self.SUPPORTED_KINDS:
            return self._block("invalid_action", f"unsupported action kind: {candidate.kind}")

        hold = float(candidate.hold_seconds or 0.0)
        if hold < 0 or hold > self.config.max_hold_seconds:
            return self._block("hold_timeout", "key hold exceeds configured maximum")
        if candidate.kind != "key" and hold:
            return self._block("invalid_action", "hold_seconds is only valid for key actions")
        if candidate.kind in {"click", "double_click"} and (candidate.x is None or candidate.y is None):
            return self._block("invalid_coordinate", "click action requires coordinates")
        if candidate.kind == "wait":
            try:
                wait_seconds = float(candidate.label or "0.5")
            except ValueError:
                wait_seconds = 0.5
            if wait_seconds < 0 or wait_seconds > self.config.max_wait_seconds:
                return self._block("wait_timeout", "wait duration exceeds configured maximum")
        if candidate.kind == "key" and self._normalize_key(candidate.label) in self.DENIED_KEY_LABELS:
            return self._block("denied_key", f"OS/reserved key is denied: {candidate.label}")

        if self.config.require_target:
            if self.window_handle is None:
                return self._block("target_missing", "live input requires a target window")
            target = self.target_probe.inspect(self.window_handle)
            if not target.valid or not target.visible:
                return self._block("target_invalid", "target window is missing or not visible")
            if target.pid <= 0:
                return self._block("target_pid_invalid", "target PID could not be resolved")
            if self._bound_pid is None:
                self._bound_pid = target.pid
            elif target.pid != self._bound_pid:
                return self._block("target_pid_changed", "target window PID changed during the session")
            if self.config.require_foreground and not target.foreground:
                return self._block("target_not_foreground", "target window is not foreground")
            if candidate.kind in {"click", "double_click"}:
                assert candidate.x is not None and candidate.y is not None
                if not (0 <= candidate.x < target.window_width and 0 <= candidate.y < target.window_height):
                    return self._block("invalid_coordinate", "click coordinate is outside the target window")
                if not target.contains_client_point(candidate.x, candidate.y):
                    return self._block("outside_client_area", "click coordinate is outside the target client area")
            if self.native_validator is not None:
                allowed, reason_code = self.native_validator.validate(
                    candidate,
                    target.window_width,
                    target.window_height,
                    self.config.max_hold_seconds,
                )
                if not allowed:
                    return self._block("native_reject", f"native SafetyGuard rejected action (reason={reason_code})")

        self._trim_history(now)
        recent_rate = sum(now - timestamp <= self.config.rate_window_seconds for timestamp, _ in self._history)
        if recent_rate >= self.config.max_actions_per_second:
            return self._block("rate_limit", "action rate limit exceeded")
        if len(self._history) >= self.config.max_burst_actions:
            return self._block("burst_limit", "action burst limit exceeded")

        consecutive = 0
        for _, action_id in reversed(self._history):
            if action_id != candidate.action_id:
                break
            consecutive += 1
        if consecutive >= self.config.max_same_action_repeats:
            return self._block("repeat_limit", "same action repeated too many times")
        if self._has_repeating_cycle(candidate.action_id):
            return self._block("cycle_limit", "repeating action cycle detected")
        if self._no_progress_count >= self.config.no_progress_repeat_limit and consecutive:
            return self._block("no_progress", "repeated action continued without observed progress")

        self._history.append((now, candidate.action_id))
        self._session_actions += 1
        return SafetyDecision(
            True,
            "allowed",
            "deterministic SafetyGuard checks passed",
            self.emergency_stop.state,
            self._bound_pid,
        )

    def _has_repeating_cycle(self, next_action_id: str) -> bool:
        ids = [action_id for _, action_id in self._history] + [next_action_id]
        for width in range(2, 5):
            needed = width * self.config.max_cycle_repeats
            if len(ids) < needed:
                continue
            tail = ids[-needed:]
            pattern = tail[:width]
            if all(tail[index : index + width] == pattern for index in range(0, needed, width)):
                return True
        return False

    def _trim_history(self, now: float) -> None:
        while self._history and now - self._history[0][0] > self.config.burst_window_seconds:
            self._history.popleft()

    def _block(self, code: str, reason: str) -> SafetyDecision:
        return SafetyDecision(False, code, reason, self.emergency_stop.state, self._bound_pid)

    def _log(self, candidate: ActionCandidate, decision: SafetyDecision) -> None:
        self.log.append(
            SafetyEvent(
                time.time(),
                candidate.action_id,
                decision.allowed,
                decision.code,
                decision.reason,
                decision.state.value,
                decision.target_pid,
            )
        )

    @staticmethod
    def _normalize_key(label: str) -> str:
        return "+".join(token for token in label.upper().replace(" ", "+").split("+") if token)


_DEFAULT_EMERGENCY_STOP = EmergencyStop()
_DEFAULT_MONITOR: EmergencyStopMonitor | None = None
_DEFAULT_MONITOR_LOCK = threading.Lock()


def default_emergency_stop() -> EmergencyStop:
    return _DEFAULT_EMERGENCY_STOP


def ensure_default_emergency_monitor() -> EmergencyStopMonitor | None:
    global _DEFAULT_MONITOR
    if os.name != "nt":
        return None
    with _DEFAULT_MONITOR_LOCK:
        if _DEFAULT_MONITOR is None:
            _DEFAULT_MONITOR = EmergencyStopMonitor(_DEFAULT_EMERGENCY_STOP)
            _DEFAULT_MONITOR.start()
        return _DEFAULT_MONITOR


def rearm_default_emergency_stop() -> None:
    _DEFAULT_EMERGENCY_STOP.rearm()
