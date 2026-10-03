import ctypes
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_game_player.runtime import native as abi
from ai_game_player.runtime import RuntimeCapability, RuntimeRegistry


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls += 1
        return self.callback(*args)


class FakeLibrary:
    def __init__(self):
        self.version = 1
        self.query_status = 0
        self.info_abi = 1
        self.info_size = ctypes.sizeof(abi._Info)
        self.capabilities = 4
        self.init_status = 0
        self.null_handle = False
        self.shutdown_status = 0
        self.release_handle = True
        self.batch_status = 0
        self.corrupt_result = {}
        self.requests = []
        self.kadoka_runtime_abi_version = FakeFunction(lambda: self.version)
        self.kadoka_runtime_query = FakeFunction(self.query)
        self.kadoka_runtime_init = FakeFunction(self.init)
        self.kadoka_runtime_shutdown = FakeFunction(self.shutdown)
        self.kadoka_runtime_process_batch = FakeFunction(self.batch)

    def query(self, pointer):
        info = ctypes.cast(pointer, ctypes.POINTER(abi._Info)).contents
        assert info.struct_size == ctypes.sizeof(abi._Info)
        if self.query_status == 0:
            info.struct_size = self.info_size
            info.abi_version = self.info_abi
            info.capabilities = self.capabilities
        return self.query_status

    def init(self, options_pointer, handle_pointer):
        options = ctypes.cast(options_pointer, ctypes.POINTER(abi._Options)).contents
        handle = ctypes.cast(handle_pointer, ctypes.POINTER(ctypes.c_void_p))
        assert options.struct_size == ctypes.sizeof(abi._Options)
        assert options.requested_abi_version == 1
        assert handle[0] is None
        if self.init_status == 0 and not self.null_handle:
            handle[0] = 0x12345678
        return self.init_status

    def shutdown(self, handle_pointer):
        handle = ctypes.cast(handle_pointer, ctypes.POINTER(ctypes.c_void_p))
        assert handle[0] == 0x12345678
        if self.shutdown_status == 0 and self.release_handle:
            handle[0] = None
        return self.shutdown_status

    def batch(self, handle, request_pointer, result_pointer):
        assert handle.value == 0x12345678
        request = ctypes.cast(request_pointer, ctypes.POINTER(abi._BatchRequest)).contents
        result = ctypes.cast(result_pointer, ctypes.POINTER(abi._BatchResult)).contents
        assert request.struct_size == ctypes.sizeof(abi._BatchRequest)
        assert request.abi_version == 1
        assert request.reserved == 0
        assert result.struct_size == ctypes.sizeof(abi._BatchResult)
        self.requests.append((request.kind, request.batch_id))
        if self.batch_status in (0, -4):
            result.abi_version = 1
            result.batch_id = request.batch_id
            result.status = self.batch_status
            for field, value in self.corrupt_result.items():
                setattr(result, field, value)
        return self.batch_status


