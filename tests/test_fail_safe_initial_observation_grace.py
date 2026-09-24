import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from ai_game_player.fail_safe_runtime import (
    AtomicJsonStore,
    FailSafeConfig,
    FailSafeRuntime,
    FailSafeState,
    run_external_watchdog,
)


class FailSafeInitialObservationGraceTest(unittest.TestCase):
    @patch("ai_game_player.fail_safe_runtime._windows_target_matches", return_value=True)
    def test_rearm_allows_initial_observation_grace_without_granting_input(self, _target_matches):
        with tempfile.TemporaryDirectory() as directory:
            state_directory = Path(directory)
            runtime = FailSafeRuntime(
                state_directory,
                FailSafeConfig(
                    lease_timeout_seconds=1.0,
                    observation_timeout_seconds=0.25,
                    watchdog_poll_seconds=0.01,
                ),
                external_watchdog=False,
            )
            try:
                runtime.rearm(target_pid=os.getpid(), target_handle=1)
                lease = AtomicJsonStore(state_directory / "lease.json").read()
                self.assertEqual(lease["state"], FailSafeState.ACTIVE.value)
                self.assertEqual(float(lease["observation_at"]), 0.0)
                self.assertGreater(float(lease["observation_expires_at"]), time.time())

                # A watchdog may start immediately after re-arm, but must leave enough
                # time for the first real Observation to be recorded.
                self.assertEqual(run_external_watchdog(state_directory, max_runtime_seconds=0.05), 0)
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
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
