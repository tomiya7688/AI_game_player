from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

from ai_game_player.cross_process_lock import cross_process_file_lock


# {
#   責務: [_finite_time: 外部数値を比較する前に有限の実数へ限定する]
#   処理: [bool/文字列/任意objectを拒否し、巨大整数のfloat変換もValueErrorへ統一する]
#   引数: [value: 秒単位の時刻または時間上限] 戻り値: [float: 有限値。正数条件は呼出元が持つ]
#   エラー: [ValueError: 非数値またはNaN/Infinity/表現不能値]
# }
def _finite_time(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("fail-safe times must be finite numeric values")
    try:
        result = float(cast(int | float, value))
    except OverflowError as error:
        raise ValueError("fail-safe times must be finite numeric values") from error
    if not math.isfinite(result):
        raise ValueError("fail-safe times must be finite numeric values")
    return result


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
    hold_ttl_seconds: float = 1.0
    watchdog_poll_seconds: float = 0.05

    # {
    #   責務: [__post_init__: 有限正数の時間上限と正整数のqueue上限だけを受け付ける]
    #   処理: [型・非有限値を大小比較の前に拒否し、既存poll/lease大小条件を維持する]
    #   引数: [self: 構築中の設定] 戻り値: [なし] エラー: [ValueError: 不正な数値境界]
    # }
    def __post_init__(self) -> None:
        times = (
            self.lease_timeout_seconds,
            self.observation_timeout_seconds,
            self.max_command_age_seconds,
            self.hold_ttl_seconds,
            self.watchdog_poll_seconds,
        )
        if any(_finite_time(value) <= 0 for value in times):
            raise ValueError("fail-safe time limits must be positive")
        if type(self.max_queue_depth) is not int or self.max_queue_depth < 1:
            raise ValueError("fail-safe queue depth must be positive")
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

    # {
    #   責務: [from_dict: 復号commandの2時刻を有限数値として検証する]
    #   処理: [文字列/boolの暗黙変換をせず、不正時刻をRuntimeへ渡さない。他ID契約は維持する]
    #   引数: [value: 外部commandのJSON object] 戻り値: [FailSafeCommand: 数値時刻を持つcommand]
    #   エラー: [ValueError: 時刻または既存ID復号に失敗]
    # }
    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "FailSafeCommand":
        return cls(
            action_id=str(value.get("action_id", "")),
            sequence=int(value.get("sequence", 0)),
            epoch=str(value.get("epoch", "")),
            issued_at=_finite_time(value.get("issued_at", 0.0)),
            observation_at=_finite_time(value.get("observation_at", 0.0)),
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
    """Crash-consistent JSON store with cross-process read/write serialization."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")

    def read(self) -> dict[str, object]:
        with cross_process_file_lock(self.lock_path):
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def write(self, value: dict[str, object]) -> None:
        with cross_process_file_lock(self.lock_path):
            self._write_unlocked(value)

    def _write_unlocked(self, value: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(8):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.005 * (attempt + 1))
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
    """Cross-process ledger for keys/buttons that must be releasable after parent loss."""

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
            keys = self._entries(current, "held_keys", integer_keys=True)
            keys[str(int(virtual_key))] = self.clock() + float(ttl_seconds)
            current["held_keys"] = keys
            current["updated_at"] = self.clock()
            self.store.write(current)

    def release_key(self, virtual_key: int) -> None:
        with self._lock:
            current = self._read()
            keys = self._entries(current, "held_keys", integer_keys=True)
            keys.pop(str(int(virtual_key)), None)
            current["held_keys"] = keys
            current["updated_at"] = self.clock()
            self.store.write(current)

    def hold_mouse(self, button: str, ttl_seconds: float) -> None:
        with self._lock:
            current = self._read()
            buttons = self._entries(current, "held_mouse", integer_keys=False)
            buttons[str(button)] = self.clock() + float(ttl_seconds)
            current["held_mouse"] = buttons
            current["updated_at"] = self.clock()
            self.store.write(current)

    def release_mouse(self, button: str) -> None:
        with self._lock:
            current = self._read()
            buttons = self._entries(current, "held_mouse", integer_keys=False)
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
            "held_keys": self._entries(value, "held_keys", integer_keys=True),
            "held_mouse": self._entries(value, "held_mouse", integer_keys=False),
            "updated_at": float(value.get("updated_at", 0.0)),
        }

    @staticmethod
    def _entries(value: dict[str, object], name: str, *, integer_keys: bool) -> dict[str, float]:
        raw = value.get(name, {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for key, expiry in raw.items():
            try:
                normalized = str(int(key)) if integer_keys else str(key)
                result[normalized] = float(expiry)
            except (TypeError, ValueError):
                continue
        return result


class ExternalWatchdogProcess:
    """Detached watcher that survives parent loss and has no AI/model dependency."""

    def __init__(self, state_directory: Path) -> None:
        self.state_directory = state_directory
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--failsafe-watchdog", str(self.state_directory)]
        else:
            command = [sys.executable, "-m", "ai_game_player", "--failsafe-watchdog", str(self.state_directory)]
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(Path.cwd()),
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
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


# {
#   責務: [FailSafeRuntime: 期限付きの入力lease・対象・保持入力を管理し、停止と保存障害では実行を拒否する]
#   フィールド: [state_directory/config: 保存先と秒単位予算, _lock: Runtime状態の同期,
#     journal/lease_store/ledger: 状態と入力所有権の永続化, _state/_reason/_epoch/_session_id: 実行状態と世代,
#     _initial_lease_available: constructor後の一度だけ初期待機を開始できる権利,
#     _target_pid/_target_handle: 許可対象, _last_observation_at/_lease_expires_at/_observation_expires_at: 期限,
#     _last_sequence/_next_sequence/_inflight: 操作連番と処理数, _watchdog/_heartbeat_thread: 有限leaseの監視]
# }
class FailSafeRuntime:
    """Short-lease fail-closed gate between SafetyGuard and OS input."""

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
        self._initial_lease_available = True
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
        self._heartbeat_shutdown = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._watchdog = ExternalWatchdogProcess(self.state_directory) if external_watchdog else None

        previous = self.journal.read()
        previous_state = str(previous.get("state", ""))
        if previous_state in {
            FailSafeState.ACTIVE.value,
            FailSafeState.EMERGENCY_STOP.value,
            FailSafeState.RECOVERY_REQUIRED.value,
        }:
            self._state = FailSafeState.RECOVERY_REQUIRED
            self._reason = "unclean_previous_runtime_state"
        _release_ledger(self.ledger, release_all=True, now=self.clock())
        self._persist_or_fail(self._reason)

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

    # {
    #   責務: [initialize: 停止を解除せず新しいSAFE_IDLE Runtimeの初期leaseだけを開始する]
    #   処理: [Runtime lock内で初期待機と保存状態を照合し、停止・復旧・既存実行からは開始しない]
    #   引数: [target_pid/target_handle: 所有確認済み対象, session_id: 任意のSession識別子]
    #   戻り値: [str: 開始したleaseのepoch]
    #   エラー: [RuntimeError/OSError: 初期待機でない、状態不明、保存・初期化に失敗した]
    # }
    def initialize(
        self, *, target_pid: int, target_handle: int, session_id: str | None = None
    ) -> str:
        with self._lock:
            self._require_initial_idle_locked()
            return self._arm_locked(
                target_pid, target_handle, session_id, initial_only=True
            )

    # {
    #   責務: [rearm: 利用者が明示的に再開したRuntimeを新しいleaseへ切り替える]
    #   処理: [既存の明示再開契約を保ち、保持入力を解放して対象と期限を再設定する]
    #   引数: [target_pid/target_handle: 所有確認済み対象, session_id: 任意のSession識別子]
    #   戻り値: [str: 再開したleaseのepoch]
    #   エラー: [RuntimeError: 対象または保存に失敗した]
    # }
    def rearm(
        self, *, target_pid: int, target_handle: int, session_id: str | None = None
    ) -> str:
        with self._lock:
            return self._arm_locked(
                target_pid, target_handle, session_id, initial_only=False
            )

    # {
    #   責務: [_require_initial_idle_locked: 初期化が停止や保存済みの復旧状態を上書きしないことを確認する]
    #   処理: [呼出側がRuntime lockを保持し、memoryと最新leaseのSAFE_IDLE/epochを照合する]
    #   引数: [なし] 戻り値: [なし]
    #   エラー: [RuntimeError/OSError: 初期待機以外、保存状態不一致、読込失敗]
    # }
    def _require_initial_idle_locked(self) -> None:
        persisted = self.lease_store.read()
        if (
            not self._initial_lease_available
            or self._state != FailSafeState.SAFE_IDLE
            or persisted.get("state") != FailSafeState.SAFE_IDLE.value
            or str(persisted.get("epoch", "")) != self._epoch
            or str(persisted.get("reason", "")) != self._reason
        ):
            raise RuntimeError(
                "initial safety lease requires unchanged SAFE_IDLE state"
            )

    # {
    #   責務: [_arm_locked: Runtime lock内で対象・保持入力・永続lease・watchdogを準備する]
    #   処理: [初期化はI/Oの後にもSAFE_IDLEを照合し、明示再開だけが既存の停止状態から再開できる]
    #   引数: [target_pid/target_handle: 対象, session_id: 任意識別子, initial_only: 初期待機のみを許す場合True]
    #   戻り値: [str: 新しいlease epoch]
    #   エラー: [RuntimeError/OSError: 対象・状態・保存・起動の失敗]
    # }
    def _arm_locked(
        self,
        target_pid: int,
        target_handle: int,
        session_id: str | None,
        *,
        initial_only: bool,
    ) -> str:
        if target_pid <= 0 or target_handle <= 0:
            self._transition(FailSafeState.RECOVERY_REQUIRED, "invalid_target_on_rearm")
            raise RuntimeError("fail-safe re-arm requires a valid target PID/HWND")
        if self._watchdog is not None:
            self._watchdog.stop()
        if initial_only:
            self._require_initial_idle_locked()
        _release_ledger(self.ledger, release_all=True, now=self.clock())
        self._release_inputs()
        if initial_only:
            self._require_initial_idle_locked()
        self._initial_lease_available = False
        self._epoch = uuid4().hex
        self._session_id = session_id or uuid4().hex
        self._target_pid = int(target_pid)
        self._target_handle = int(target_handle)
        self._last_observation_at = 0.0
        self._observation_expires_at = (
            self.clock() + self.config.observation_timeout_seconds
        )
        self._last_sequence = 0
        self._next_sequence = 0
        self._inflight = 0
        self._state = FailSafeState.ACTIVE
        self._reason = "initial_lease" if initial_only else "explicit_rearm"
        self._renew_lease_locked()
        if not self._persist_or_fail(self._reason):
            raise RuntimeError("fail-safe re-arm could not persist critical state")
        self._start_heartbeat_locked()
        if self._watchdog is not None:
            self._watchdog.start()
        if initial_only and (
            self._state != FailSafeState.ACTIVE
            or not self._honor_persisted_stop_locked()
        ):
            raise RuntimeError("initial safety lease was stopped while arming")
        return self._epoch

    def heartbeat(self) -> bool:
        with self._lock:
            if self._state != FailSafeState.ACTIVE or not self._honor_persisted_stop_locked():
                return False
            self._renew_lease_locked()
            return self._write_lease_or_fail("heartbeat_storage_failure")

    # {
    #   責務: [record_observation: 有限正数で未来許容内の観測時刻だけを受理する]
    #   処理: [不正値は受理済み時刻を変更せずRecoveryへ移し、leaseへNaN等を書かない]
    #   引数: [observed_at: clockと同じ秒基準の観測時刻。Noneは現在時刻]
    #   戻り値: [bool: 観測を受理・保存したか。既存Stop latchは解除しない]
    # }
    def record_observation(self, observed_at: float | None = None) -> bool:
        with self._lock:
            if self._state != FailSafeState.ACTIVE or not self._honor_persisted_stop_locked():
                return False
            now = self.clock()
            try:
                timestamp = _finite_time(now if observed_at is None else observed_at)
            except ValueError:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "invalid_observation_timestamp")
                return False
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

    # {
    #   責務: [check_command: finite時刻を含む既存lease/観測/世代/対象の全条件で入力をgateする]
    #   処理: [直接typed commandも時刻を検査し、不正commandではsequence/inflightを前進しない]
    #   引数: [command: 復号済み、または直接作成したcommand] 戻り値: [FailSafeDecision: 許可または固定拒否code]
    # }
    def check_command(self, command: FailSafeCommand) -> FailSafeDecision:
        with self._lock:
            now = self.clock()
            if self._state != FailSafeState.ACTIVE:
                return self._block("runtime_not_active", f"fail-safe runtime is {self._state.value}")
            if not self._honor_persisted_stop_locked():
                return self._block("runtime_not_active", f"fail-safe runtime is {self._state.value}")
            if now > self._lease_expires_at:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "lease_expired")
                return self._block("lease_expired", "control lease expired")
            if self._last_observation_at <= 0 or now > self._observation_expires_at:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "observation_stale")
                return self._block("observation_stale", "fresh observation is required")
            if command.epoch != self._epoch:
                return self._block("stale_epoch", "command epoch does not match active control epoch")
            try:
                _finite_time(command.issued_at)
                _finite_time(command.observation_at)
            except ValueError:
                return self._block("invalid_command_timestamp", "command timestamps must be finite numeric values")
            age = now - command.issued_at
            if age < -0.05 or age > self.config.max_command_age_seconds:
                return self._block("stale_command", "command age exceeds fail-safe limit")
            if command.observation_at != self._last_observation_at:
                return self._block("stale_observation", "command was not created from the latest observation")
            if command.sequence <= self._last_sequence:
                return self._block("stale_sequence", "command sequence is stale or duplicated")
            if command.target_pid != self._target_pid or command.target_handle != self._target_handle:
                self._transition(FailSafeState.RECOVERY_REQUIRED, "target_changed")
                return self._block("target_changed", "command target differs from the re-armed target")
            if command.session_id != self._session_id:
                return self._block("stale_session", "command session does not match active session")
            if self._inflight >= self.config.max_queue_depth:
                return self._block("queue_full", "fail-safe command queue depth exceeded")

            self._last_sequence = command.sequence
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
            if self._state == FailSafeState.ACTIVE and self._honor_persisted_stop_locked():
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
        if self._watchdog is not None:
            self._watchdog.stop()

    def _start_heartbeat_locked(self) -> None:
        self._heartbeat_shutdown.clear()
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
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
            if not self.heartbeat() and self.state != FailSafeState.ACTIVE:
                return

    def _renew_lease_locked(self) -> None:
        self._lease_expires_at = self.clock() + self.config.lease_timeout_seconds

    def _honor_persisted_stop_locked(self) -> bool:
        persisted = self.lease_store.read()
        if not persisted:
            if not self.lease_store.path.exists():
                return True
            self._state = FailSafeState.RECOVERY_REQUIRED
            self._reason = "lease_unreadable"
            self._heartbeat_shutdown.set()
            self._release_inputs()
            try:
                self.journal.record(self._state, self._reason, self._epoch, timestamp=self.clock())
            except Exception:
                pass
            return False
        if str(persisted.get("epoch", "")) != self._epoch:
            return True
        persisted_state = str(persisted.get("state", ""))
        if not persisted_state or persisted_state == FailSafeState.ACTIVE.value:
            return True
        try:
            adopted = FailSafeState(persisted_state)
        except ValueError:
            adopted = FailSafeState.RECOVERY_REQUIRED
        self._state = adopted if adopted != FailSafeState.ACTIVE else FailSafeState.RECOVERY_REQUIRED
        self._reason = str(persisted.get("reason", "external_watchdog_stop"))
        self._heartbeat_shutdown.set()
        self._release_inputs()
        return False

    # {
    #   責務: [_transition: 停止・復旧・closeへの遷移を保存し保持入力を解放する]
    #   処理: [初回lease権利を失効しheartbeatを止め、journalとleaseへ状態を反映する]
    #   引数: [state: 遷移先, reason: 停止理由] 戻り値: [なし]
    #   エラー: [保存失敗は既存fail-closed処理で監査する]
    # }
    def _transition(self, state: FailSafeState, reason: str) -> None:
        self._initial_lease_available = False
        self._state = state
        self._reason = reason
        self._heartbeat_shutdown.set()
        if state != FailSafeState.ACTIVE:
            self._lease_expires_at = 0.0
        self._release_inputs()
        self._persist_or_fail(reason, allow_state_change=False)

    def _release_inputs(self) -> None:
        if self.release_callback is not None:
            try:
                self.release_callback()
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


# {
#   責務: [run_external_watchdog: 親から独立してlease・観測期限を監視し保持入力を解放する]
#   処理: [外部leaseの期限・観測時刻も有限数値に限定し、不正時は期限を失効してRecoveryへ移す]
#   引数: [state_directory: 所有状態directory, max_runtime_seconds: 開発時の実行上限]
#   戻り値: [int: 監視の終了code。通常期限・既存Stopの意味を変更しない]
# }
def run_external_watchdog(state_directory: Path, *, max_runtime_seconds: float | None = None) -> int:
    """Run the dependency-minimal watchdog in a separate process."""

    state_directory = Path(state_directory)
    lease_store = AtomicJsonStore(state_directory / "lease.json")
    journal = FailSafeJournal(state_directory / "journal.json")
    ledger = InputLedger(state_directory / "input_ledger.json")
    started = time.time()

    while True:
        lease = lease_store.read()
        if not lease:
            if lease_store.path.exists():
                _release_ledger(ledger, release_all=True, now=time.time())
                try:
                    journal.record(FailSafeState.RECOVERY_REQUIRED, "lease_unreadable", "")
                except Exception:
                    pass
                return 2
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

        if state != FailSafeState.ACTIVE.value:
            _release_ledger(ledger, release_all=True, now=now)
            return 0

        reason = ""
        if owner_pid <= 0 or not _process_alive(owner_pid):
            reason = "owner_process_lost"
        elif target_pid > 0 and not _process_alive(target_pid):
            reason = "target_process_lost"
        elif os.name == "nt" and target_handle > 0 and not _windows_target_matches(target_handle, target_pid):
            reason = "target_window_lost"
        else:
            try:
                lease_expires = _finite_time(lease.get("lease_expires_at", 0.0))
                observation_expires = _finite_time(lease.get("observation_expires_at", 0.0))
                _finite_time(lease.get("observation_at", 0.0))
            except ValueError:
                reason = "invalid_lease_timestamp"
            else:
                if now > lease_expires:
                    reason = "lease_expired"
                elif observation_expires <= 0 or now > observation_expires:
                    reason = "observation_stale"

        if reason:
            _release_ledger(ledger, release_all=True, now=now)
            updated = dict(lease)
            updated["state"] = FailSafeState.RECOVERY_REQUIRED.value
            updated["reason"] = reason
            updated["lease_expires_at"] = 0.0
            if reason == "invalid_lease_timestamp":
                updated["observation_at"] = 0.0
                updated["observation_expires_at"] = 0.0
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
            return 0

        _release_ledger(ledger, release_all=False, now=now)
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
    raw_keys = snapshot.get("held_keys", {})
    raw_buttons = snapshot.get("held_mouse", {})
    keys = dict(raw_keys) if isinstance(raw_keys, dict) else {}
    buttons = dict(raw_buttons) if isinstance(raw_buttons, dict) else {}
    changed = False

    for raw_key, raw_expiry in list(keys.items()):
        try:
            key = int(raw_key)
            expiry = float(raw_expiry)
        except (TypeError, ValueError):
            keys.pop(raw_key, None)
            changed = True
            continue
        if release_all or expiry <= now:
            _release_key_os(key, mode, target_handle)
            keys.pop(raw_key, None)
            changed = True

    for button, raw_expiry in list(buttons.items()):
        try:
            expiry = float(raw_expiry)
        except (TypeError, ValueError):
            buttons.pop(button, None)
            changed = True
            continue
        if release_all or expiry <= now:
            _release_mouse_os(str(button), mode, target_handle)
            buttons.pop(button, None)
            changed = True

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
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
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