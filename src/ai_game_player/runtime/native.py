"""Thin, optional C ABI v1 binding. Importing this module loads no library."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from ai_game_player.runtime.contracts import RuntimeCapability, RuntimeDescriptor


NATIVE_ABI_VERSION = 1


class NativeStatus(IntEnum):
    OK = 0
    INVALID_ARGUMENT = -1
    UNSUPPORTED_ABI = -2
    ALLOCATION_FAILED = -3
    NOT_IMPLEMENTED = -4


class NativeBatchKind(IntEnum):
    FRAME = 1
    OBSERVATION = 2
    CANDIDATE_SET = 3
    DECISION = 4
    INPUT = 5
    CONTROL_LEASE = 6


class NativeLoadStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"
    LOAD_FAILED = "load_failed"


class NativeRuntimeError(RuntimeError):
    def __init__(self, operation: str, status_code: int) -> None:
        self.operation = operation
        self.status_code = status_code
        try:
            name = NativeStatus(status_code).name
        except ValueError:
            name = "UNKNOWN"
        super().__init__(f"Native Runtime {operation}: {name} (status={status_code})")


class NativeABIError(RuntimeError):
    pass


class NativeContractError(NativeABIError):
    pass


class _Options(ctypes.Structure):
    _fields_ = [("struct_size", ctypes.c_uint32), ("requested_abi_version", ctypes.c_uint32)]


class _Info(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint64),
    ]


class _BatchRequest(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("batch_id", ctypes.c_uint64),
        ("kind", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class _BatchResult(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("batch_id", ctypes.c_uint64),
        ("status", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


@dataclass(frozen=True)
class NativeRuntimeInfo:
    abi_version: int
    capability_bits: int


@dataclass(frozen=True)
class NativeBatchResult:
    batch_id: int
    abi_version: int
    status: NativeStatus


@dataclass(frozen=True)
class NativeLoadResult:
    status: NativeLoadStatus
    runtime: NativeRuntime | None = None
    library_path: Path | None = None
    reason: str = ""


def _bind(library: object, name: str, argtypes: list[Any], restype: Any) -> Any:
    # ctypes' dynamically resolved function edge is confined to this helper.
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise NativeABIError(f"Native Runtime required symbol missing: {name}") from exc
    function.argtypes = argtypes
    function.restype = restype
    return function


def _check(operation: str, status: int) -> None:
    if status != NativeStatus.OK:
        raise NativeRuntimeError(operation, status)


class NativeRuntime:
    """Owns one native instance; serializes batch calls and shutdown."""

    def __init__(self, library: object, *, library_path: Path | None = None) -> None:
        self._library = library  # Retain the DLL for at least the handle lifetime.
        self.library_path = library_path
        self._lock = threading.RLock()
        self._handle = ctypes.c_void_p()
        self._healthy = False
        version = int(_bind(library, "kadoka_runtime_abi_version", [], ctypes.c_uint32)())
        if version != NATIVE_ABI_VERSION:
            raise NativeABIError(f"Native Runtime ABI mismatch: expected {NATIVE_ABI_VERSION}, received {version}")

        query = _bind(library, "kadoka_runtime_query", [ctypes.POINTER(_Info)], ctypes.c_int32)
        init = _bind(library, "kadoka_runtime_init", [ctypes.POINTER(_Options), ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32)
        self._shutdown = _bind(library, "kadoka_runtime_shutdown", [ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32)
        self._process_batch = _bind(library, "kadoka_runtime_process_batch", [ctypes.c_void_p, ctypes.POINTER(_BatchRequest), ctypes.POINTER(_BatchResult)], ctypes.c_int32)
        info = _Info(ctypes.sizeof(_Info), 0, 0)
        _check("query", int(query(ctypes.byref(info))))
        if info.struct_size != ctypes.sizeof(_Info) or info.abi_version != NATIVE_ABI_VERSION:
            raise NativeContractError("Native Runtime returned an incompatible info structure")
        self.info = NativeRuntimeInfo(int(info.abi_version), int(info.capabilities))
        capabilities = frozenset(
            capability for bit, capability in (
                (1, RuntimeCapability.CAPTURE), (2, RuntimeCapability.INPUT),
                (4, RuntimeCapability.SAFETY), (8, RuntimeCapability.FAST_CV),
            ) if self.info.capability_bits & bit
        )
        self._descriptor = RuntimeDescriptor("native-cabi-v1", "cpp", capabilities=capabilities)
        options = _Options(ctypes.sizeof(_Options), NATIVE_ABI_VERSION)
        _check("init", int(init(ctypes.byref(options), ctypes.byref(self._handle))))
        if not self._handle.value:
            raise NativeContractError("Native Runtime init succeeded without a handle")
        self._healthy = True

    @classmethod
    def load(cls, path: str | Path) -> NativeRuntime:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise OSError(f"Native Runtime library is not a file: {resolved}")
        if sys.platform == "win32":
            # Search dependencies beside this DLL or in trusted default locations.
            library = ctypes.CDLL(str(resolved), winmode=0x00000100 | 0x00001000)
        else:
            library = ctypes.CDLL(str(resolved))
        return cls(library, library_path=resolved)

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._descriptor

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy and bool(self._handle.value)

    def process_batch(self, kind: NativeBatchKind, batch_id: int) -> NativeBatchResult:
        if not isinstance(kind, NativeBatchKind):
            raise ValueError("batch kind must be NativeBatchKind")
        if isinstance(batch_id, bool) or not isinstance(batch_id, int) or not 0 <= batch_id <= (1 << 64) - 1:
            raise ValueError("batch_id must be an unsigned 64-bit integer")
        with self._lock:
            if not self.is_healthy():
                raise NativeRuntimeError("process_batch (closed/unhealthy)", NativeStatus.INVALID_ARGUMENT)
            request = _BatchRequest(ctypes.sizeof(_BatchRequest), NATIVE_ABI_VERSION, batch_id, int(kind), 0)
            result = _BatchResult(ctypes.sizeof(_BatchResult), 0, 0, 0, 0)
            status = int(self._process_batch(self._handle, ctypes.byref(request), ctypes.byref(result)))
            # Only OK and NOT_IMPLEMENTED promise a populated result in ABI v1.
            if status in (NativeStatus.OK, NativeStatus.NOT_IMPLEMENTED):
                if (result.struct_size != ctypes.sizeof(_BatchResult) or result.abi_version != NATIVE_ABI_VERSION
                        or result.batch_id != batch_id or result.status != status or result.reserved != 0):
                    self._healthy = False
                    raise NativeContractError("Native Runtime returned an invalid batch result")
            _check("process_batch", status)
            return NativeBatchResult(int(result.batch_id), int(result.abi_version), NativeStatus(status))

    def close(self) -> None:
        with self._lock:
            if not self._handle.value:
                return
            self._healthy = False
            _check("shutdown", int(self._shutdown(ctypes.byref(self._handle))))
            if self._handle.value:
                raise NativeContractError("Native Runtime shutdown did not release the handle")

    def __enter__(self) -> NativeRuntime:
        if not self.is_healthy():
            raise NativeRuntimeError("enter (closed/unhealthy)", NativeStatus.INVALID_ARGUMENT)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()


def discover_native_runtime(path: str | Path | None = None) -> NativeLoadResult:
    """Return availability explicitly; never substitute an incompatible library."""
    configured = path if path is not None else os.environ.get("KADOKA_NATIVE_RUNTIME")
    try:
        if path is not None and not str(path):
            raise ValueError("Native Runtime explicit path must not be empty")
        if configured:
            candidates = [Path(configured).expanduser().resolve()]
        else:
            name = {"win32": "kadoka_native_runtime.dll", "darwin": "libkadoka_native_runtime.dylib"}.get(sys.platform, "libkadoka_native_runtime.so")
            executable_dir = Path(sys.executable).resolve().parent
            candidates = [executable_dir / name]
            bundle_dir = getattr(sys, "_MEIPASS", None)
            if bundle_dir:
                candidates.append(Path(bundle_dir).resolve() / name)
    except (OSError, RuntimeError, ValueError) as exc:
        return NativeLoadResult(NativeLoadStatus.LOAD_FAILED, reason=str(exc))
    for candidate in dict.fromkeys(candidates):
        try:
            runtime = NativeRuntime.load(candidate)
        except FileNotFoundError:
            continue
        except NativeABIError as exc:
            return NativeLoadResult(NativeLoadStatus.INCOMPATIBLE, library_path=candidate, reason=str(exc))
        except NativeRuntimeError as exc:
            status = NativeLoadStatus.INCOMPATIBLE if exc.status_code == NativeStatus.UNSUPPORTED_ABI else NativeLoadStatus.LOAD_FAILED
            return NativeLoadResult(status, library_path=candidate, reason=str(exc))
        except (OSError, ValueError) as exc:
            return NativeLoadResult(NativeLoadStatus.LOAD_FAILED, library_path=candidate, reason=str(exc))
        return NativeLoadResult(NativeLoadStatus.AVAILABLE, runtime, candidate)
    return NativeLoadResult(NativeLoadStatus.UNAVAILABLE, reason="Native Runtime library not found")
