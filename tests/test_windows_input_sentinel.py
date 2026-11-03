import ctypes
import os
import unittest

from tools.windows_input_sentinel import (
    GLOBAL_INPUT_FUNCTIONS,
    GuardedFunction,
    InputSentinel,
    InputSentinelViolation,
    is_input_message,
    is_user32_library,
    windows_owner_process_id_of,
)

TEST_PROCESS_ID = 4242
OWNED_WINDOW = 0x1001
FOREIGN_WINDOW = 0x2002


class RecordingFunction:
    def __init__(self, result=1):
        self.calls = []
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.result


def make_sentinel():
    owners = {OWNED_WINDOW: TEST_PROCESS_ID, FOREIGN_WINDOW: 7}
    return InputSentinel(
        lambda handle: owners.get(handle, 0), current_process_id=TEST_PROCESS_ID
    )


class InputSentinelPolicyTest(unittest.TestCase):
    def test_global_input_apis_are_blocked_before_the_real_function_runs(self):
        for name in sorted(GLOBAL_INPUT_FUNCTIONS):
            with self.subTest(function=name):
                sentinel = make_sentinel()
                real = RecordingFunction()
                guarded = sentinel.guard(name, real)

                with self.assertRaises(InputSentinelViolation):
                    guarded(0, 0, 0, 0)

                self.assertEqual(real.calls, [])
                self.assertEqual(
                    [event.reason for event in sentinel.blocked], ["global-input-api"]
                )

    def test_input_messages_to_foreign_or_unknown_windows_are_blocked(self):
        for window, expected_owner in ((FOREIGN_WINDOW, 7), (0x9999, 0), (0, None)):
            with self.subTest(window=window):
                sentinel = make_sentinel()
                real = RecordingFunction()
                guarded = sentinel.guard("PostMessageW", real)

                with self.assertRaises(InputSentinelViolation):
                    guarded(window, 0x0100, 0x41, 0)

                self.assertEqual(real.calls, [])
                event = sentinel.blocked[0]
                self.assertEqual(event.reason, "foreign-window-input")
                self.assertEqual(event.window_handle, window)
                self.assertEqual(
                    event.owner_process_id,
                    0 if expected_owner is None else expected_owner,
                )

    def test_input_messages_to_windows_owned_by_the_test_process_pass_through(self):
        sentinel = make_sentinel()
        real = RecordingFunction()
        guarded = sentinel.guard("PostMessageW", real)

        self.assertEqual(guarded(ctypes.c_void_p(OWNED_WINDOW), 0x0201, 1, 0), 1)

        self.assertEqual(len(real.calls), 1)
        self.assertEqual(sentinel.blocked, [])
        self.assertEqual(sentinel.allowed[0].reason, "owned-test-window")

    def test_non_input_messages_such_as_wm_print_are_not_restricted(self):
        sentinel = make_sentinel()
        real = RecordingFunction()
        guarded = sentinel.guard("SendMessageTimeoutW", real)

        guarded(FOREIGN_WINDOW, 0x0317, 0, 0, 0, 100, None)

        self.assertEqual(len(real.calls), 1)
        self.assertEqual(sentinel.blocked, [])
        self.assertEqual(sentinel.allowed, [])

    def test_blocked_call_is_recorded_even_when_the_caller_swallows_the_exception(self):
        sentinel = make_sentinel()
        guarded = sentinel.guard("mouse_event", RecordingFunction())

        try:
            guarded(0x0004, 0, 0, 0, 0)
        except Exception:
            pass

        self.assertEqual(len(sentinel.blocked), 1)

    def test_message_call_without_message_argument_is_blocked(self):
        sentinel = make_sentinel()
        guarded = sentinel.guard("SendMessageW", RecordingFunction())

        with self.assertRaises(InputSentinelViolation):
            guarded(OWNED_WINDOW)

        self.assertEqual(sentinel.blocked[0].reason, "unparsable-message-call")

    def test_unrelated_functions_are_returned_unwrapped(self):
        sentinel = make_sentinel()
        real = RecordingFunction()

        self.assertIs(sentinel.guard("GetWindowRect", real), real)
        self.assertIsInstance(sentinel.guard("SendInput", real), GuardedFunction)

    def test_prototype_attributes_are_delegated_to_the_real_function(self):
        sentinel = make_sentinel()
        real = RecordingFunction()
        guarded = sentinel.guard("keybd_event", real)

        guarded.argtypes = [ctypes.c_ubyte]
        guarded.restype = None

        self.assertEqual(real.argtypes, [ctypes.c_ubyte])
        self.assertEqual(guarded.argtypes, [ctypes.c_ubyte])
        self.assertIsNone(guarded.restype)

    def test_install_and_uninstall_restore_the_previous_ctypes_lookup(self):
        before = ctypes.CDLL.__getitem__
        sentinel = make_sentinel()

        sentinel.install()
        try:
            self.assertIsNot(ctypes.CDLL.__getitem__, before)
            with self.assertRaises(RuntimeError):
                sentinel.install()
        finally:
            sentinel.uninstall()

        self.assertIs(ctypes.CDLL.__getitem__, before)

    def test_nested_sentinel_judges_before_an_outer_wrapper(self):
        outer = make_sentinel()
        inner = make_sentinel()
        real = RecordingFunction()
        wrapped_twice = inner.guard("SendInput", outer.guard("SendInput", real))

        with self.assertRaises(InputSentinelViolation):
            wrapped_twice(1, None, 0)

        self.assertEqual(len(inner.blocked), 1)
        self.assertEqual(outer.blocked, [])
        self.assertEqual(real.calls, [])

    def test_out_of_order_uninstall_keeps_the_later_sentinel_installed(self):
        before = ctypes.CDLL.__getitem__
        first = make_sentinel()
        second = make_sentinel()
        first.install()
        second.install()
        try:
            with self.assertRaises(RuntimeError):
                first.uninstall()
            self.assertIsNot(ctypes.CDLL.__getitem__, before)
        finally:
            second.uninstall()
            first.uninstall()

        self.assertIs(ctypes.CDLL.__getitem__, before)

    def test_owner_lookup_failure_fails_closed_and_is_recorded(self):
        def broken_lookup(_handle):
            raise OSError("lookup failed")

        sentinel = InputSentinel(broken_lookup, current_process_id=TEST_PROCESS_ID)
        real = RecordingFunction()

        with self.assertRaises(InputSentinelViolation):
            sentinel.guard("PostMessageW", real)(OWNED_WINDOW, 0x0100, 0, 0)

        self.assertEqual(real.calls, [])
        self.assertEqual(sentinel.blocked[0].owner_process_id, 0)

    def test_thread_input_messages_are_always_blocked(self):
        sentinel = make_sentinel()
        real = RecordingFunction()
        guarded = sentinel.guard("PostThreadMessageW", real)

        with self.assertRaises(InputSentinelViolation):
            guarded(TEST_PROCESS_ID, 0x0100, 0x41, 0)
        guarded(TEST_PROCESS_ID, 0x0010, 0, 0)

        self.assertEqual(len(real.calls), 1)
        self.assertEqual(sentinel.blocked[0].reason, "thread-input-message")

    def test_blocked_event_records_the_running_test(self):
        sentinel = make_sentinel()
        sentinel.context = "test_module.Case.test_name"

        with self.assertRaises(InputSentinelViolation):
            sentinel.guard("SendInput", RecordingFunction())(1, None, 0)

        self.assertEqual(sentinel.blocked[0].context, "test_module.Case.test_name")

    def test_message_ranges_cover_keyboard_and_mouse_only(self):
        self.assertTrue(is_input_message(0x0100))
        self.assertTrue(is_input_message(0x0109))
        self.assertTrue(is_input_message(0x0200))
        self.assertTrue(is_input_message(0x020E))
        for input_equivalent in (0x00FF, 0x0112, 0x0312, 0x0319):
            self.assertTrue(is_input_message(input_equivalent), hex(input_equivalent))
        self.assertFalse(is_input_message(0x0010))
        self.assertFalse(is_input_message(0x0317))

    def test_user32_library_name_matching(self):
        for name in ("user32", "USER32.DLL", r"C:\Windows\System32\user32.dll"):
            self.assertTrue(is_user32_library(name), name)
        for name in (None, "", "gdi32", "kernel32.dll", "user32x"):
            self.assertFalse(is_user32_library(name), name)


@unittest.skipUnless(os.name == "nt", "real user32 hook is available only on Windows")
class WindowsUser32HookTest(unittest.TestCase):
    def test_real_user32_input_is_blocked_before_dispatch(self):
        sentinel = InputSentinel(windows_owner_process_id_of())
        sentinel.install()
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            keybd_event = user32.keybd_event
            post_message = user32.PostMessageW
            desktop = int(user32.GetDesktopWindow())

            self.assertIsInstance(keybd_event, GuardedFunction)
            self.assertNotIsInstance(user32.GetDesktopWindow, GuardedFunction)
            with self.assertRaises(InputSentinelViolation):
                keybd_event(0, 0, 0, 0)
            with self.assertRaises(InputSentinelViolation):
                post_message(desktop, 0x0101, 0, 0)
        finally:
            sentinel.uninstall()

        self.assertEqual(
            [event.function for event in sentinel.blocked],
            ["keybd_event", "PostMessageW"],
        )
        self.assertNotEqual(sentinel.blocked[1].owner_process_id, os.getpid())


if __name__ == "__main__":
    unittest.main()