class NativeRuntimeTest(unittest.TestCase):
    def test_version_rejected_before_query_or_init(self):
        library = FakeLibrary()
        library.version = 2
        with self.assertRaises(abi.NativeABIError):
            abi.NativeRuntime(library)
        self.assertEqual(0, library.kadoka_runtime_query.calls)
        self.assertEqual(0, library.kadoka_runtime_init.calls)

    def test_missing_required_symbol_rejected_before_init(self):
        library = FakeLibrary()
        del library.kadoka_runtime_process_batch
        with self.assertRaisesRegex(abi.NativeABIError, "required symbol missing"):
            abi.NativeRuntime(library)
        self.assertEqual(0, library.kadoka_runtime_init.calls)

    def test_query_structure_rejected_before_init(self):
        for field, value in (("info_size", 0), ("info_abi", 2)):
            with self.subTest(field=field):
                library = FakeLibrary()
                setattr(library, field, value)
                with self.assertRaises(abi.NativeContractError):
                    abi.NativeRuntime(library)
                self.assertEqual(0, library.kadoka_runtime_init.calls)

    def test_query_and_init_error_mapping_preserves_unknown_codes(self):
        for operation, status in (("query", -2), ("init", -3), ("init", -987)):
            with self.subTest(operation=operation, status=status):
                library = FakeLibrary()
                setattr(library, operation + "_status", status)
                with self.assertRaises(abi.NativeRuntimeError) as caught:
                    abi.NativeRuntime(library)
                self.assertEqual(operation, caught.exception.operation)
                self.assertEqual(status, caught.exception.status_code)
                self.assertIn(str(status), str(caught.exception))

    def test_successful_init_requires_non_null_handle(self):
        library = FakeLibrary()
        library.null_handle = True
        with self.assertRaises(abi.NativeContractError):
            abi.NativeRuntime(library)

    def test_capability_mapping_preserves_unknown_bits_without_advertising_them(self):
        library = FakeLibrary()
        library.capabilities = 15 | (1 << 63)
        with abi.NativeRuntime(library) as runtime:
            self.assertEqual(library.capabilities, runtime.info.capability_bits)
            self.assertEqual(frozenset(RuntimeCapability), runtime.descriptor.capabilities)

    def test_prototypes_set_before_calls(self):
        library = FakeLibrary()
        with abi.NativeRuntime(library):
            self.assertEqual([], library.kadoka_runtime_abi_version.argtypes)
            self.assertIs(ctypes.c_uint32, library.kadoka_runtime_abi_version.restype)
            self.assertEqual([ctypes.POINTER(abi._Options), ctypes.POINTER(ctypes.c_void_p)], library.kadoka_runtime_init.argtypes)
            self.assertEqual([ctypes.POINTER(ctypes.c_void_p)], library.kadoka_runtime_shutdown.argtypes)
            self.assertEqual([ctypes.c_void_p, ctypes.POINTER(abi._BatchRequest), ctypes.POINTER(abi._BatchResult)], library.kadoka_runtime_process_batch.argtypes)
            self.assertIs(ctypes.c_int32, library.kadoka_runtime_process_batch.restype)

    def test_context_exception_releases_once_and_propagates(self):
        library = FakeLibrary()
        runtime = abi.NativeRuntime(library)
        with self.assertRaisesRegex(ValueError, "user failure"):
            with runtime:
                raise ValueError("user failure")
        runtime.close()
        self.assertFalse(runtime.is_healthy())
        self.assertEqual(1, library.kadoka_runtime_shutdown.calls)

    def test_closed_backend_is_not_resolved_or_called(self):
        library = FakeLibrary()
        runtime = abi.NativeRuntime(library)
        registry = RuntimeRegistry()
        registry.register(runtime)
        self.assertIs(runtime, registry.resolve({RuntimeCapability.SAFETY}))
        runtime.close()
        with self.assertRaises(LookupError):
            registry.resolve({RuntimeCapability.SAFETY})
        with self.assertRaises(abi.NativeRuntimeError):
            runtime.process_batch(abi.NativeBatchKind.FRAME, 1)
        with self.assertRaises(abi.NativeRuntimeError):
            runtime.__enter__()
        self.assertEqual(0, library.kadoka_runtime_process_batch.calls)

    def test_shutdown_error_retains_handle_for_retry_but_disables_batch(self):
        library = FakeLibrary()
        runtime = abi.NativeRuntime(library)
        library.shutdown_status = -1
        with self.assertRaises(abi.NativeRuntimeError) as caught:
            runtime.close()
        self.assertEqual("shutdown", caught.exception.operation)
        self.assertFalse(runtime.is_healthy())
        with self.assertRaises(abi.NativeRuntimeError):
            runtime.process_batch(abi.NativeBatchKind.INPUT, 0)
        library.shutdown_status = 0
        runtime.close()
        self.assertEqual(2, library.kadoka_runtime_shutdown.calls)

    def test_shutdown_success_without_release_is_contract_failure(self):
        library = FakeLibrary()
        runtime = abi.NativeRuntime(library)
        library.release_handle = False
        with self.assertRaises(abi.NativeContractError):
            runtime.close()
        self.assertFalse(runtime.is_healthy())
        library.release_handle = True
        runtime.close()

    def test_invalid_batch_arguments_never_cross_ffi(self):
        library = FakeLibrary()
        with abi.NativeRuntime(library) as runtime:
            for batch_id in (-1, 1 << 64, True, 0.5, "1"):
                with self.subTest(batch_id=batch_id), self.assertRaises(ValueError):
                    runtime.process_batch(abi.NativeBatchKind.FRAME, batch_id)
            with self.assertRaises(ValueError):
                runtime.process_batch(1, 1)
            self.assertEqual(0, library.kadoka_runtime_process_batch.calls)

    def test_batch_mapping_accepts_full_unsigned_64_bit_range(self):
        library = FakeLibrary()
        with abi.NativeRuntime(library) as runtime:
            for batch_id in (0, 1 << 63, (1 << 64) - 1):
                result = runtime.process_batch(abi.NativeBatchKind.DECISION, batch_id)
                self.assertEqual(abi.NativeBatchResult(batch_id, 1, abi.NativeStatus.OK), result)
        self.assertEqual([(4, 0), (4, 1 << 63), (4, (1 << 64) - 1)], library.requests)

    def test_batch_error_mapping_and_placeholder_remain_explicit(self):
        library = FakeLibrary()
        with abi.NativeRuntime(library) as runtime:
            for status in (-1, -2, -3, -4, -999):
                with self.subTest(status=status):
                    library.batch_status = status
                    with self.assertRaises(abi.NativeRuntimeError) as caught:
                        runtime.process_batch(abi.NativeBatchKind.FRAME, 9)
                    self.assertEqual(status, caught.exception.status_code)
            self.assertTrue(runtime.is_healthy())

    def test_corrupted_batch_result_invalidates_backend(self):
        for status in (0, -4):
            for field, value in (("struct_size", 0), ("abi_version", 2), ("batch_id", 3), ("status", -987), ("reserved", 1)):
                with self.subTest(status=status, field=field):
                    library = FakeLibrary()
                    library.batch_status = status
                    library.corrupt_result = {field: value}
                    with abi.NativeRuntime(library) as runtime:
                        with self.assertRaises(abi.NativeContractError):
                            runtime.process_batch(abi.NativeBatchKind.FRAME, 9)
                        self.assertFalse(runtime.is_healthy())
                        with self.assertRaises(abi.NativeRuntimeError):
                            runtime.process_batch(abi.NativeBatchKind.FRAME, 10)
                    self.assertEqual(1, library.kadoka_runtime_shutdown.calls)

    def test_shutdown_waits_for_in_flight_batch(self):
        library = FakeLibrary()
        runtime = abi.NativeRuntime(library)
        self.addCleanup(runtime.close)
        entered = threading.Event()
        release = threading.Event()
        closing = threading.Event()
        closed = threading.Event()
        failures = []
        original_batch = library.kadoka_runtime_process_batch.callback

        def blocking_batch(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test batch was not released")
            return original_batch(*args)

        def work():
            try:
                runtime.process_batch(abi.NativeBatchKind.FRAME, 1)
            except Exception as exc:
                failures.append(exc)

        def close():
            closing.set()
            try:
                runtime.close()
            except Exception as exc:
                failures.append(exc)
            finally:
                closed.set()

        library.kadoka_runtime_process_batch.callback = blocking_batch
        worker = threading.Thread(target=work)
        closer = threading.Thread(target=close)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            closer.start()
            self.assertTrue(closing.wait(5))
            self.assertFalse(closed.wait(0.05))
            self.assertEqual(0, library.kadoka_runtime_shutdown.calls)
        finally:
            release.set()
            worker.join(5)
            if closer.ident is not None:
                closer.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, library.kadoka_runtime_shutdown.calls)


