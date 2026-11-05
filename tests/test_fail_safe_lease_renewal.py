"""Issue #794: 制御lease延長の時系列・失効・永続Stop競合の回帰試験（ネットワーク不要・決定論）。"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player import fail_safe_runtime as runtime_module
from ai_game_player.fail_safe_runtime import (
    FailSafeConfig,
    FailSafeRuntime,
    FailSafeState,
    run_external_watchdog,
)
from ai_game_player.heartbeat_diagnostic import HeartbeatDiagnostics
from tools.run_2048_campaign import ReferenceGame2048Episode

FIXTURE = Path(__file__).parent / "fixtures" / "issue794_lease_expiry_rings.json"
TTL = 0.75
INTERVAL = 0.25


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ScriptedSchedule:
    """偽monotonic時計と停止event。heartbeatは台本の所要時間だけ時計を進める。"""

    def __init__(self, cycles: list[tuple[float, float]]) -> None:
        # cycles: (更新までの秒, 更新から保存完了までの秒)
        self.now = 0.0
        self.cycles = list(cycles)
        self.waits: list[float] = []
        self.renewals: list[float] = []
        self.commits: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, timeout: float) -> bool:
        self.waits.append(round(timeout, 6))
        self.now += timeout
        return not self.cycles

    def heartbeat(self) -> bool:
        before_renewal, renewal_to_commit = self.cycles.pop(0)
        self.now += before_renewal
        self.renewals.append(self.now)
        self.now += renewal_to_commit
        self.commits.append(self.now)
        return True

    def worst_margin(self) -> float:
        """前回更新の期限 − 今回の保存完了（秒）。負ならwatchdogが失効を観測し得る。"""
        return min(
            previous + TTL - commit
            for previous, commit in zip(self.renewals, self.commits[1:])
        )


def run_deadline_loop(cycles: list[tuple[float, float]]) -> ScriptedSchedule:
    schedule = ScriptedSchedule(cycles)
    runtime = SimpleNamespace(
        _heartbeat_shutdown=SimpleNamespace(wait=schedule.wait),
        heartbeat=schedule.heartbeat,
        state=FailSafeState.ACTIVE,
    )
    with patch.object(runtime_module, "_schedule_clock", schedule.clock):
        FailSafeRuntime._heartbeat_loop(runtime, INTERVAL)  # type: ignore[arg-type]
    return schedule


def run_fixed_delay_loop(cycles: list[tuple[float, float]]) -> ScriptedSchedule:
    """修正前の「完了後に間隔を待つ」loop（因果の比較用）。"""
    schedule = ScriptedSchedule(cycles)
    while not schedule.wait(INTERVAL):
        schedule.heartbeat()
    return schedule


def fixture_runs() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]


def ns(value: int) -> float:
    return value / 1e9


class NaturalFailureReplayTests(unittest.TestCase):
    def test_recorded_rings_show_fixed_delay_commit_after_previous_expiry(self):
        for run in fixture_runs():
            with self.subTest(run=run["run"]):
                rows = run["rows"]
                renewed = [r for r in rows if r["result"] == "renewed"]
                self.assertEqual(rows[-1]["result"], "persisted_stop")
                margins = []
                for previous, current in zip(renewed, renewed[1:]):
                    # 修正前は読取直後に更新し、書込完了でディスクへ見える。
                    committed = current["renewal_unix_seconds"] + ns(
                        current["lease_write_finished_mono_ns"]
                        - current["persisted_read_finished_mono_ns"]
                    )
                    margins.append(previous["lease_expires_unix_seconds"] - committed)
                self.assertLess(min(margins), 0.0)
                # 失効直前まで正常に更新していた（stop以前は全てrenewed）。
                self.assertTrue(all(r["result"] == "renewed" for r in rows[:-1]))

    def test_same_phase_durations_stay_within_ttl_with_deadline_schedule(self):
        for run in fixture_runs():
            with self.subTest(run=run["run"]):
                cycles = []
                for row in run["rows"]:
                    if row["result"] != "renewed":
                        continue
                    # 修正後も更新は永続読取の後・書込の直前。同じ相別所要時間を再生する。
                    before_renewal = ns(
                        row["persisted_read_finished_mono_ns"] - row["entry_mono_ns"]
                    )
                    write = ns(
                        row["lease_write_finished_mono_ns"]
                        - row["persisted_read_finished_mono_ns"]
                    )
                    cycles.append((before_renewal, write))
                legacy = run_fixed_delay_loop(cycles)
                fixed = run_deadline_loop(cycles)
                self.assertGreater(fixed.worst_margin(), 0.0)
                self.assertGreater(fixed.worst_margin(), legacy.worst_margin())

    def test_deadline_waits_subtract_elapsed_cycle_and_never_negative(self):
        schedule = run_deadline_loop([(0.0, 0.1), (0.0, 0.4), (0.0, 0.05)])
        self.assertEqual(schedule.waits, [0.25, 0.15, 0.0, 0.2])

    def test_cycle_longer_than_ttl_is_not_hidden_by_schedule(self):
        schedule = run_deadline_loop([(0.0, 0.1), (0.0, 0.9), (0.0, 0.1)])
        self.assertLess(schedule.worst_margin(), 0.0)


class LeaseExtensionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = FakeClock()
        self.runtime = FailSafeRuntime(
            Path(self.directory.name),
            FailSafeConfig(observation_timeout_seconds=2.0),
            clock=self.clock,
            external_watchdog=False,
        )
        self.addCleanup(self.runtime.close)
        with patch.object(self.runtime, "_start_heartbeat_locked"):
            self.runtime.initialize(target_pid=4242, target_handle=99, session_id="s")
        self.assertTrue(self.runtime.record_observation())

    def persisted(self) -> dict[str, object]:
        return self.runtime.lease_store.read()

    def command(self):
        return self.runtime.make_command("a", target_pid=4242, target_handle=99)

    def assert_expired_and_blocked(self):
        self.assertEqual(self.runtime.state, FailSafeState.RECOVERY_REQUIRED)
        self.assertEqual(self.runtime.reason, "lease_expired")
        self.assertEqual(self.persisted()["state"], "recovery_required")
        self.assertEqual(self.persisted()["lease_expires_at"], 0.0)
        decision = self.runtime.check_command(self.command())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "runtime_not_active")

    def test_boundary_equal_to_expiry_extends_and_beyond_expiry_does_not(self):
        self.clock.advance(TTL)
        self.assertTrue(self.runtime.heartbeat())
        self.assertEqual(self.runtime.state, FailSafeState.ACTIVE)
        self.clock.advance(TTL + 0.001)
        self.assertFalse(self.runtime.heartbeat())
        self.assert_expired_and_blocked()

    def test_record_observation_does_not_revive_expired_lease(self):
        self.clock.advance(TTL + 0.001)
        self.assertFalse(self.runtime.record_observation())
        self.assert_expired_and_blocked()

    def test_complete_command_does_not_revive_expired_lease(self):
        command = self.command()
        self.assertTrue(self.runtime.check_command(command).allowed)
        self.clock.advance(TTL + 0.001)
        self.runtime.complete_command(command)
        self.assert_expired_and_blocked()

    def test_traced_heartbeat_records_lease_expired(self):
        self.runtime.configure_heartbeat_diagnostics(enabled=True)
        self.clock.advance(TTL + 0.001)
        self.assertFalse(self.runtime.heartbeat())
        row = self.runtime.heartbeat_diagnostic_snapshot()["completed"][-1]
        self.assertEqual(row["result"], "lease_expired")
        self.assert_expired_and_blocked()

    def inject_stop_after(self, method: str, epoch: str | None = None):
        original = getattr(self.runtime, method)

        def wrapped(*args):
            result = original(*args)
            stop = self.persisted()
            stop.update(
                state="recovery_required",
                reason="lease_expired",
                epoch=self.runtime.epoch if epoch is None else epoch,
            )
            self.runtime.lease_store.write(stop)
            return result

        return patch.object(self.runtime, method, wrapped)

    def assert_watchdog_stop_kept(self):
        self.assertEqual(self.persisted()["state"], "recovery_required")
        self.assertEqual(self.persisted()["reason"], "lease_expired")
        self.assertEqual(self.runtime.state, FailSafeState.RECOVERY_REQUIRED)
        self.assertEqual(self.runtime.reason, "lease_expired")

    def write_watchdog_stop(self, epoch: str | None = None) -> None:
        stop = self.persisted()
        stop.update(
            state="recovery_required",
            reason="lease_expired",
            epoch=self.runtime.epoch if epoch is None else epoch,
        )
        self.runtime.lease_store.write(stop)

    def test_heartbeat_checks_stop_and_writes_in_one_lock(self):
        for diagnostics in (False, True):
            with self.subTest(diagnostics=diagnostics):
                self.setUp()
                self.runtime.configure_heartbeat_diagnostics(enabled=diagnostics)
                calls = []
                original = self.runtime.lease_store.replace_unless

                def recorded(value, reject):
                    calls.append("replace_unless")
                    return original(value, reject)

                self.write_watchdog_stop()
                with (
                    patch.object(self.runtime.lease_store, "replace_unless", recorded),
                    patch.object(
                        self.runtime.lease_store, "read", side_effect=AssertionError
                    ),
                ):
                    self.assertFalse(self.runtime.heartbeat())
                self.assertEqual(calls, ["replace_unless"])
                self.assert_watchdog_stop_kept()
                if diagnostics:
                    row = self.runtime.heartbeat_diagnostic_snapshot()["completed"][-1]
                    self.assertEqual(row["result"], "persisted_stop")
                    self.assertIsNone(row["renewal_mono_ns"])
                    self.assertIsNone(row["lease_write_started_mono_ns"])

    def test_store_rejection_or_aborted_payload_does_not_write(self):
        store = self.runtime.lease_store
        before = store.path.read_bytes()
        self.assertEqual(
            store.replace_unless({"x": 1}, lambda current, exists: True)["epoch"],
            self.runtime.epoch,
        )
        self.assertIsNotNone(store.replace_unless(lambda: None, lambda c, e: False))
        self.assertEqual(store.path.read_bytes(), before)
        missing = Path(self.directory.name) / "new" / "nested.json"
        written = runtime_module.AtomicJsonStore(missing)
        self.assertIsNone(written.replace_unless({"y": 2}, lambda c, e: e))
        self.assertEqual(written.read(), {"y": 2})

    def test_observation_and_completion_do_not_overwrite_concurrent_stop(self):
        with self.inject_stop_after("_extend_lease_locked"):
            self.assertFalse(self.runtime.record_observation())
        self.assert_watchdog_stop_kept()
        self.setUp()
        command = self.command()
        self.assertTrue(self.runtime.check_command(command).allowed)
        with self.inject_stop_after("_extend_lease_locked"):
            self.runtime.complete_command(command)
        self.assert_watchdog_stop_kept()

    def test_check_command_reports_concurrent_stop_as_not_active(self):
        with self.inject_stop_after("_extend_lease_locked"):
            decision = self.runtime.check_command(self.command())
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "runtime_not_active")
        self.assert_watchdog_stop_kept()

    def test_check_command_does_not_revive_lease_expired_during_persisted_read(self):
        command = self.command()
        original = self.runtime.lease_store.read

        def slow_read():
            result = original()
            self.clock.advance(TTL + 0.001)  # 永続読取のI/Oが期限を跨いだ
            return result

        with patch.object(self.runtime.lease_store, "read", slow_read):
            decision = self.runtime.check_command(command)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "lease_expired")
        self.assertEqual(self.runtime.state, FailSafeState.RECOVERY_REQUIRED)
        self.assertEqual(self.persisted()["state"], "recovery_required")

    def test_check_command_extension_rechecks_expiry_after_judgement(self):
        command = self.command()
        calls = {"n": 0}
        real_clock = self.clock

        def jumping_clock():
            calls["n"] += 1
            if calls["n"] == 2:  # 判定用の時刻の次、延長用の時刻で期限超過
                real_clock.advance(TTL + 0.001)
            return real_clock()

        self.runtime.clock = jumping_clock
        decision = self.runtime.check_command(command)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "lease_expired")

    def test_same_epoch_lease_without_state_is_treated_as_stop(self):
        incomplete = self.persisted()
        incomplete.pop("state")
        self.runtime.lease_store.write(incomplete)
        self.assertFalse(self.runtime.heartbeat())
        self.assertEqual(self.runtime.state, FailSafeState.RECOVERY_REQUIRED)
        self.assertEqual(self.persisted().get("state"), None)
        decision = self.runtime.check_command(self.command())
        self.assertFalse(decision.allowed)

    def test_second_close_keeps_the_first_previous_reason(self):
        self.clock.advance(TTL + 0.001)
        self.assertFalse(self.runtime.heartbeat())
        self.runtime.close()
        first = self.runtime.journal.read()
        self.runtime.close()
        self.assertEqual(self.runtime.journal.read(), first)
        self.assertEqual(first["details"]["previous_reason"], "lease_expired")

    def test_diagnostic_callback_failure_does_not_change_heartbeat(self):
        self.runtime.configure_heartbeat_diagnostics(enabled=True)
        recorder = self.runtime._heartbeat_diagnostics
        with patch.object(recorder, "renewal", side_effect=MemoryError):
            self.assertTrue(self.runtime.heartbeat())
        self.assertEqual(self.runtime.state, FailSafeState.ACTIVE)

    def test_other_epoch_stop_and_missing_file_keep_existing_semantics(self):
        self.write_watchdog_stop(epoch="older-epoch")
        self.assertTrue(self.runtime.heartbeat())
        self.assertEqual(self.persisted()["state"], "active")
        self.assertEqual(self.persisted()["epoch"], self.runtime.epoch)
        self.runtime.lease_store.path.unlink()
        self.assertTrue(self.runtime.heartbeat())
        self.assertEqual(self.persisted()["state"], "active")

    def test_unreadable_lease_is_adopted_as_recovery(self):
        self.runtime.lease_store.path.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.runtime.heartbeat())
        self.assertEqual(self.runtime.state, FailSafeState.RECOVERY_REQUIRED)
        self.assertEqual(self.runtime.reason, "lease_unreadable")

    def test_close_keeps_previous_reason_in_journal(self):
        self.clock.advance(TTL + 0.001)
        self.assertFalse(self.runtime.heartbeat())
        self.runtime.close()
        journal = self.runtime.journal.read()
        self.assertEqual(journal["reason"], "runtime closed")
        self.assertEqual(journal["details"]["previous_state"], "recovery_required")
        self.assertEqual(journal["details"]["previous_reason"], "lease_expired")


class WatchdogRaceTests(unittest.TestCase):
    def test_watchdog_survives_lease_lock_timeout_and_keeps_monitoring(self):
        reads = {"n": 0}
        original = runtime_module.AtomicJsonStore.read

        def flaky(self):
            reads["n"] += 1
            if reads["n"] <= 3:
                raise TimeoutError("lock held by a slow writer")
            return original(self)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runtime_module.AtomicJsonStore, "read", flaky):
                self.assertEqual(
                    run_external_watchdog(Path(directory), max_runtime_seconds=0.5), 0
                )
        self.assertGreater(reads["n"], 3)

    def test_runtime_does_not_resume_after_watchdog_wrote_expiry(self):
        import time

        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock(time.time() - 10.0)
            runtime = FailSafeRuntime(
                Path(directory), clock=clock, external_watchdog=False
            )
            try:
                with patch.object(runtime, "_start_heartbeat_locked"):
                    runtime.initialize(
                        target_pid=os.getpid(), target_handle=99, session_id="w"
                    )
                with patch.object(
                    runtime_module, "_windows_target_matches", return_value=True
                ):
                    self.assertEqual(
                        run_external_watchdog(Path(directory), max_runtime_seconds=1.0),
                        0,
                    )
                self.assertEqual(runtime.lease_store.read()["reason"], "lease_expired")
                # Runtime自身の時計では未失効でも、永続Stopを上書きせず採用する。
                self.assertFalse(runtime.heartbeat())
                self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
                self.assertEqual(
                    runtime.lease_store.read()["state"], "recovery_required"
                )
            finally:
                runtime.close()


class HeartbeatSummaryTests(unittest.TestCase):
    def test_margin_uses_previous_expiry_and_commit_time(self):
        recorder = HeartbeatDiagnostics()
        stamps = iter(range(1_000_000_000, 1_000_000_000 + 10**9, 10_000_000))
        for renewal in (1000.0, 1000.3):
            with patch(
                "ai_game_player.heartbeat_diagnostic._sample_monotonic_ns",
                side_effect=lambda: next(stamps),
            ):
                row = recorder.begin()
                for field in (
                    "lock_acquired_mono_ns",
                    "persisted_read_started_mono_ns",
                    "persisted_read_finished_mono_ns",
                    "renewal_mono_ns",
                    "lease_write_started_mono_ns",
                    "lease_write_finished_mono_ns",
                ):
                    recorder.mark(row, field)
                recorder.renewal(row, renewal, renewal + TTL)
                recorder.finish(row, "renewed")
        summary = recorder.snapshot()["summary"]
        # 更新から書込完了まで2刻み=20ms。期限1000.75 − (1000.3+0.02) = 430ms。
        self.assertEqual(summary["margin_ms_min"], 430.0)
        self.assertEqual(summary["renewals"], 2)
        self.assertEqual(summary["margin_samples"], 1)
        self.assertEqual(summary["window_samples"], 2048)
        # 最小値は全期間の累積で、直近windowから落ちても失われない。
        recorder._margins_ms.clear()
        self.assertEqual(recorder.summary()["margin_ms_min"], 430.0)
        self.assertEqual(summary["negative_margin_count"], 0)
        self.assertEqual(summary["duration_ms_max"], 70.0)

    def test_summary_survives_in_bounded_snapshot(self):
        recorder = HeartbeatDiagnostics()
        for _ in range(3000):
            row = recorder.begin()
            recorder.finish(row, "not_active")
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertIsNone(snapshot["summary"]["margin_ms_min"])
        self.assertLess(len(json.dumps(snapshot)), 32 * 1024)


class CampaignBudgetLineTests(unittest.TestCase):
    def episode(self, enabled: bool, snapshot):
        runtime = SimpleNamespace(
            heartbeat_diagnostic_snapshot=lambda: snapshot,
            config=FailSafeConfig(),
        )
        return SimpleNamespace(
            _save_heartbeat_diagnostics=enabled,
            pipeline=SimpleNamespace(
                executor=SimpleNamespace(fail_safe_runtime=runtime)
            ),
        )

    def emit(self, episode) -> str:
        line = ReferenceGame2048Episode._heartbeat_lease_budget_line(episode)  # type: ignore[arg-type]
        stream = io.StringIO()
        with redirect_stdout(stream):
            ReferenceGame2048Episode._emit_heartbeat_lease_budget(line)
        return stream.getvalue()

    def test_line_is_printed_only_after_all_resources_are_closed(self):
        order = []
        episode = SimpleNamespace(
            _closed=False,
            pipeline=SimpleNamespace(close=lambda: order.append("pipeline")),
            source=SimpleNamespace(close=lambda: order.append("source")),
            launcher=SimpleNamespace(close=lambda: order.append("launcher")),
            _heartbeat_lease_budget_line=lambda: order.append("build") or "LINE",
            _emit_heartbeat_lease_budget=lambda line: order.append(f"emit:{line}"),
        )
        ReferenceGame2048Episode.close(episode)  # type: ignore[arg-type]
        self.assertEqual(
            order, ["build", "pipeline", "source", "launcher", "emit:LINE"]
        )

    def test_blocked_stdout_cannot_skip_cleanup_or_raise(self):
        def broken(*_args, **_kwargs):
            raise OSError("stdout closed")

        with patch("builtins.print", broken):
            ReferenceGame2048Episode._emit_heartbeat_lease_budget("LINE")

    def test_emits_one_line_only_when_enabled(self):
        summary = {"renewals": 3, "margin_ms_min": 120.5}
        line = self.emit(self.episode(True, {"summary": summary}))
        event = json.loads(line)
        self.assertEqual(event["event"], "heartbeat_lease_budget")
        self.assertEqual(event["lease_timeout_ms"], 750.0)
        self.assertEqual(event["summary"], summary)
        self.assertEqual(self.emit(self.episode(False, {"summary": summary})), "")

    def test_failures_are_silent(self):
        self.assertEqual(self.emit(self.episode(True, None)), "")
        broken = self.episode(True, {"summary": {"x": float("nan")}})
        self.assertEqual(self.emit(broken), "")


if __name__ == "__main__":
    unittest.main()
