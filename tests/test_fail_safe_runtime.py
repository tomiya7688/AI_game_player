import os
import tempfile
import time
import unittest
from pathlib import Path

from ai_game_player.action_executor import ActionExecutor
from ai_game_player.fail_safe_runtime import (
    AtomicJsonStore,
    FailSafeCommand,
    FailSafeConfig,
    FailSafeJournal,
    FailSafeRuntime,
    FailSafeState,
    InputLedger,
    run_external_watchdog,
)
from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import SafetyGuard, SafetyGuardConfig, TargetState


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FailingStore(AtomicJsonStore):
    def __init__(self, path: Path, fail_after: int) -> None:
        super().__init__(path)
        self.fail_after = fail_after
        self.writes = 0

    def write(self, value: dict[str, object]) -> None:
        self.writes += 1
        if self.writes > self.fail_after:
            raise OSError("injected disk failure")
        super().write(value)


class TargetProbe:
    def inspect(self, handle: int) -> TargetState:
        return TargetState(handle, 4242, True, True, True, 640, 480, 0, 0, 640, 480)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.releases = 0

    def execute(self, candidate):
        self.calls += 1
        return type(
            "Result",
            (),
            {"action_id": candidate.action_id, "executed": True, "mode": "fake", "detail": "ok"},
        )()

    def release_all(self) -> None:
        self.releases += 1