class NativeDiscoveryTest(unittest.TestCase):
    def test_path_resolution_errors_are_reported(self):
        with patch.object(Path, "resolve", side_effect=OSError("path inaccessible")):
            result = abi.discover_native_runtime("configured-runtime")
        self.assertEqual(abi.NativeLoadStatus.LOAD_FAILED, result.status)
        self.assertIn("path inaccessible", result.reason)
        self.assertEqual(abi.NativeLoadStatus.LOAD_FAILED, abi.discover_native_runtime("").status)

    def test_missing_explicit_library_does_not_fall_back(self):
        with patch.object(abi.NativeRuntime, "load", side_effect=FileNotFoundError) as loader:
            result = abi.discover_native_runtime("missing-runtime")
        self.assertEqual(abi.NativeLoadStatus.UNAVAILABLE, result.status)
        self.assertIsNone(result.runtime)
        self.assertEqual(1, loader.call_count)

    def test_configured_environment_path_and_explicit_override(self):
        with patch.dict(os.environ, {"KADOKA_NATIVE_RUNTIME": "configured-runtime"}):
            with patch.object(abi.NativeRuntime, "load", side_effect=FileNotFoundError) as loader:
                abi.discover_native_runtime()
                abi.discover_native_runtime("explicit-runtime")
        self.assertEqual([Path("configured-runtime").resolve(), Path("explicit-runtime").resolve()], [call.args[0] for call in loader.call_args_list])

    def test_bundle_discovery_never_searches_working_directory_or_path(self):
        runtime = abi.NativeRuntime(FakeLibrary())
        self.addCleanup(runtime.close)
        with patch.dict(os.environ, {"KADOKA_NATIVE_RUNTIME": ""}), patch.object(sys, "executable", str(Path("python-bin/python").resolve())), patch.object(sys, "_MEIPASS", str(Path("bundle").resolve()), create=True):
            with patch.object(abi.NativeRuntime, "load", side_effect=[FileNotFoundError, runtime]) as loader:
                result = abi.discover_native_runtime()
        self.assertEqual(abi.NativeLoadStatus.AVAILABLE, result.status)
        self.assertIs(runtime, result.runtime)
        paths = [call.args[0] for call in loader.call_args_list]
        self.assertEqual([Path("python-bin").resolve(), Path("bundle").resolve()], [path.parent for path in paths])
        self.assertEqual(paths[1], result.library_path)

    def test_discovery_reports_incompatibility_or_failure_without_fallback(self):
        for error, status in (
            (abi.NativeABIError("wrong version"), abi.NativeLoadStatus.INCOMPATIBLE),
            (abi.NativeContractError("wrong layout"), abi.NativeLoadStatus.INCOMPATIBLE),
            (abi.NativeRuntimeError("init", -2), abi.NativeLoadStatus.INCOMPATIBLE),
            (abi.NativeRuntimeError("init", -3), abi.NativeLoadStatus.LOAD_FAILED),
            (OSError("invalid library"), abi.NativeLoadStatus.LOAD_FAILED),
            (ValueError("invalid path"), abi.NativeLoadStatus.LOAD_FAILED),
        ):
            with self.subTest(error=error), patch.object(abi.NativeRuntime, "load", side_effect=error) as loader:
                result = abi.discover_native_runtime("configured-runtime")
                self.assertEqual(status, result.status)
                self.assertIsNone(result.runtime)
                self.assertIn(str(error), result.reason)
                self.assertEqual(1, loader.call_count)

    def test_load_uses_absolute_path_and_safe_windows_dependency_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime-library"
            path.touch()
            for platform in ("win32", "linux", "darwin"):
                with self.subTest(platform=platform), patch.object(sys, "platform", platform), patch.object(ctypes, "CDLL", return_value=FakeLibrary()) as loader:
                    with abi.NativeRuntime.load(path) as runtime:
                        self.assertEqual(path.resolve(), runtime.library_path)
                    kwargs = {"winmode": 0x1100} if platform == "win32" else {}
                    loader.assert_called_once_with(str(path.resolve()), **kwargs)

    def test_directory_is_not_loadable(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(ctypes, "CDLL") as loader:
            with self.assertRaises(OSError):
                abi.NativeRuntime.load(temp_dir)
            loader.assert_not_called()


@unittest.skipUnless(os.environ.get("KADOKA_NATIVE_RUNTIME"), "built Native Runtime not configured")
class NativeRuntimeIntegrationTest(unittest.TestCase):
    def test_real_library_lifecycle_query_and_all_metadata_batches(self):
        # A configured library must load; do not silently skip ABI/load failures.
        result = abi.discover_native_runtime()
        self.assertEqual(abi.NativeLoadStatus.AVAILABLE, result.status, result.reason)
        runtime = result.runtime
        self.assertIsNotNone(runtime)
        self.addCleanup(runtime.close)
        with abi.NativeRuntime.load(os.environ["KADOKA_NATIVE_RUNTIME"]) as independent:
            self.assertEqual(1, runtime.info.abi_version)
            self.assertEqual(frozenset({RuntimeCapability.SAFETY}), runtime.descriptor.capabilities)
            registry = RuntimeRegistry()
            registry.register(runtime)
            self.assertIs(runtime, registry.resolve({RuntimeCapability.SAFETY}))
            for kind in abi.NativeBatchKind:
                with self.subTest(kind=kind), self.assertRaises(abi.NativeRuntimeError) as caught:
                    runtime.process_batch(kind, (1 << 64) - 1)
                self.assertEqual(abi.NativeStatus.NOT_IMPLEMENTED, caught.exception.status_code)
            runtime.close()
            runtime.close()
            self.assertTrue(independent.is_healthy())
            self.assertFalse(runtime.is_healthy())
            with self.assertRaises(LookupError):
                registry.resolve({RuntimeCapability.SAFETY})

    def test_real_context_releases_on_exception(self):
        runtime = abi.NativeRuntime.load(os.environ["KADOKA_NATIVE_RUNTIME"])
        self.addCleanup(runtime.close)
        with self.assertRaisesRegex(ValueError, "integration block failure"):
            with runtime:
                raise ValueError("integration block failure")
        self.assertFalse(runtime.is_healthy())
        runtime.close()


if __name__ == "__main__":
    unittest.main()
