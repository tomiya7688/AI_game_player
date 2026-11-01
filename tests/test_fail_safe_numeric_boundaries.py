from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.fail_safe_runtime import (
    AtomicJsonStore,
    FailSafeCommand,
    FailSafeConfig,
    FailSafeRuntime,
    FailSafeState,
    run_external_watchdog,
)
from test_fail_safe_runtime import FakeClock


class FailSafeNumericBoundaryTests(unittest.TestCase):
    def runtime(self, directory, clock, config=None):
        runtime = FailSafeRuntime(
            Path(directory),
            config or FailSafeConfig(),
            clock=clock,
            external_watchdog=False,
        )
        runtime.rearm(target_pid=4242, target_handle=99, session_id="finite-time")
        runtime._heartbeat_shutdown.set()
        self.assertTrue(runtime.record_observation())
        self.addCleanup(
            lambda: runtime.close() if runtime.state_directory.exists() else None
        )
        return runtime

    def test_nan_timeout_cannot_bypass_real_observation_deadline(self):
        # A rejected config is the safe result; on the old implementation this
        # proceeds to the live gate and demonstrates the actual 3s bypass.
        try:
            config = FailSafeConfig(observation_timeout_seconds=float("nan"))
        except ValueError:
            return
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.runtime(directory, clock, config)
            clock.advance(3)
            self.assertTrue(runtime.heartbeat())
            command = runtime.make_command("move", target_pid=4242, target_handle=99)
            self.assertFalse(
                runtime.check_command(command).allowed,
                "NaN accepted a 3-second-old observation",
            )

    def test_finite_timeout_stays_stale_after_heartbeat_renews_only_control_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.runtime(directory, clock)
            clock.advance(3)
            self.assertTrue(runtime.heartbeat())
            command = runtime.make_command("move", target_pid=4242, target_handle=99)
            decision = runtime.check_command(command)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "observation_stale")

    def test_all_config_times_and_queue_depth_reject_wrong_types_or_nonfinite_values(
        self,
    ):
        for name in (
            "lease_timeout_seconds",
            "observation_timeout_seconds",
            "max_command_age_seconds",
            "hold_ttl_seconds",
            "watchdog_poll_seconds",
        ):
            for value in (
                float("nan"),
                float("inf"),
                float("-inf"),
                True,
                False,
                "2",
                None,
                0,
                -1,
                2**2048,
            ):
                with (
                    self.subTest(field=name, type=type(value).__name__),
                    self.assertRaises(ValueError),
                ):
                    FailSafeConfig(**{name: value})
        for value in (
            True,
            False,
            1.0,
            1.5,
            float("nan"),
            float("inf"),
            "32",
            None,
            0,
            -1,
        ):
            with self.subTest(queue=value), self.assertRaises(ValueError):
                FailSafeConfig(max_queue_depth=value)
        self.assertEqual(FailSafeConfig(max_queue_depth=1).max_queue_depth, 1)
        self.assertEqual(
            FailSafeConfig(observation_timeout_seconds=5).observation_timeout_seconds, 5
        )

    def test_invalid_direct_command_times_never_advance_sequence_or_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.runtime(directory, clock)
            command = runtime.make_command("move", target_pid=4242, target_handle=99)
            baseline = runtime.lease_store.read()
            for name in ("issued_at", "observation_at"):
                for value in (
                    float("nan"),
                    float("inf"),
                    float("-inf"),
                    True,
                    "1000",
                    None,
                    2**2048,
                ):
                    with self.subTest(field=name, type=type(value).__name__):
                        decision = runtime.check_command(
                            replace(command, **{name: value})
                        )
                        self.assertFalse(decision.allowed)
                        self.assertEqual(runtime._last_sequence, 0)
                        self.assertEqual(runtime._inflight, 0)
                        self.assertEqual(runtime.lease_store.read(), baseline)
            self.assertTrue(runtime.check_command(command).allowed)
            self.assertEqual(runtime._last_sequence, command.sequence)
            self.assertEqual(runtime._inflight, 1)

    def test_external_command_decoder_rejects_nonfinite_and_coerced_times(self):
        data = dict(
            action_id="move",
            sequence=1,
            epoch="e",
            issued_at=1000.0,
            observation_at=1000.0,
            target_pid=4242,
            target_handle=99,
            session_id="s",
        )
        self.assertEqual(FailSafeCommand.from_dict(data).issued_at, 1000)
        for name in ("issued_at", "observation_at"):
            for value in (
                float("nan"),
                float("inf"),
                float("-inf"),
                True,
                "1000",
                None,
                2**2048,
            ):
                with (
                    self.subTest(field=name, type=type(value).__name__),
                    self.assertRaises(ValueError),
                ):
                    FailSafeCommand.from_dict({**data, name: value})

    def test_invalid_observation_does_not_replace_accepted_time_or_persist_nan(self):
        for value in (
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            "1000",
            object(),
            2**2048,
        ):
            with (
                self.subTest(type=type(value).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                runtime = self.runtime(directory, FakeClock())
                prior = runtime.lease_store.read()
                self.assertFalse(runtime.record_observation(value))
                self.assertEqual(runtime._last_observation_at, prior["observation_at"])
                self.assertEqual(
                    runtime._observation_expires_at, prior["observation_expires_at"]
                )
                self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
                text = runtime.lease_store.path.read_text()
                self.assertNotIn("NaN", text)
                self.assertNotIn("Infinity", text)
                json.loads(
                    text, parse_constant=lambda _value: self.fail("nonfinite persisted")
                )

    def test_watchdog_rejects_nonfinite_external_expiry_without_os_input(self):
        for name in ("lease_expires_at", "observation_expires_at", "observation_at"):
            for value in (float("nan"), float("inf"), float("-inf"), True, "1005"):
                with (
                    self.subTest(field=name, type=type(value).__name__),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    store = AtomicJsonStore(Path(directory) / "lease.json")
                    store.write(
                        {
                            "state": "active",
                            "owner_pid": 123,
                            "target_pid": 0,
                            "target_handle": 0,
                            "epoch": "e",
                            "lease_expires_at": 1005.0,
                            "observation_expires_at": 1005.0,
                            name: value,
                        }
                    )
                    with (
                        patch(
                            "ai_game_player.fail_safe_runtime.time.time",
                            return_value=1000.0,
                        ),
                        patch(
                            "ai_game_player.fail_safe_runtime._process_alive",
                            return_value=True,
                        ),
                        patch(
                            "ai_game_player.fail_safe_runtime._release_ledger"
                        ) as release,
                    ):
                        self.assertEqual(
                            run_external_watchdog(
                                Path(directory), max_runtime_seconds=0
                            ),
                            0,
                        )
                    self.assertEqual(store.read()["state"], "recovery_required")
                    self.assertEqual(store.read()["reason"], "invalid_lease_timestamp")
                    self.assertEqual(store.read()["observation_expires_at"], 0.0)
                    json.loads(
                        store.path.read_text(),
                        parse_constant=lambda _value: self.fail("nonfinite persisted"),
                    )
                    self.assertTrue(
                        any(
                            call.kwargs.get("release_all")
                            for call in release.call_args_list
                        )
                    )
