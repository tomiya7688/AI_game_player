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

from ai_game_player.frame_preprocessor import FramePrimitiveBatch
from ai_game_player.models import DetectedElement
from ai_game_player.runtime.contracts import RuntimeCapability, RuntimeDescriptor
from ai_game_player.screen_capture import ScreenFrame


NATIVE_ABI_VERSION = 1
CAP_FAST_CV = 1 << 3


class NativeStatus(IntEnum):
    OK = 0
    INVALID_ARGUMENT = -1
    UNSUPPORTED_ABI = -2
    ALLOCATION_FAILED = -3
    NOT_IMPLEMENTED = -4
    BUFFER_TOO_SMALL = -5


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


class _FrameInput(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("stride_bytes", ctypes.c_uint32),
        ("brightness_threshold", ctypes.c_uint32),
        ("min_region_pixels", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("bgra_size", ctypes.c_uint64),
        ("bgra", ctypes.POINTER(ctypes.c_uint8)),
    ]


class _FrameRegion(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int32),
        ("y", ctypes.c_int32),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
        ("pixel_count", ctypes.c_uint64),
    ]


class _FrameResult(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("mean_red", ctypes.c_uint32),
        ("mean_green", ctypes.c_uint32),
        ("mean_blue", ctypes.c_uint32),
        ("mean_brightness", ctypes.c_uint32),
        ("perceptual_hash", ctypes.c_uint64),
        ("region_count", ctypes.c_uint32),
        ("region_capacity", ctypes.c_uint32),
        ("regions", ctypes.POINTER(_FrameRegion)),
        ("input_bytes_read", ctypes.c_uint64),
        ("output_bytes_written", ctypes.c_uint64),
        ("input_copy_count", ctypes.c_uint64),
        ("input_copy_bytes", ctypes.c_uint64),
        ("processing_ns", ctypes.c_uint64),
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
        self._preprocess_frame = None
        info = _Info(ctypes.sizeof(_Info), 0, 0)
        _check("query", int(query(ctypes.byref(info))))
        if info.struct_size != ctypes.sizeof(_Info) or info.abi_version != NATIVE_ABI_VERSION:
            raise NativeContractError("Native Runtime returned an incompatible info structure")
        self.info = NativeRuntimeInfo(int(info.abi_version), int(info.capabilities))
        if self.info.capability_bits & CAP_FAST_CV:
            self._preprocess_frame = _bind(
                library,
                "kadoka_runtime_preprocess_frame",
                [ctypes.c_void_p, ctypes.POINTER(_FrameInput), ctypes.POINTER(_FrameResult)],
                ctypes.c_int32,
            )
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

    def preprocess(
        self,
        frame: ScreenFrame,
        *,
        brightness_threshold: int = 220,
        min_region_pixels: int = 9,
    ) -> FramePrimitiveBatch:
        if (
            isinstance(frame.width, bool) or not isinstance(frame.width, int)
            or isinstance(frame.height, bool) or not isinstance(frame.height, int)
            or frame.width <= 0 or frame.height <= 0
            or len(frame.bgra) != frame.width * frame.height * 4
        ):
            raise ValueError("BGRA buffer size does not match frame dimensions")
        if self._preprocess_frame is None:
            raise NativeRuntimeError("preprocess_frame (unsupported capability)", NativeStatus.NOT_IMPLEMENTED)
        if (
            frame.width > 0x7FFFFFFF or frame.height > 0x7FFFFFFF
            or frame.width * 4 > 0xFFFFFFFF
            or isinstance(brightness_threshold, bool) or not isinstance(brightness_threshold, int)
            or not 0 <= brightness_threshold <= 255
            or isinstance(min_region_pixels, bool) or not isinstance(min_region_pixels, int)
            or not 1 <= min_region_pixels <= 0xFFFFFFFF
        ):
            raise ValueError("frame preprocessing parameters are outside the C ABI range")

        pixel_count = frame.width * frame.height
        initial_capacity = min(256, pixel_count // min_region_pixels)
        buffer = ctypes.c_char_p(frame.bgra)
        data_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
        with self._lock:
            if not self.is_healthy():
                raise NativeRuntimeError("preprocess_frame (closed/unhealthy)", NativeStatus.INVALID_ARGUMENT)
            capacity = initial_capacity
            total_processing_ns = 0
            total_input_bytes_read = 0
            total_output_bytes_written = 0
            while True:
                regions = (_FrameRegion * capacity)() if capacity else None
                region_pointer = (
                    ctypes.cast(regions, ctypes.POINTER(_FrameRegion))
                    if regions is not None else ctypes.POINTER(_FrameRegion)()
                )
                request = _FrameInput(
                    ctypes.sizeof(_FrameInput),
                    NATIVE_ABI_VERSION,
                    frame.width,
                    frame.height,
                    frame.width * 4,
                    brightness_threshold,
                    min_region_pixels,
                    0,
                    len(frame.bgra),
                    data_pointer,
                )
                result = _FrameResult()
                result.struct_size = ctypes.sizeof(_FrameResult)
                result.abi_version = NATIVE_ABI_VERSION
                result.region_capacity = capacity
                result.regions = region_pointer
                status = int(self._preprocess_frame(self._handle, ctypes.byref(request), ctypes.byref(result)))
                if status == NativeStatus.BUFFER_TOO_SMALL:
                    if not self._valid_frame_result(
                        result, capacity, pixel_count, frame.width, frame.height,
                        min_region_pixels, regions, buffer_bytes=len(frame.bgra), buffer_too_small=True,
                    ):
                        self._healthy = False
                        raise NativeContractError("Native Runtime returned an invalid frame result")
                    total_processing_ns += int(result.processing_ns)
                    total_input_bytes_read += int(result.input_bytes_read)
                    total_output_bytes_written += int(result.output_bytes_written)
                    capacity = int(result.region_count)
                    continue
                if status in (NativeStatus.OK,):
                    if not self._valid_frame_result(
                        result, capacity, pixel_count, frame.width, frame.height,
                        min_region_pixels, regions, buffer_bytes=len(frame.bgra), buffer_too_small=False,
                    ):
                        self._healthy = False
                        raise NativeContractError("Native Runtime returned an invalid frame result")
                _check("preprocess_frame", status)
                total_processing_ns += int(result.processing_ns)
                total_input_bytes_read += int(result.input_bytes_read)
                total_output_bytes_written += int(result.output_bytes_written)
                detected = tuple(
                    DetectedElement(
                        f"bright-region-{index}",
                        "region",
                        (int(region.x), int(region.y), int(region.width), int(region.height)),
                        "bright_region",
                        round(0.5 + 0.2 * int(region.pixel_count) / (int(region.width) * int(region.height)), 2),
                    )
                    for index, region in enumerate(() if regions is None else regions[:result.region_count])
                )
                return FramePrimitiveBatch(
                    mean_rgb={
                        "r": int(result.mean_red),
                        "g": int(result.mean_green),
                        "b": int(result.mean_blue),
                    },
                    mean_brightness=int(result.mean_brightness),
                    perceptual_hash=f"{int(result.perceptual_hash):016x}",
                    detected_elements=detected,
                    processing_ns=total_processing_ns,
                    input_bytes_read=total_input_bytes_read,
                    output_bytes_written=total_output_bytes_written,
                    input_copy_count=int(result.input_copy_count),
                    input_copy_bytes=int(result.input_copy_bytes),
                )

    @staticmethod
    def _valid_frame_result(
        result: _FrameResult,
        capacity: int,
        pixel_count: int,
        frame_width: int,
        frame_height: int,
        min_region_pixels: int,
        regions: Any,
        *,
        buffer_bytes: int,
        buffer_too_small: bool,
    ) -> bool:
        if (
            result.struct_size != ctypes.sizeof(_FrameResult)
            or result.abi_version != NATIVE_ABI_VERSION
            or result.region_capacity != capacity
            or result.reserved != 0
            or any(value > 255 for value in (
                result.mean_red, result.mean_green, result.mean_blue, result.mean_brightness,
            ))
            or result.input_bytes_read != buffer_bytes
            or result.input_copy_count != 0
            or result.input_copy_bytes != 0
            or result.region_count > pixel_count // min_region_pixels
        ):
            return False
        expected_pointer = (
            ctypes.cast(regions, ctypes.c_void_p).value
            if regions is not None else None
        )
        actual_pointer = ctypes.cast(result.regions, ctypes.c_void_p).value
        if actual_pointer != expected_pointer:
            return False
        if buffer_too_small:
            return bool(result.region_count > capacity and result.output_bytes_written == 0)
        if result.region_count > capacity:
            return False
        expected_output = result.region_count * ctypes.sizeof(_FrameRegion)
        if result.output_bytes_written != expected_output:
            return False
        if result.region_count and not result.regions:
            return False
        for index in range(result.region_count):
            region = result.regions[index]
            if (
                region.reserved != 0
                or region.x < 0 or region.y < 0
                or region.width < 3 or region.height < 3
                or region.x + region.width > frame_width
                or region.y + region.height > frame_height
                or region.pixel_count < min_region_pixels
                or region.pixel_count > region.width * region.height
            ):
                return False
        return True

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
