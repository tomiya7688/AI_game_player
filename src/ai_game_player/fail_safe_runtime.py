from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4


class FailSafeState(str, Enum):
    SAFE_IDLE = "safe_idle"
    ACTIVE = "active"
    EMERGENCY_STOP = "emergency_stop"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class FailSafeConfig:
    lease_timeout_seconds: float = 0.75
    observation_timeout_seconds: float = 2.0
    max_command_age_seconds: float = 0.5
    max_queue_depth: int = 32
    recent_action_window: int = 256
    hold_ttl_seconds: float = 1.0
    watchdog_poll_seconds: float = 0.05

    def __post_init__(self) -> None:
        values = (
            self.lease_timeout_seconds,
            self.observation_timeout_seconds,
            self.max_command_age_seconds,
            self.hold_ttl_seconds,
            self.watchdog_poll_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("fail-safe time limits must be positive")
        if self.max_queue_depth < 1 or self.recent_action_window < 1:
            raise ValueError("fail-safe queue/history limits must be positive")
        if self.watchdog_poll_seconds >= self.lease_timeout_seconds:
            raise ValueError("watchdog poll interval must be shorter than lease timeout")


@dataclass(frozen=True)
class FailSafeCommand:
    action_id: str
    sequence: int
    epoch: str
    issued_at: float
    observation_at: float
    target_pid: int
    target_handle: int
    session_id: str

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "FailSafeCommand":
        return cls(
            action_id=str(value.get("action_id", "")),
            sequence=int(value.get("sequence", 0)),
            epoch=str(value.get("epoch", "")),
            issued_at=float(value.get("issued_at", 0.0)),
            observation_at=float(value.get("observation_at", 0.0)),
            target_pid=int(value.get("target_pid", 0)),
            target_handle=int(value.get("target_handle", 0)),
            session_id=str(value.get("session_id", "")),
        )


@dataclass(frozen=True)
class FailSafeDecision:
    allowed: bool
    code: str
    reason: str
    state: FailSafeState


class AtomicJsonStore:
    """Crash-consistent single-record JSON store using fsync + atomic replace."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def write(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
            except (AttributeError, OSError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class FailSafeJournal:
    def __init__(self, path: Path, store: AtomicJsonStore | None = None) -> None:
        self.path = path
        self.store = store or AtomicJsonStore(path)

    def read(self) -> dict[str, object]:
        return self.store.read()

    def record(
        self,
        state: FailSafeState,
        reason: str,
        epoch: str,
        *,
        details: dict[str, object] | None = None,
        timestamp: float | None = None,
    ) -> None:
        self.store.write(
            {
                "schema": "kadoka-failsafe-journal/v1",
                "state": state.value,
                "reason": reason,
                "epoch": epoch,
                "timestamp": time.time() if timestamp is None else timestamp,
                "details": dict(details or {}),
            }
        )


class InputLedger:
    """Small cross-process ledger of input that may need an out-of-process release."""

    def __init__(self, path: Path, store: AtomicJsonStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.store = store or AtomicJsonStore(path)
        self.clock = clock
        self._lock = threading.Lock()

    def configure(self, *, target_handle: int, input_mode: str) -> None:
        with self._lock:
            current = self._read()
            current["target_handle"] = int(target_handle)
            current["input_mode"] = str(input_mode)
            current["updated_at"] = self.clock()
            self.store.write(current)

    def hold_key(self, virtual_key: int, ttl_seconds: float) -> None:
        with self._lock:
            current = self._read()
            keys = self._key_entries(current)
            keys[str(int(virtual_key))] = self.clock() + float(ttl_seconds)
            current["held_keys"] = keys
            current["updated_at"] = self.clock()
            self.store.write(current)

    def release_key(self, virtual_key: int) -> None:
        with self._lock:
            current = self._read()
            keys = self._key_entries(current)
            keys.pop(str(int(virtual_key)), None)
            current["held_keys"] = keys
            current["updated_at"] = self.clock()
            self.store.write(current)

    def hold_mouse(self, button: str, ttl_seconds: float) -> None:
        with self._lock:
            current = self._read()
            buttons = self._mouse_entries(current)
            buttons[str(button)] = self.clock() + float(ttl_seconds)
            current["held_mouse"] = buttons
            current["updated_at"] = self.clock()
            self.store.write(current)

    def release_mouse(self, button: str) -> None:
        with self._lock:
            current = self._read()
            buttons = self._mouse_entries(current)
            buttons.pop(str(button), None)
            current["held_mouse"] = buttons
            current["updated_at"] = self.clock()
            self.store.write(current)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._read()

    def clear(self) -> None:
        with self._lock:
            current = self._read()
            current["held_keys"] = {}
            current["held_mouse"] = {}
            current["updated_at"] = self.clock()
            self.store.write(current)

    def _read(self) -> dict[str, object]:
        value = self.store.read()
        return {
            "schema": "kadoka-input-ledger/v1",
            "target_handle": int(value.get("target_handle", 0)),
            "input_mode": str(value.get("input_mode", "mouse")),
            "held_keys": self._key_entries(value),
            "held_mouse": self._mouse_entries(value),
            "updated_at": float(value.get("updated_at", 0.0)),
        }

    @staticmethod
    def _key_entries(value: dict[str, object]) -> dict[str, float]:
        raw = value.get("held_keys", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for key, expires in raw.items():
            try:
                result[str(int(key))] = float(expires)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _mouse_entries(value: dict[str, object]) -> dict[str, float]:
        raw = value.get("held_mouse", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for key, expires in raw.items():
            try:
                result[str(key)] = float(expires)
            except (TypeError, ValueError):
                continue
        return result


class ExternalWatchdogProcess:
    """Detached watchdog that survives loss of the AI process long enough to release input."""

    def __init__(self, state_directory: Path, poll_seconds: float) -> None:
        self.state_directory = state_directory
        self.poll_seconds = poll_seconds
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--failsafe-watchdog", str(self.state_directory)]
        else:
            command = [
                sys.executable,
                "-m",
                "ai_game_player",
                "--failsafe-watchdog",
                str(self.state_directory),
            ]
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path.cwd()),
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)


class FailSafeRuntime:
    """Fail-closed lease/epoch gate between SafetyGuard and OS input."""

    def __init__(
        self,
        state_directory: Path,
        config: FailSafeConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
        journal: FailSafeJournal | None = None,
        lease_store: AtomicJsonStore | None = None,
        ledger: InputLedger | None = None,
        release_callback: Callable[[], None] | None = None,
        external_watchdog: bool = True,
    ) -> None:
        self.state_directory = Path(state_directory)
        self.config = config or FailSafeConfig()
        self.clock = clock
        self.journal = journal or FailSafeJournal(self.state_directory / "journal.json")
        self.lease_store = lease_store or AtomicJsonStore(self.state_directory / "lease.json")
        self.ledger = ledger or InputLedger(self.state_directory / "input_ledger.json", clock=clock)
        self.release_callback = release_callback
        self._lock = threading.RLock()
        self._state = FailSafeState.SAFE_IDLE
        self._reason = "startup_safe_idle"
        self._epoch = ""
        self._session_id = ""
        self._target_pid = 0
        self._target_handle = 0
        self._last_observation_at = 0.0
        self._lease_expires_at = 0.0
        self._observation_expires_at = 0.0
        self._last_sequence = 0
        self._next_sequence = 0
        self._inflight = 0
        self._recent_actions: deque[str] = deque(maxlen=self.config.recent_action_window)
        self._heartbeat_shutdown = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._watchdog = ExternalWatchdogProcess(self.state_directory, self.config.watchdog_poll_seconds) if external_watchdog else None

        previous = self.journal.read()
        previous_state = str(previous.get("state", ""))
        if previous_state in {
            FailSafeState.ACTIVE.value,
            FailSafeState.EMERGENCY_STOP.value,
            FailSafeState.RECOVERY_REQUIRED.value,
        }:
            self._state = FailSafeState.RECOVERY_REQUIRED
            self._reason = "unclean_previous_runtime_state"
        self._persist_or_fail(self._reason)
        if self._watchdog is not None:
            self._watchdog.start()

    @property
    def state(self) -> FailSafeState:
        with self._lock:
            return self._state

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def epoch(self) -> str:
        with self._lock:
            return self._epoch

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def rearm(self, *, target_pid: int, target_handle: int, session_id: str | None = None) -> str:
        with self._lock:
            if target_pid <= 0 or target_handle <= 0:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "invalid_target_on_rearm")
                raise RuntimeError("fail-safe re-arm requires a valid target PID/HWND")
            self._release_inputs()
            self._epoch = uuid4().hex
            self._session_id = session_id or uuid4().hex
            self._target_pid = int(target_pid)
            self._target_handle = int(target_handle)
            self._last_observation_at = 0.0
            self._observation_expires_at = 0.0
            self._last_sequence = 0
            self._next_sequence = 0
            self._inflight = 0
            self._recent_actions.clear()
            self._state = FailSafeState.ACTIVE
            self._reason = "explicit_rearm"
            self._renew_lease_locked()
            if not self._persist_or_fail("explicit_rearm"):
                raise RuntimeError("fail-safe re-arm could not persist critical state")
            self._start_heartbeat_locked()
            return self._epoch

    def heartbeat(self) -> bool:
        with self._lock:
            if self._state != FailSafeState.ACTIVE:
                return False
            self._renew_lease_locked()
            return self._write_lease_or_fail("heartbeat_storage_failure")

    def record_observation(self, observed_at: float | None = None) -> bool:
        with self._lock:
            if self._state != FailSafeState.ACTIVE:
                return False
            now = self.clock()
            timestamp = now if observed_at is None else float(observed_at)
            if timestamp <= 0 or timestamp > now + 1.0:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "invalid_observation_timestamp")
                return False
            self._last_observation_at = timestamp
            self._observation_expires_at = timestamp + self.config.observation_timeout_seconds
            self._renew_lease_locked()
            return self._write_lease_or_fail("observation_storage_failure")

    def make_command(
        self,
        action_id: str,
        *,
        target_pid: int,
        target_handle: int,
        session_id: str | None = None,
    ) -> FailSafeCommand:
        with self._lock:
            self._next_sequence += 1
            return FailSafeCommand(
                action_id=str(action_id),
                sequence=self._next_sequence,
                epoch=self._epoch,
                issued_at=self.clock(),
                observation_at=self._last_observation_at,
                target_pid=int(target_pid),
                target_handle=int(target_handle),
                session_id=self._session_id if session_id is None else str(session_id),
            )

    def check_command(self, command: FailSafeCommand) -> FailSafeDecision:
        with self._lock:
            now = self.clock()
            if self._state != FailSafeState.ACTIVE:
                return self._block("runtime_not_active", f"fail-safe runtime is {self._state.value}")
            if now > self._lease_expires_at:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "lease_expired")
                return self._block("lease_expired", "control lease expired")
            if self._last_observation_at <= 0 or now > self._observation_expires_at:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "observation_stale")
                return self._block("observation_stale", "fresh observation is required")
            if command.epoch != self._epoch:
                return self._block("stale_epoch", "command epoch does not match active control epoch")
            age = now - command.issued_at
            if age < -0.05 or age > self.config.max_command_age_seconds:
                return self._block("stale_command", "command age exceeds fail-safe limit")
            if command.observation_at != self._last_observation_at:
                return self._block("stale_observation", "command was not created from the latest observation")
            if command.sequence <= self._last_sequence:
                return self._block("stale_sequence", "command sequence is stale or duplicated")
            if command.action_id in self._recent_actions:
                return self._block("duplicate_action", "action_id was already accepted in this epoch")
            if command.target_pid != self._target_pid or command.target_handle != self._target_handle:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "target_changed")
                return self._block("target_changed", "command target differs from the re-armed target")
            if command.session_id != self._session_id:
                return self._block("stale_session", "command session does not match active session")
            if self._inflight >= self.config.max_queue_depth:
                return self._block("queue_full", "fail-safe command queue depth exceeded")

            self._last_sequence = command.sequence
            self._recent_actions.append(command.action_id)
            self._inflight += 1
            self._renew_lease_locked()
            if not self._write_lease_or_fail("command_accept_storage_failure"):
                self._inflight = max(0, self._inflight - 1)
                return self._block("storage_failure", "critical fail-safe state could not be persisted")
            return FailSafeDecision(True, "allowed", "fresh lease/observation/target/command", self._state)

    def complete_command(self, command: FailSafeCommand) -> None:
        del command
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if self._state == FailSafeState.ACTIVE:
                self._renew_lease_locked()
                self._write_lease_or_fail("command_complete_storage_failure")

    def emergency_stop(self, reason: str = "emergency stop requested") -> None:
        with self._lock:
            self._transition(FailSafeState.EMERGENCY_STOP, reason)

    def recovery_required(self, reason: str) -> None:
        with self._lock:
            self._transition(FailSafeState.RECOVERY_REQUIRED, reason)

    def safe_idle(self, reason: str = "safe idle requested") -> None:
        with self._lock:
            self._transition(FailSafeState.SAFE_IDLE, reason)

    def close(self) -> None:
        self.safe_idle("runtime closed")
        watchdog = self._watchdog
        if watchdog is not None:
            watchdog.stop()

    def _start_heartbeat_locked(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_shutdown.clear()
        interval = min(self.config.lease_timeout_seconds / 3.0, 0.25)
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(interval,),
            name="kadoka-failsafe-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self, interval: float) -> None:
        while not self._heartbeat_shutdown.wait(interval):
            if not self.heartbeat():
                if self.state != FailSafeState.ACTIVE:
                    return

    def _renew_lease_locked(self) -> None:
        self._lease_expires_at = self.clock() + self.config.lease_timeout_seconds

    def _transition(self, state: FailSafeState, reason: str) -> None:
        self._state = state
        self._reason = reason
        self._heartbeat_shutdown.set()
        self._lease_expires_at = 0.0 if state != FailSafeState.ACTIVE else self._lease_expires_at
        self._release_inputs()
        self._persist_or_fail(reason, allow_state_change=False)

    def _release_inputs(self) -> None:
        callback = self.release_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _persist_or_fail(self, reason: str, *, allow_state_change: bool = True) -> bool:
        try:
            self.journal.record(
                self._state,
                reason,
                self._epoch,
                details={
                    "owner_pid": os.getpid(),
                    "target_pid": self._target_pid,
                    "target_handle": self._target_handle,
                    "session_id": self._session_id,
                },
                timestamp=self.clock(),
            )
            self.lease_store.write(self._lease_payload())
            return True
        except Exception:
            if allow_state_change:
                self._state = FailSafeState.RECOVERY_REQUIRED
                self._reason = "critical_storage_failure"
                self._heartbeat_shutdown.set()
                self._release_inputs()
            return False

    def _write_lease_or_fail(self, reason: str) -> bool:
        try:
            self.lease_store.write(self._lease_payload())
            return True
        except Exception:
            self._state = FailSafeState.RECOVERY_REQUIRED
            self._reason = reason
            self._heartbeat_shutdown.set()
            self._release_inputs()
            try:
                self.journal.record(self._state, reason, self._epoch, timestamp=self.clock())
            except Exception:
                pass
            return False

    def _lease_payload(self) -> dict[str, object]:
        return {
            "schema": "kadoka-control-lease/v1",
            "state": self._state.value,
            "reason": self._reason,
            "epoch": self._epoch,
            "session_id": self._session_id,
            "owner_pid": os.getpid(),
            "target_pid": self._target_pid,
            "target_handle": self._target_handle,
            "lease_expires_at": self._lease_expires_at,
            "observation_at": self._last_observation_at,
            "observation_expires_at": self._observation_expires_at,
            "last_sequence": self._last_sequence,
            "inflight": self._inflight,
            "ledger_path": str(self.ledger.path),
            "journal_path": str(self.journal.path),
            "watchdog_poll_seconds": self.config.watchdog_poll_seconds,
            "updated_at": self.clock(),
        }

    def _block(self, code: str, reason: str) -> FailSafeDecision:
        return FailSafeDecision(False, code, reason, self._state)


def run_external_watchdog(state_directory: Path, *, max_runtime_seconds: float | None = None) -> int:
    """Run the dependency-minimal out-of-process watchdog loop."""

    state_directory = Path(state_directory)
    lease_store = AtomicJsonStore(state_directory / "lease.json")
    journal = FailSafeJournal(state_directory / "journal.json")
    ledger = InputLedger(state_directory / "input_ledger.json")
    started = time.time()
    owner_seen = False

    while True:
        lease = lease_store.read()
        if not lease:
            if max_runtime_seconds is not None and time.time() - started >= max_runtime_seconds:
                return 0
            time.sleep(0.05)
            continue

        now = time.time()
        owner_pid = int(lease.get("owner_pid", 0))
        target_pid = int(lease.get("target_pid", 0))
        target_handle = int(lease.get("target_handle", 0))
        state = str(lease.get("state", FailSafeState.SAFE_IDLE.value))
        epoch = str(lease.get("epoch", ""))
        owner_alive = _process_alive(owner_pid) if owner_pid > 0 else False
        owner_seen = owner_seen or owner_pid > 0

        reason = ""
        if state == FailSafeState.ACTIVE.value:
            if owner_pid > 0 and not owner_alive:
                reason = "owner_process_lost"
            elif target_pid > 0 and not _process_alive(target_pid):
                reason = "target_process_lost"
            elif os.name == "nt" and target_handle > 0 and not _windows_target_matches(target_handle, target_pid):
                reason = "target_window_lost"
            elif now > float(lease.get("lease_expires_at", 0.0)):
                reason = "lease_expired"
            else:
                observation_expires = float(lease.get("observation_expires_at", 0.0))
                if observation_expires <= 0 or now > observation_expires:
                    reason = "observation_stale"

        if reason:
            _release_ledger(ledger, release_all=True, now=now)
            updated = dict(lease)
            updated["state"] = FailSafeState.RECOVERY_REQUIRED.value
            updated["reason"] = reason
            updated["lease_expires_at"] = 0.0
            updated["updated_at"] = now
            try:
                lease_store.write(updated)
                journal.record(
                    FailSafeState.RECOVERY_REQUIRED,
                    reason,
                    epoch,
                    details={"owner_pid": owner_pid, "target_pid": target_pid, "target_handle": target_handle},
                    timestamp=now,
                )
            except Exception:
                pass
            state = FailSafeState.RECOVERY_REQUIRED.value
        elif state != FailSafeState.ACTIVE.value:
            _release_ledger(ledger, release_all=True, now=now)
        else:
            _release_ledger(ledger, release_all=False, now=now)

        if owner_seen and owner_pid > 0 and not owner_alive and state != FailSafeState.ACTIVE.value:
            return 0
        if max_runtime_seconds is not None and now - started >= max_runtime_seconds:
            return 0
        poll = float(lease.get("watchdog_poll_seconds", 0.05))
        time.sleep(max(0.01, min(poll, 0.25)))


def _release_ledger(ledger: InputLedger, *, release_all: bool, now: float) -> None:
    try:
        snapshot = ledger.snapshot()
    except Exception:
        return
    mode = str(snapshot.get("input_mode", "mouse"))
    target_handle = int(snapshot.get("target_handle", 0))
    keys = snapshot.get("held_keys", {})
    buttons = snapshot.get("held_mouse", {})
    if not isinstance(keys, dict):
        keys = {}
    if not isinstance(buttons, dict):
        buttons = {}

    changed = False
    for raw_key, raw_expiry in list(keys.items()):
        try:
            key = int(raw_key)
            expiry = float(raw_expiry)
        except (TypeError, ValueError):
            changed = True
            continue
        if release_all or expiry <= now:
            _release_key_os(key, mode, target_handle)
            changed = True
        else:
            continue
        keys.pop(raw_key, None)
    for button, raw_expiry in list(buttons.items()):
        try:
            expiry = float(raw_expiry)
        except (TypeError, ValueError):
            changed = True
            continue
        if release_all or expiry <= now:
            _release_mouse_os(str(button), mode, target_handle)
            changed = True
        else:
            continue
        buttons.pop(button, None)
    if changed:
        try:
            ledger.store.write(
                {
                    "schema": "kadoka-input-ledger/v1",
                    "target_handle": target_handle,
                    "input_mode": mode,
                    "held_keys": keys,
                    "held_mouse": buttons,
                    "updated_at": now,
                }
            )
        except Exception:
            pass


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _windows_target_matches(handle: int, pid: int) -> bool:
    if os.name != "nt":
        return True
    user32 = ctypes.windll.user32
    if not user32.IsWindow(int(handle)):
        return False
    actual_pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(int(handle), ctypes.byref(actual_pid))
    return int(actual_pid.value) == int(pid)


def _release_key_os(virtual_key: int, input_mode: str, target_handle: int) -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    if input_mode == "window_message" and target_handle > 0 and user32.IsWindow(target_handle):
        user32.PostMessageW(target_handle, 0x0101, int(virtual_key), 0)
    else:
        user32.keybd_event(int(virtual_key), 0, 0x0002, 0)


def _release_mouse_os(button: str, input_mode: str, target_handle: int) -> None:
    if os.name != "nt" or button != "left":
        return
    user32 = ctypes.windll.user32
    if input_mode == "window_message" and target_handle > 0 and user32.IsWindow(target_handle):
        user32.PostMessageW(target_handle, 0x0202, 0, 0)
    else:
        user32.mouse_event(0x0004, 0, 0, 0, 0)