class FailSafeRuntimeTest(unittest.TestCase):
    def make_runtime(self, directory: str, clock: FakeClock | None = None, **kwargs) -> FailSafeRuntime:
        active_clock = clock or FakeClock()
        config = kwargs.pop(
            "config",
            FailSafeConfig(
                lease_timeout_seconds=1.0,
                observation_timeout_seconds=2.0,
                max_command_age_seconds=0.5,
                max_queue_depth=2,
                hold_ttl_seconds=0.5,
                watchdog_poll_seconds=0.05,
            ),
        )
        return FailSafeRuntime(
            Path(directory),
            config,
            clock=active_clock,
            external_watchdog=False,
            **kwargs,
        )

    def arm_and_observe(self, runtime: FailSafeRuntime) -> None:
        runtime.rearm(target_pid=4242, target_handle=99, session_id="session")
        runtime._heartbeat_shutdown.set()
        self.assertTrue(runtime.record_observation())

    def test_startup_is_never_active_and_requires_explicit_rearm(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            self.assertEqual(runtime.state, FailSafeState.SAFE_IDLE)
            command = runtime.make_command("go", target_pid=4242, target_handle=99)
            decision = runtime.check_command(command)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "runtime_not_active")
            self.arm_and_observe(runtime)
            self.assertEqual(runtime.state, FailSafeState.ACTIVE)
            runtime.close()

    def test_restart_from_active_journal_requires_recovery_and_rearm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            FailSafeJournal(root / "journal.json").record(
                FailSafeState.ACTIVE,
                "simulated_parent_crash",
                "old-epoch",
            )
            runtime = self.make_runtime(directory)
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            self.assertEqual(runtime.reason, "unclean_previous_runtime_state")
            self.arm_and_observe(runtime)
            self.assertEqual(runtime.state, FailSafeState.ACTIVE)
            runtime.close()

    def test_fresh_observation_and_sequence_allow_repeated_candidate_action(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(directory)
            self.arm_and_observe(runtime)

            first = runtime.make_command("same-candidate", target_pid=4242, target_handle=99)
            self.assertTrue(runtime.check_command(first).allowed)
            runtime.complete_command(first)
            second = runtime.make_command("same-candidate", target_pid=4242, target_handle=99)
            self.assertTrue(runtime.check_command(second).allowed)
            runtime.complete_command(second)

            replay = runtime.check_command(first)
            self.assertFalse(replay.allowed)
            self.assertEqual(replay.code, "stale_sequence")
            runtime.close()

    def test_stale_epoch_session_command_age_and_observation_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.make_runtime(directory, clock)
            self.arm_and_observe(runtime)
            current = runtime.make_command("a", target_pid=4242, target_handle=99)

            stale_epoch = FailSafeCommand(**{**current.to_dict(), "epoch": "old"})
            self.assertEqual(runtime.check_command(stale_epoch).code, "stale_epoch")
            stale_session = FailSafeCommand(**{**current.to_dict(), "session_id": "old"})
            self.assertEqual(runtime.check_command(stale_session).code, "stale_session")

            old = FailSafeCommand(**{**current.to_dict(), "issued_at": clock() - 1.0})
            self.assertEqual(runtime.check_command(old).code, "stale_command")
            stale_observation = FailSafeCommand(**{**current.to_dict(), "observation_at": clock() - 0.1})
            self.assertEqual(runtime.check_command(stale_observation).code, "stale_observation")
            runtime.close()

    def test_target_change_and_capture_staleness_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.make_runtime(directory, clock)
            self.arm_and_observe(runtime)
            changed_target = runtime.make_command("a", target_pid=5000, target_handle=99)
            decision = runtime.check_command(changed_target)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.code, "target_changed")
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)

            runtime.rearm(target_pid=4242, target_handle=99, session_id="next")
            runtime._heartbeat_shutdown.set()
            runtime.record_observation()
            clock.advance(2.1)
            stale = runtime.make_command("b", target_pid=4242, target_handle=99)
            decision = runtime.check_command(stale)
            self.assertEqual(decision.code, "lease_expired")
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            runtime.close()

    def test_observation_stale_is_distinct_when_lease_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            runtime = self.make_runtime(
                directory,
                clock,
                config=FailSafeConfig(
                    lease_timeout_seconds=5.0,
                    observation_timeout_seconds=1.0,
                    max_command_age_seconds=0.5,
                    max_queue_depth=2,
                    hold_ttl_seconds=0.5,
                    watchdog_poll_seconds=0.05,
                ),
            )
            self.arm_and_observe(runtime)
            clock.advance(1.1)
            command = runtime.make_command("a", target_pid=4242, target_handle=99)
            decision = runtime.check_command(command)
            self.assertEqual(decision.code, "observation_stale")
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            runtime.close()

    def test_queue_depth_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.make_runtime(
                directory,
                config=FailSafeConfig(
                    lease_timeout_seconds=5.0,
                    observation_timeout_seconds=5.0,
                    max_command_age_seconds=1.0,
                    max_queue_depth=1,
                    hold_ttl_seconds=0.5,
                    watchdog_poll_seconds=0.05,
                ),
            )
            self.arm_and_observe(runtime)
            first = runtime.make_command("a", target_pid=4242, target_handle=99)
            self.assertTrue(runtime.check_command(first).allowed)
            second = runtime.make_command("b", target_pid=4242, target_handle=99)
            self.assertEqual(runtime.check_command(second).code, "queue_full")
            runtime.complete_command(first)
            third = runtime.make_command("b", target_pid=4242, target_handle=99)
            self.assertTrue(runtime.check_command(third).allowed)
            runtime.complete_command(third)
            runtime.close()

    def test_disk_failure_transitions_to_recovery_and_releases_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = FakeClock()
            release_calls: list[str] = []
            store = FailingStore(root / "lease.json", fail_after=2)
            runtime = FailSafeRuntime(
                root,
                FailSafeConfig(
                    lease_timeout_seconds=5.0,
                    observation_timeout_seconds=5.0,
                    watchdog_poll_seconds=0.05,
                ),
                clock=clock,
                lease_store=store,
                release_callback=lambda: release_calls.append("release"),
                external_watchdog=False,
            )
            runtime.rearm(target_pid=4242, target_handle=99)
            runtime._heartbeat_shutdown.set()
            self.assertFalse(runtime.record_observation())
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            self.assertTrue(release_calls)
            runtime.close()

    def test_input_ledger_keeps_expiry_and_clears_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            ledger = InputLedger(Path(directory) / "ledger.json", clock=clock)
            ledger.configure(target_handle=99, input_mode="mouse")
            ledger.hold_key(0x20, 0.5)
            ledger.hold_mouse("left", 0.25)
            snapshot = ledger.snapshot()
            self.assertEqual(snapshot["held_keys"]["32"], 1000.5)
            self.assertEqual(snapshot["held_mouse"]["left"], 1000.25)
            ledger.release_key(0x20)
            ledger.release_mouse("left")
            self.assertEqual(ledger.snapshot()["held_keys"], {})
            self.assertEqual(ledger.snapshot()["held_mouse"], {})

    def test_dependency_minimal_watchdog_detects_dead_owner_and_clears_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            AtomicJsonStore(root / "lease.json").write(
                {
                    "schema": "kadoka-control-lease/v1",
                    "state": "active",
                    "epoch": "epoch",
                    "owner_pid": 99999999,
                    "target_pid": 0,
                    "target_handle": 0,
                    "lease_expires_at": now + 30,
                    "observation_expires_at": now + 30,
                    "watchdog_poll_seconds": 0.01,
                }
            )
            ledger = InputLedger(root / "input_ledger.json")
            ledger.configure(target_handle=0, input_mode="mouse")
            ledger.hold_key(0x20, 30)
            self.assertEqual(run_external_watchdog(root, max_runtime_seconds=1.0), 0)
            lease = AtomicJsonStore(root / "lease.json").read()
            self.assertEqual(lease["state"], "recovery_required")
            self.assertEqual(lease["reason"], "owner_process_lost")
            self.assertEqual(ledger.snapshot()["held_keys"], {})

    def test_action_executor_requires_rearm_and_fresh_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeExecutor()
            stop_releases: list[str] = []
            runtime = self.make_runtime(directory, release_callback=lambda: stop_releases.append("release"))
            guard = SafetyGuard(
                SafetyGuardConfig(require_target=False, require_foreground=False),
                window_handle=99,
                target_probe=TargetProbe(),
            )
            executor = ActionExecutor(
                False,
                fake,
                window_handle=99,
                safety_guard=guard,
                fail_safe_runtime=runtime,
            )
            candidate = ActionCandidate("go", "wait", "0")
            with self.assertRaisesRegex(RuntimeError, "runtime_not_active"):
                executor.execute(candidate)
            executor.rearm_safety()
            with self.assertRaisesRegex(RuntimeError, "observation_stale"):
                executor.execute(candidate)
            executor.rearm_safety()
            executor.record_observation()
            result = executor.execute(candidate)
            self.assertTrue(result.executed)
            self.assertEqual(fake.calls, 1)
            executor.trigger_emergency_stop("test stop")
            self.assertEqual(runtime.state, FailSafeState.EMERGENCY_STOP)
            executor.close()


if __name__ == "__main__":
    unittest.main()
