from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ai_game_player.fail_safe_runtime import FailSafeRuntime, FailSafeState
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.safety_guard import (
    EmergencyStop,
    SafetyGuard,
    SafetyGuardConfig,
    TargetState,
)
from test_game_2048_adapter import render_fixture, read_fixture
from tools.run_2048_campaign import (
    Cached2048ObservationSource,
    ReferenceGame2048Episode,
)


class ComparisonPreflightTests(unittest.TestCase):
    def setUp(self):
        self.deadline = time.monotonic() + 5
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.global_stop, self.local_stop = EmergencyStop(), EmergencyStop()
        self.default_patch = patch(
            "ai_game_player.action_executor.default_emergency_stop",
            return_value=self.global_stop,
        )
        self.default_patch.start()
        self.addCleanup(self.default_patch.stop)
        self.controller = RunController()
        self.runtime = FailSafeRuntime(
            Path(self.directory.name) / "runtime", external_watchdog=False
        )
        self.target = TargetState(123, 456, True, True, True, 304, 304, 0, 0, 304, 304)
        self.probe = SimpleNamespace(
            inspect=Mock(side_effect=lambda _handle: self.target)
        )
        self.capture_hook = None
        self.captures = 0
        self.frame = render_fixture(read_fixture("standard.json"))
        source = Cached2048ObservationSource.__new__(Cached2048ObservationSource)
        source.window_handle, source.window_process_id = 123, 456
        source.capture = SimpleNamespace(
            capture=self.capture,
            last_capture_backend="gdi",
            last_backend_error=None,
            close=Mock(),
        )
        source.analyzer = FrameAnalyzer()
        source.adapter = ReferenceGame2048Adapter()
        source.failure_evidence = None
        source.source_id, source.capture_sequence = "preflight", 0
        source.not_before, source.settle_seconds = 0.0, 0.0
        source.latest_observation, source.latest_frame, source._cached_read = (
            None,
            None,
            None,
        )
        source.latest_capture_diagnostics, source.recent_capture_diagnostics = {}, []
        source._last_accepted_frame_ns, source._closed = 0, False
        source._preprocessor_runtime = SimpleNamespace(close=Mock())
        self.source = source
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=True),
            window_handle=123,
            target_probe=self.probe,
            emergency_stop=self.local_stop,
        )
        self.pipeline = DecisionPipeline(
            source,
            Path(self.directory.name) / "game",
            controller=self.controller,
            dry_run=False,
            window_handle=123,
            window_process_id=456,
            input_mode="window_message",
            safety_guard=guard,
            fail_safe_runtime=self.runtime,
            external_watchdog=False,
        )
        self.pipeline.executor.live_executor = SimpleNamespace(
            execute=Mock(), release_all=Mock(), close=Mock()
        )
        self.pipeline.record_observation = Mock(wraps=self.pipeline.record_observation)
        self.pipeline.initialize_safety = Mock(wraps=self.pipeline.initialize_safety)
        self.pipeline.complete_execution_observation = Mock(
            wraps=self.pipeline.complete_execution_observation
        )
        source.fresh_frame_callback = self.pipeline.record_observation
        episode = ReferenceGame2048Episode.__new__(ReferenceGame2048Episode)
        episode.source, episode.pipeline = source, self.pipeline
        episode._comparison_preflight, episode._preflight_token = True, 0
        episode._preflight_deadline = None
        episode._safety_rearmed, episode._closed = False, False
        episode.launcher = SimpleNamespace(
            browser_process=SimpleNamespace(pid=456, poll=lambda: None),
            window_info=SimpleNamespace(handle=123, process_id=456),
            close=Mock(),
        )
        self.episode = episode
        self.addCleanup(episode.close)

    def capture(self, handle):
        self.captures += 1
        if self.capture_hook:
            self.capture_hook()
        return replace(self.frame, captured_at=time.monotonic())

    def preflight(self):
        return self.episode.capture_preflight(
            expected_rearm_token=0, deadline_monotonic=self.deadline
        )

    def activate(self):
        return self.episode.activate_preflight(
            expected_rearm_token=0, deadline_monotonic=self.deadline
        )

    def assert_no_input(self):
        self.pipeline.executor.live_executor.execute.assert_not_called()
        self.assertEqual(self.pipeline.load_execution_history(), [])
        self.assertFalse(self.pipeline.engine.has_pending_execution_observation)

    def test_safe_idle_capture_and_single_activation_keep_completion_separate(self):
        self.preflight()
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.pipeline.record_observation.assert_not_called()
        self.pipeline.initialize_safety.assert_not_called()
        self.pipeline.complete_execution_observation.assert_not_called()
        previous = self.source._last_accepted_frame_ns
        self.activate()
        self.assertGreater(self.source._last_accepted_frame_ns, previous)
        self.assertIs(self.runtime.state, FailSafeState.ACTIVE)
        self.assertTrue(self.episode._safety_rearmed)
        self.pipeline.initialize_safety.assert_called_once_with(expected_rearm_token=0)
        self.pipeline.complete_execution_observation.assert_not_called()
        self.episode.observe()
        self.pipeline.complete_execution_observation.assert_called_once()
        self.assert_no_input()

    def test_stop_in_capture_cancels_before_analysis_publication_without_rearming(self):
        self.capture_hook = lambda: self.controller.stop("user Stop")
        with self.assertRaises(ExecutionCancelled):
            self.preflight()
        self.assertEqual(self.captures, 1)
        self.assertEqual(self.source.capture_sequence, 0)
        self.assertIsNone(self.source._cached_read)
        self.assertFalse(self.episode._safety_rearmed)
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_original_token_is_not_replaced_after_user_rearm(self):
        self.controller.stop("user Stop")
        self.controller.start()
        with self.assertRaises(ExecutionCancelled):
            self.preflight()
        with self.assertRaises(RuntimeError):
            self.episode.capture_preflight(
                expected_rearm_token=1, deadline_monotonic=self.deadline
            )
        self.assertEqual(self.captures, 0)
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_default_and_local_stop_are_never_cleared_by_preflight(self):
        for event in (self.global_stop, self.local_stop):
            with self.subTest(event=event):
                event.trigger()
                with self.assertRaises(RuntimeError):
                    self.preflight()
                self.assertTrue(event.is_triggered())
                event.rearm()  # fixture cleanup; the API never rearms it.
        self.assertEqual(self.captures, 0)
        self.assert_no_input()

    def test_owner_pid_reuse_and_persisted_stop_cannot_initialize(self):
        self.target = replace(self.target, pid=999)
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assertEqual(self.captures, 0)
        self.target = replace(self.target, pid=456)
        self.preflight()
        persisted = self.runtime.lease_store.read()
        self.runtime.lease_store.write(
            {**persisted, "state": "emergency_stop", "reason": "native persisted Stop"}
        )
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertFalse(self.episode._safety_rearmed)
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.assertEqual(self.runtime.lease_store.read()["state"], "emergency_stop")
        self.assert_no_input()

    def test_second_activation_and_closed_source_are_rejected(self):
        self.preflight()
        self.activate()
        count = self.captures
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertEqual(self.captures, count)
        self.source.close()
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assert_no_input()

    def test_parity_work_is_idle_and_stop_during_it_cannot_activate(self):
        self.preflight()

        def inspect(*args):
            self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
            self.pipeline.initialize_safety.assert_not_called()
            self.pipeline.record_observation.assert_not_called()
            self.local_stop.trigger()
            return {"match": True}

        with patch(
            "tools.planning_frame_parity.validate_native_preprocessing",
            side_effect=inspect,
        ):
            with self.assertRaises(RuntimeError):
                self.episode.validate_preprocessing()
        self.assertTrue(self.local_stop.is_triggered())
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertFalse(self.episode._safety_rearmed)
        self.assert_no_input()

    def test_stop_during_analysis_prevents_source_publication(self):
        adapt = self.source.adapter.adapt

        def interrupted(*args):
            result = adapt(*args)
            self.global_stop.trigger()
            return result

        self.source.adapter.adapt = interrupted
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assertEqual(self.source.capture_sequence, 0)
        self.assertIsNone(self.source._cached_read)
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_cancelled_retry_does_not_capture_again_or_hold_run_lock(self):
        def stop_from_another_thread():
            worker = threading.Thread(target=lambda: self.controller.stop("user Stop"))
            worker.start()
            worker.join(0.2)
            self.assertFalse(
                worker.is_alive(), "capture must not run under controller metadata lock"
            )

        self.capture_hook = stop_from_another_thread
        with self.assertRaises(ExecutionCancelled):
            self.preflight()
        self.assertEqual(self.captures, 1)
        self.assert_no_input()

    def test_unready_retry_honors_stop_before_next_capture(self):
        adapt = self.source.adapter.adapt

        def unready(*args):
            obs, _ = adapt(*args)
            state = {**obs.features["game_2048"], "board": None}
            return replace(obs, features={**obs.features, "game_2048": state}), []

        self.source.adapter.adapt = unready
        with patch(
            "tools.run_2048_campaign.time.sleep",
            side_effect=lambda _: self.local_stop.trigger(),
        ):
            with self.assertRaises(RuntimeError):
                self.preflight()
        self.assertEqual(self.captures, 1)
        self.pipeline.record_observation.assert_not_called()
        self.pipeline.complete_execution_observation.assert_not_called()
        self.assert_no_input()

    def test_activation_io_stop_or_rearm_never_initializes_runtime(self):
        self.preflight()
        configure = self.runtime.ledger.configure

        def stopped(**kwargs):
            result = configure(**kwargs)
            self.controller.stop("user Stop")
            return result

        with (
            patch.object(self.runtime.ledger, "configure", side_effect=stopped),
            patch.object(
                self.runtime, "initialize", wraps=self.runtime.initialize
            ) as initialize,
        ):
            with self.assertRaises(ExecutionCancelled):
                self.activate()
        initialize.assert_not_called()
        self.assertFalse(self.episode._safety_rearmed)
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.assertEqual(self.runtime.epoch, "")
        self.assert_no_input()

    def test_pending_actual_and_recovery_are_not_preflight_states(self):
        self.pipeline.engine._pending_execution_receipt = object()
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assertTrue(self.pipeline.engine.has_pending_execution_observation)
        self.pipeline.complete_execution_observation.assert_not_called()
        self.pipeline.engine._pending_execution_receipt = None
        self.runtime._transition(FailSafeState.RECOVERY_REQUIRED, "fixture recovery")
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assertEqual(self.captures, 0)
        self.assert_no_input()

    def test_rearm_and_local_stop_during_capture_keep_original_epoch(self):
        self.capture_hook = self.controller.start
        with self.assertRaises(ExecutionCancelled):
            self.preflight()
        self.assertEqual(self.source.capture_sequence, 0)
        self.assertFalse(self.episode._safety_rearmed)
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_local_stop_during_activation_io_is_not_cleared(self):
        self.preflight()
        configure = self.runtime.ledger.configure

        def interrupted(**kwargs):
            result = configure(**kwargs)
            self.local_stop.trigger()
            return result

        with patch.object(self.runtime.ledger, "configure", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.activate()
        self.assertTrue(self.local_stop.is_triggered())
        self.assertFalse(self.episode._safety_rearmed)
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.assert_no_input()

    def test_stop_after_runtime_initialize_leaves_no_active_input_gate(self):
        self.preflight()
        initialize = self.runtime.initialize

        def interrupted(**kwargs):
            result = initialize(**kwargs)
            self.controller.stop("user Stop during initialization")
            return result

        with patch.object(self.runtime, "initialize", side_effect=interrupted):
            with self.assertRaises(ExecutionCancelled):
                self.activate()
        self.assertIs(self.runtime.state, FailSafeState.EMERGENCY_STOP)
        self.assertFalse(self.controller.is_running)
        self.assertEqual(self.controller.stop_reason, "user Stop during initialization")
        self.assertFalse(self.episode._safety_rearmed)
        self.assert_no_input()

    def test_activation_rejects_an_old_cached_frame_without_initializing(self):
        state = self.preflight()
        with patch.object(self.source, "capture_playable_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "stale or unrelated"):
                self.activate()
        self.pipeline.initialize_safety.assert_not_called()
        self.assertFalse(self.episode._safety_rearmed)
        self.assert_no_input()

    def test_already_active_and_same_handle_foreign_pid_cannot_preflight(self):
        self.preflight()
        self.activate()
        self.episode._safety_rearmed = False
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assert_no_input()

    def test_closed_unactivated_source_and_disabled_profile_are_rejected(self):
        self.episode._comparison_preflight = False
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.episode._comparison_preflight = True
        self.source.close()
        with self.assertRaises(RuntimeError):
            self.preflight()
        self.assertEqual(self.captures, 0)
        self.assert_no_input()

    def test_invalid_preflight_budget_cannot_extend_readiness(self):
        for value in (True, 0, -1, float("nan"), float("inf"), time.monotonic() + 46):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.episode.capture_preflight(
                    expected_rearm_token=0, deadline_monotonic=value
                )
        self.assertEqual(self.captures, 0)
        self.assert_no_input()

    def test_post_arm_error_stops_controller_before_secondary_storage_failure(self):
        self.preflight()
        initialize = self.runtime.initialize

        def activation_error(**kwargs):
            initialize(**kwargs)
            raise ValueError("original activation failure")

        def secondary_error(_reason):
            self.assertFalse(self.controller.is_running)
            raise OSError("secondary stop storage failure")

        with (
            patch.object(self.runtime, "initialize", side_effect=activation_error),
            patch.object(self.runtime, "emergency_stop", side_effect=secondary_error),
        ):
            with self.assertRaisesRegex(ValueError, "original activation failure"):
                self.activate()
        self.assertFalse(self.controller.is_running)
        self.assertFalse(self.episode._safety_rearmed)
        self.assertEqual(self.episode._preflight_cleanup_error_type, "OSError")
        with self.assertRaises(ExecutionCancelled):
            self.preflight()
        self.assert_no_input()

    def test_original_deadline_is_shared_and_cannot_be_extended(self):
        self.preflight()
        for changed in (self.deadline + 1, self.deadline - 1):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "original ready deadline"),
            ):
                self.episode.capture_preflight(
                    expected_rearm_token=0, deadline_monotonic=changed
                )
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_late_source_return_cannot_activate_or_publish_a_ready_gate(self):
        now = [100.0]
        self.deadline = 101.0
        with (
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
        ):
            state = self.preflight()

            def late_return(**kwargs):
                self.assertLessEqual(kwargs["timeout_seconds"], 1)
                self.assertIsNone(kwargs["on_fresh_frame"])
                now[0] = 102.0
                # Even a Source which returns without calling cancellation is bounded.
                return state

            with patch.object(
                self.source, "capture_playable_state", side_effect=late_return
            ):
                with self.assertRaisesRegex(RuntimeError, "ready deadline expired"):
                    self.activate()
        self.assertIs(self.runtime.state, FailSafeState.SAFE_IDLE)
        self.assertFalse(self.episode._safety_rearmed)
        self.pipeline.initialize_safety.assert_not_called()
        self.pipeline.record_observation.assert_not_called()
        self.pipeline.complete_execution_observation.assert_not_called()
        self.assert_no_input()

    def test_target_checks_consume_the_original_source_budget(self):
        now = [100.0]
        self.deadline = 101.0
        with (
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
        ):
            state = self.preflight()

            def slow_target(_handle):
                now[0] += 0.1
                return self.target

            def bounded_return(**kwargs):
                self.assertGreater(kwargs["timeout_seconds"], 0)
                self.assertLess(kwargs["timeout_seconds"], 0.95)
                return state

            with (
                patch.object(self.probe, "inspect", side_effect=slow_target),
                patch.object(
                    self.source, "capture_playable_state", side_effect=bounded_return
                ),
            ):
                self.preflight()
        self.pipeline.initialize_safety.assert_not_called()
        self.assert_no_input()

    def test_deadline_expiry_during_arm_stops_before_publishing_activation(self):
        for phase in ("initialize", "publish"):
            with self.subTest(phase=phase):
                # Use an independent real gate for both post-I/O and lock-entry races.
                case = ComparisonPreflightTests()
                case.setUp()
                now = [100.0]
                case.deadline = 101.0
                try:
                    with (
                        patch(
                            "tools.run_2048_campaign.time.monotonic",
                            side_effect=lambda: now[0],
                        ),
                        patch(
                            "tools.run_2048_campaign.time.monotonic_ns",
                            side_effect=lambda: int(now[0] * 1e9),
                        ),
                    ):
                        case.preflight()
                        now[0] = 100.1
                        initialize = case.runtime.initialize
                        publish = case.controller.run_if_current

                        def late_initialize(**kwargs):
                            initialize(**kwargs)
                            now[0] = 102.0

                        def late_publish(token, commit):
                            if commit.__name__ == "publish_activation":
                                now[0] = 102.0
                            return publish(token, commit)

                        with patch.object(
                            case.runtime if phase == "initialize" else case.controller,
                            "initialize" if phase == "initialize" else "run_if_current",
                            side_effect=late_initialize
                            if phase == "initialize"
                            else late_publish,
                        ):
                            with case.assertRaisesRegex(
                                RuntimeError, "ready deadline expired"
                            ):
                                case.activate()
                    self.assertFalse(case.controller.is_running)
                    self.assertFalse(case.episode._safety_rearmed)
                    self.assertIs(case.runtime.state, FailSafeState.EMERGENCY_STOP)
                    case.pipeline.initialize_safety.assert_called_once_with(
                        expected_rearm_token=0
                    )
                    case.pipeline.complete_execution_observation.assert_not_called()
                    case.assert_no_input()
                finally:
                    case.doCleanups()

    def test_publish_lock_wait_cannot_admit_a_five_second_old_capture(self):
        now = [100.0]
        self.deadline = 145.0
        with (
            patch("tools.run_2048_campaign.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "tools.run_2048_campaign.time.monotonic_ns",
                side_effect=lambda: int(now[0] * 1e9),
            ),
        ):
            self.preflight()
            now[0] = 100.1
            publish = self.controller.run_if_current

            def late_publish(token, commit):
                if commit.__name__ == "publish_activation":
                    now[0] = 106.0
                return publish(token, commit)

            with patch.object(
                self.controller, "run_if_current", side_effect=late_publish
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "observation expired before publication"
                ):
                    self.activate()
        self.assertFalse(self.controller.is_running)
        self.assertFalse(self.episode._safety_rearmed)
        self.assertIs(self.runtime.state, FailSafeState.EMERGENCY_STOP)
        self.pipeline.complete_execution_observation.assert_not_called()
        self.assert_no_input()

    def test_stop_events_during_publish_wait_keep_the_gate_unpublished(self):
        for stop_kind in ("local", "default"):
            with self.subTest(stop_kind=stop_kind):
                case = ComparisonPreflightTests()
                case.setUp()
                try:
                    case.preflight()
                    publish = case.controller.run_if_current
                    stop_event = (
                        case.local_stop if stop_kind == "local" else case.global_stop
                    )

                    def interrupted(token, commit):
                        if commit.__name__ == "publish_activation":
                            stop_event.trigger()
                        return publish(token, commit)

                    with patch.object(
                        case.controller, "run_if_current", side_effect=interrupted
                    ):
                        with case.assertRaisesRegex(
                            RuntimeError, "emergency stop prevents"
                        ):
                            case.activate()
                    self.assertTrue(stop_event.is_triggered())
                    self.assertFalse(case.controller.is_running)
                    self.assertFalse(case.episode._safety_rearmed)
                    self.assertIs(case.runtime.state, FailSafeState.EMERGENCY_STOP)
                    case.assert_no_input()
                finally:
                    case.doCleanups()
