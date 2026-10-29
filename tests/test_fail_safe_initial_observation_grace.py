import os
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from ai_game_player.fail_safe_runtime import (
    AtomicJsonStore,
    FailSafeConfig,
    FailSafeRuntime,
    FailSafeState,
    run_external_watchdog,
)


class ManualClock:
    """Runtimeとwatchdogが共有し、テストが指定した時間だけ進む時計。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@patch("ai_game_player.fail_safe_runtime._windows_target_matches", return_value=True)
class FailSafeInitialObservationGraceTest(unittest.TestCase):
    @contextmanager
    def armed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory)
            clock = ManualClock()
            runtime = FailSafeRuntime(
                state_directory,
                FailSafeConfig(
                    lease_timeout_seconds=1.0,
                    observation_timeout_seconds=0.25,
                    watchdog_poll_seconds=0.01,
                ),
                clock=clock,
                external_watchdog=False,
            )
            try:
                # heartbeatの並行更新は別テストに委ね、観測猶予だけを検証します。
                with patch.object(runtime, "_start_heartbeat_locked"):
                    runtime.rearm(target_pid=os.getpid(), target_handle=1)
                yield runtime, clock, state_directory
            finally:
                runtime.close()

    def run_watchdog(self, state_directory, clock, duration=0.05):
        # グローバルなtimeモジュールは変更せず、watchdogのsleepだけで時計を進めます。
        with patch(
            "ai_game_player.fail_safe_runtime.time",
            SimpleNamespace(time=clock, sleep=clock.advance),
        ):
            return run_external_watchdog(state_directory, max_runtime_seconds=duration)

    def test_rearm_allows_initial_observation_grace_without_granting_input(
        self, _target_matches
    ):
        with self.armed_runtime() as (runtime, clock, state_directory):
            lease = AtomicJsonStore(state_directory / "lease.json").read()
            self.assertEqual(lease["state"], FailSafeState.ACTIVE.value)
            self.assertEqual(float(lease["observation_at"]), 0.0)
            self.assertEqual(float(lease["observation_expires_at"]), clock() + 0.25)

            # A watchdog may start immediately after re-arm, but must leave enough
            # time for the first real Observation to be recorded.
            self.assertEqual(self.run_watchdog(state_directory, clock), 0)
            self.assertGreaterEqual(clock(), 1000.05)
            self.assertLess(clock(), float(lease["observation_expires_at"]))
            lease = AtomicJsonStore(state_directory / "lease.json").read()
            self.assertEqual(lease["state"], FailSafeState.ACTIVE.value)

            # Input still remains blocked until a real Observation is recorded.
            command = runtime.make_command(
                "before-first-observation",
                target_pid=os.getpid(),
                target_handle=1,
            )
            decision = runtime.check_command(command)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "observation_stale")

    def test_watchdog_keeps_active_state_at_exact_observation_deadline(
        self, _target_matches
    ):
        with self.armed_runtime() as (_runtime, clock, state_directory):
            clock.advance(0.25)
            # watchdogは期限判定後に実行時間上限を確認するため、0秒でも1回判定します。
            # sleepで時計を進めず、期限ちょうどの状態を確認します。
            self.assertEqual(self.run_watchdog(state_directory, clock, duration=0.0), 0)
            lease = AtomicJsonStore(state_directory / "lease.json").read()
            self.assertEqual(lease["state"], FailSafeState.ACTIVE.value)

    def test_watchdog_rejects_missing_observation_after_grace_expires(
        self, _target_matches
    ):
        with self.armed_runtime() as (_runtime, clock, state_directory):
            # 猶予0.25秒を約1マイクロ秒だけ超え、期限内との境界を確認します。
            clock.advance(0.250001)
            self.assertEqual(self.run_watchdog(state_directory, clock), 0)
            lease = AtomicJsonStore(state_directory / "lease.json").read()
            self.assertEqual(lease["state"], FailSafeState.RECOVERY_REQUIRED.value)
            self.assertEqual(lease["reason"], "observation_stale")


if __name__ == "__main__":
    unittest.main()
