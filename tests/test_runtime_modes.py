from dataclasses import FrozenInstanceError, replace
from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player.event_journal import EventJournal
from ai_game_player.fail_safe_runtime import InputLedger
from ai_game_player.game_session import GameSessionController, SessionStep
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.runtime_mode_config import (
    DEFAULT_RUNTIME_MODE,
    RuntimeMode,
    RuntimeModeConfig,
    RuntimeOperation,
    RuntimePhase,
    load_runtime_mode_config,
)
from ai_game_player.runtime_mode_controller import (
    RuntimeModeController,
    RuntimeModePersistenceError,
    RuntimeModeTransitionError,
)


class RuntimeModeContractTests(unittest.TestCase):
    def make_controller(self, mode=DEFAULT_RUNTIME_MODE, **kwargs):
        return RuntimeModeController(
            RuntimeModeConfig(mode),
            held_input_probe=kwargs.pop("held_input_probe", lambda: False),
            **kwargs,
        )

    def test_five_modes_eight_phases_and_default_light_are_fixed(self):
        self.assertEqual(len(RuntimeMode), 5)
        self.assertEqual(len(RuntimePhase), 8)
        config = RuntimeModeConfig()
        self.assertEqual(config.mode, RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION)
        self.assertTrue(config.flags["light_update_enabled"])
        self.assertFalse(config.flags["heavy_training_enabled"])
        self.assertIs(RuntimeOperation.EVALUATION, RuntimeOperation.ARTIFACT_EVALUATION)
        with self.assertRaises(FrozenInstanceError):
            config.mode = RuntimeMode.INFERENCE_ONLY

    def test_machine_readable_schema_default_and_phase_matrix_match_contract(self):
        root = Path(__file__).resolve().parents[1] / "config"
        schema = json.loads((root / "runtime_mode.schema.json").read_text())
        default = json.loads((root / "runtime_mode.default.json").read_text())
        matrix = json.loads((root / "runtime_mode_operations.json").read_text())
        self.assertEqual(RuntimeModeConfig.from_dict(default), RuntimeModeConfig())
        self.assertEqual(schema["$defs"]["mode"]["enum"], list(RuntimeMode))
        self.assertEqual(schema["$defs"]["phase"]["enum"], list(RuntimePhase))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(matrix["evaluation_scope"], "learning_artifact_only")
        self.assertEqual(set(matrix["modes"]), set(RuntimeMode))
        for mode, conditional in zip(RuntimeMode, schema["allOf"]):
            self.assertEqual(conditional["if"]["properties"]["mode"]["const"], mode)
            expected = RuntimeModeConfig(mode)
            self.assertEqual(
                conditional["then"]["properties"],
                {key: {"const": value} for key, value in expected.flags.items()},
            )
            declared = matrix["modes"][mode]
            self.assertEqual(declared["config"], expected.to_dict())
            self.assertEqual(set(declared["phases"]), set(RuntimePhase))
            for phase in RuntimePhase:
                self.assertEqual(
                    declared["phases"][phase],
                    sorted(op.value for op in expected.operations(phase)),
                )

    def test_existing_game_session_factory_shares_run_generation_and_stop(self):
        run_control = RunController()
        created = []

        def factory():
            mode = self.make_controller(run_control=run_control)
            mode.transition(RuntimePhase.INFER)
            created.append(mode)
            return SimpleNamespace(close=mode.request_stop)

        session = GameSessionController(
            factory, lambda _runtime, _command: SessionStep(), run_control=run_control
        )
        session.start()
        mode = created[-1]
        token = mode.begin_step("champion.1")
        mode.finish_step(token)
        session.stop()
        self.assertFalse(mode.permits(RuntimeOperation.ACTION))
        self.assertEqual(mode.snapshot.phase, RuntimePhase.STOPPING)
        session.start()
        self.assertIsNot(created[-1], mode)
        self.assertTrue(created[-1].permits(RuntimeOperation.ACTION))
        session.stop()

    def test_mode_flags_roundtrip_and_contradictory_overrides_are_rejected(self):
        for mode in RuntimeMode:
            config = RuntimeModeConfig(mode, "budget.small")
            self.assertEqual(RuntimeModeConfig.from_dict(config.to_dict()), config)
            self.assertTrue(config.flags["safety_core_enabled"])
            flags = config.flags
            flags["safety_core_enabled"] = False
            self.assertTrue(config.flags["safety_core_enabled"])
            for flag in config.flags:
                if type(config.flags[flag]) is not bool:
                    continue
                broken = dict(config.to_dict(), **{flag: not config.flags[flag]})
                with self.subTest(mode=mode, flag=flag), self.assertRaises(ValueError):
                    RuntimeModeConfig.from_dict(broken)
        for changes in (
            {"schema_version": True},
            {"schema_version": 2},
            {"mode": "unknown"},
            {"resource_policy_id": "../unsafe"},
            {"resource_policy_id": "sk-secret-value"},
            {"learning_enabled": True},
            {"inference_enabled": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RuntimeModeConfig.from_dict(
                    dict(RuntimeModeConfig().to_dict(), **changes)
                )
        fallback, reason = load_runtime_mode_config({"learning_enabled": True})
        self.assertEqual(fallback, RuntimeModeConfig())
        self.assertEqual(reason, "invalid_config")

    def test_inference_only_keeps_action_and_safety_but_no_learning(self):
        controller = self.make_controller(RuntimeMode.INFERENCE_ONLY)
        controller.transition(RuntimePhase.INFER)
        self.assertTrue(controller.permits(RuntimeOperation.INFERENCE))
        self.assertTrue(controller.permits(RuntimeOperation.ACTION))
        self.assertTrue(controller.permits(RuntimeOperation.EXPERIENCE_WRITE))
        for operation in (
            RuntimeOperation.LIGHT_UPDATE,
            RuntimeOperation.KNOWLEDGE_BUILD,
            RuntimeOperation.HEAVY_TRAINING,
            RuntimeOperation.ARTIFACT_EVALUATION,
            RuntimeOperation.PROMOTION,
        ):
            self.assertFalse(controller.permits(operation))
        with self.assertRaises(RuntimeModeTransitionError):
            controller.transition(RuntimePhase.LEARN)

    def test_learning_only_never_grants_gameplay_and_alternating_changes_phase(self):
        learner = self.make_controller(RuntimeMode.LEARNING_ONLY)
        with self.assertRaises(RuntimeModeTransitionError):
            learner.transition(RuntimePhase.INFER)
        learner.transition(RuntimePhase.LEARN)
        self.assertTrue(learner.permits(RuntimeOperation.HEAVY_TRAINING))
        self.assertFalse(learner.permits(RuntimeOperation.ACTION))
        self.assertFalse(learner.permits(RuntimeOperation.EXPERIENCE_WRITE))
        alternating = self.make_controller(RuntimeMode.ALTERNATING_INFER_LEARN)
        alternating.transition(RuntimePhase.INFER)
        self.assertFalse(alternating.permits(RuntimeOperation.LIGHT_UPDATE))
        alternating.transition(RuntimePhase.LEARN)
        self.assertTrue(alternating.permits(RuntimeOperation.HEAVY_TRAINING))
        self.assertFalse(alternating.permits(RuntimeOperation.INFERENCE))
        alternating.transition(RuntimePhase.EVALUATE)
        alternating.record_evaluation(
            "candidate.2", True, expected_epoch=alternating.snapshot.epoch
        )
        alternating.transition(RuntimePhase.PROMOTE)
        alternating.transition(RuntimePhase.INFER)
        self.assertEqual(alternating.snapshot.mode, RuntimeMode.ALTERNATING_INFER_LEARN)
        self.assertFalse(alternating.snapshot.evaluation_passed)

    def test_light_and_concurrent_modes_have_distinct_training_permissions(self):
        light = self.make_controller()
        light.transition(RuntimePhase.INFER)
        for operation in (
            RuntimeOperation.INFERENCE,
            RuntimeOperation.ACTION,
            RuntimeOperation.LIGHT_UPDATE,
            RuntimeOperation.KNOWLEDGE_BUILD,
            RuntimeOperation.ARTIFACT_EVALUATION,
        ):
            self.assertTrue(light.permits(operation))
        self.assertFalse(light.permits(RuntimeOperation.HEAVY_TRAINING))
        concurrent = self.make_controller(RuntimeMode.INFERENCE_WITH_LEARNING)
        concurrent.transition(RuntimePhase.INFER)
        self.assertTrue(concurrent.permits(RuntimeOperation.HEAVY_TRAINING))

    def test_mode_change_waits_for_step_and_held_input_without_replacing_artifact(self):
        held = {"value": False}
        controller = self.make_controller(held_input_probe=lambda: held["value"])
        controller.transition(RuntimePhase.INFER)
        token = controller.begin_step("champion.1")
        original_epoch = controller.snapshot.epoch
        controller.request_mode(RuntimeModeConfig(RuntimeMode.LEARNING_ONLY))
        self.assertEqual(controller.snapshot.mode, DEFAULT_RUNTIME_MODE)
        self.assertEqual(controller.snapshot.epoch, original_epoch)
        self.assertEqual(controller.snapshot.artifact_version, "champion.1")
        with self.assertRaises(RuntimeModeTransitionError):
            controller.apply_pending_mode()
        with self.assertRaises(RuntimeModeTransitionError):
            controller.transition(RuntimePhase.LEARN)
        controller.finish_step(token)
        held["value"] = True
        with self.assertRaises(RuntimeModeTransitionError):
            controller.apply_pending_mode()
        held["value"] = False
        controller.apply_pending_mode()
        self.assertEqual(controller.snapshot.mode, RuntimeMode.LEARNING_ONLY)
        self.assertEqual(controller.snapshot.phase, RuntimePhase.IDLE)
        self.assertEqual(controller.snapshot.artifact_version, "champion.1")

    def test_unknown_probe_blocks_learning_boundary_but_does_not_disable_inference(
        self,
    ):
        for probe in (
            None,
            lambda: None,
            lambda: (_ for _ in ()).throw(OSError("unavailable")),
        ):
            controller = RuntimeModeController(held_input_probe=probe)
            controller.transition(RuntimePhase.INFER)
            self.assertTrue(controller.permits(RuntimeOperation.ACTION))
            with self.assertRaises(RuntimeModeTransitionError):
                controller.transition(RuntimePhase.LEARN)

    def test_actual_input_ledger_is_a_usable_held_input_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = InputLedger(Path(directory) / "input.json")

            def held():
                current = ledger.snapshot()
                return bool(current["held_keys"] or current["held_mouse"])

            controller = self.make_controller(
                RuntimeMode.ALTERNATING_INFER_LEARN, held_input_probe=held
            )
            controller.transition(RuntimePhase.INFER)
            ledger.hold_key(65, 1)
            with self.assertRaises(RuntimeModeTransitionError):
                controller.transition(RuntimePhase.LEARN)
            ledger.release_key(65)
            ledger.hold_mouse("left", 1)
            with self.assertRaises(RuntimeModeTransitionError):
                controller.transition(RuntimePhase.LEARN)
            ledger.release_mouse("left")
            controller.transition(RuntimePhase.LEARN)

    def test_step_tokens_reject_double_start_foreign_completion_and_allow_stop_cleanup(
        self,
    ):
        first = self.make_controller()
        first.transition(RuntimePhase.INFER)
        token = first.begin_step("champion.1")
        with self.assertRaises(RuntimeModeTransitionError):
            first.begin_step("champion.2")
        with self.assertRaises(RuntimeModeTransitionError):
            first.finish_step(replace(token, owner_id="different"))
        first.request_stop()
        first.finish_step(token)
        self.assertFalse(first.snapshot.step_active)
        self.assertEqual(first.snapshot.phase, RuntimePhase.STOPPING)
        with self.assertRaises(RuntimeModeTransitionError):
            first.finish_step(token)

    def test_stop_is_prioritized_for_every_mode_and_phase_even_under_corrupt_state(
        self,
    ):
        for mode in RuntimeMode:
            for phase in RuntimePhase:
                with self.subTest(mode=mode, phase=phase):
                    controller = self.make_controller(mode)
                    # Fault injection includes impossible combinations: Stop must still win.
                    controller._state = replace(controller.snapshot, phase=phase)
                    controller.request_stop()
                    self.assertEqual(controller.snapshot.phase, RuntimePhase.STOPPING)
                    self.assertFalse(controller.run_control.is_running)
                    self.assertTrue(controller.permits(RuntimeOperation.SAFETY_CORE))
                    self.assertTrue(
                        all(
                            not controller.permits(op)
                            for op in RuntimeOperation
                            if op != RuntimeOperation.SAFETY_CORE
                        )
                    )

    def test_stale_work_and_rearmed_run_are_rejected_before_commit(self):
        controller = self.make_controller()
        controller.transition(RuntimePhase.INFER)
        epoch = controller.snapshot.epoch
        calls = []
        controller.run_if_allowed(
            RuntimeOperation.KNOWLEDGE_BUILD, epoch, lambda: calls.append("first")
        )
        controller.transition(RuntimePhase.LEARN)
        with self.assertRaises(RuntimeModeTransitionError):
            controller.run_if_allowed(
                RuntimeOperation.KNOWLEDGE_BUILD, epoch, lambda: calls.append("stale")
            )
        controller.run_control.stop()
        controller.run_control.start()
        with self.assertRaises(ExecutionCancelled):
            controller.run_if_allowed(
                RuntimeOperation.KNOWLEDGE_BUILD,
                controller.snapshot.epoch,
                lambda: calls.append("old-run"),
            )
        self.assertEqual(calls, ["first"])

    def test_failed_evaluation_and_training_failure_preserve_champion_version(self):
        controller = self.make_controller()
        controller.transition(RuntimePhase.INFER)
        token = controller.begin_step("champion.1")
        controller.finish_step(token)
        controller.transition(RuntimePhase.EVALUATE)
        controller.record_evaluation(
            "candidate.2", False, expected_epoch=controller.snapshot.epoch
        )
        with self.assertRaises(RuntimeModeTransitionError):
            controller.transition(RuntimePhase.PROMOTE)
        controller.fail("training_failed")
        self.assertEqual(controller.snapshot.artifact_version, "champion.1")
        self.assertEqual(controller.snapshot.phase, RuntimePhase.ERROR)
        controller.recover_to_idle()
        self.assertTrue(controller.snapshot.stop_requested)
        with self.assertRaises(ExecutionCancelled):
            controller.transition(RuntimePhase.INFER)
        controller.run_control.start()
        controller.resume_at_boundary()
        self.assertEqual(controller.snapshot.phase, RuntimePhase.IDLE)
        self.assertEqual(controller.snapshot.artifact_version, "champion.1")

    def test_degrade_preserves_user_selection_and_requires_safe_apply(self):
        controller = self.make_controller(RuntimeMode.INFERENCE_WITH_LEARNING)
        controller.transition(RuntimePhase.INFER)
        controller.degrade("resource_pressure")
        self.assertEqual(controller.snapshot.mode, RuntimeMode.INFERENCE_WITH_LEARNING)
        controller.apply_pending_mode()
        self.assertEqual(controller.snapshot.mode, DEFAULT_RUNTIME_MODE)
        self.assertEqual(
            controller.snapshot.selected_config.mode,
            RuntimeMode.INFERENCE_WITH_LEARNING,
        )
        self.assertEqual(controller.snapshot.degrade_reason, "resource_pressure")
        controller.degrade("resource_pressure")
        controller.apply_pending_mode()
        self.assertEqual(controller.snapshot.mode, RuntimeMode.INFERENCE_ONLY)
        with self.assertRaises(RuntimeModeTransitionError):
            controller.degrade("resource_pressure")

    def test_degrade_keeps_stricter_pending_mode_and_rejects_stale_request(self):
        controller = self.make_controller(RuntimeMode.INFERENCE_WITH_LEARNING)
        controller.request_mode(RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY))
        controller.degrade("resource_pressure")
        controller.apply_pending_mode()
        self.assertEqual(controller.snapshot.mode, RuntimeMode.INFERENCE_ONLY)

        controller = self.make_controller(RuntimeMode.INFERENCE_WITH_LEARNING)
        original = controller._metadata_change

        def concurrent_mode_change(event, **kwargs):
            controller.request_mode(RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY))
            controller.apply_pending_mode()
            return original(event, **kwargs)

        def inject_change(event, **kwargs):
            with patch.object(controller, "_metadata_change", original):
                return concurrent_mode_change(event, **kwargs)

        with patch.object(controller, "_metadata_change", inject_change):
            with self.assertRaises(RuntimeModeTransitionError):
                controller.degrade("resource_pressure")
        self.assertEqual(controller.snapshot.mode, RuntimeMode.INFERENCE_ONLY)
        self.assertIsNone(controller.snapshot.pending_config)

    def test_journal_records_modes_phase_versions_and_degrade_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EventJournal(
                Path(directory) / "events.sqlite", session_id="mode-test"
            )
            controller = self.make_controller(
                RuntimeMode.INFERENCE_WITH_LEARNING, journal=journal
            )
            controller.transition(RuntimePhase.INFER)
            controller.degrade("resource_pressure")
            controller.apply_pending_mode()
            self.assertTrue(controller.flush())
            with closing(
                sqlite3.connect(Path(directory) / "events.sqlite")
            ) as connection:
                records = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT envelope_json FROM events ORDER BY sequence"
                    )
                ]
            self.assertGreaterEqual(len(records), 3)
            payloads = [event["payload"] for event in records]
            self.assertEqual(payloads[-1]["phase"], "IDLE")
            self.assertEqual(
                payloads[-1]["effective_config"]["mode"], DEFAULT_RUNTIME_MODE.value
            )
            self.assertEqual(payloads[-1]["degrade_reason"], "resource_pressure")
            self.assertEqual(payloads[-1]["version"], controller.snapshot.version)
            journal.close()

    def test_crash_recovery_from_learning_and_promotion_never_restarts_work(self):
        for target in (RuntimePhase.LEARN, RuntimePhase.PROMOTE):
            with self.subTest(phase=target), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "mode.json"
                controller = self.make_controller(
                    RuntimeMode.LEARNING_ONLY, state_path=path
                )
                controller.transition(RuntimePhase.LEARN)
                if target == RuntimePhase.PROMOTE:
                    controller.transition(RuntimePhase.EVALUATE)
                    controller.record_evaluation(
                        "candidate.2", True, expected_epoch=controller.snapshot.epoch
                    )
                    controller.transition(RuntimePhase.PROMOTE)
                controller.flush()
                recovered = RuntimeModeController.restore(
                    path, held_input_probe=lambda: False
                )
                self.assertEqual(recovered.snapshot.phase, RuntimePhase.IDLE)
                self.assertTrue(recovered.snapshot.stop_requested)
                self.assertFalse(recovered.snapshot.evaluation_passed)
                self.assertEqual(
                    recovered.snapshot.selected_config.mode, RuntimeMode.LEARNING_ONLY
                )
                self.assertFalse(recovered.permits(RuntimeOperation.HEAVY_TRAINING))

    def test_invalid_checkpoint_uses_safe_default_and_requires_explicit_resume(self):
        bad_values = [
            b"{",
            b'{"schema_version":1,"schema_version":2}',
            b"x" * (64 * 1024 + 1),
        ]
        for raw in bad_values:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "mode.json"
                path.write_bytes(raw)
                recovered = RuntimeModeController.restore(
                    path, held_input_probe=lambda: False
                )
                self.assertEqual(recovered.snapshot.mode, DEFAULT_RUNTIME_MODE)
                self.assertEqual(recovered.snapshot.phase, RuntimePhase.IDLE)
                self.assertTrue(recovered.snapshot.stop_requested)
                self.assertEqual(
                    recovered.snapshot.degrade_reason, "invalid_checkpoint"
                )

    def test_slow_held_probe_does_not_hold_stop_lock_and_late_transition_loses(self):
        entered, release = threading.Event(), threading.Event()

        def probe():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("probe test timeout")
            return True

        controller = self.make_controller(held_input_probe=probe)
        controller.transition(RuntimePhase.INFER)
        errors = []

        def transition():
            try:
                controller.transition(RuntimePhase.LEARN)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=transition)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.perf_counter()
            controller.request_stop()
            self.assertLess(time.perf_counter() - start, 0.2)
            self.assertEqual(controller.snapshot.phase, RuntimePhase.STOPPING)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], ExecutionCancelled)
        self.assertEqual(controller.snapshot.phase, RuntimePhase.STOPPING)

    def test_resume_cannot_erase_new_stop_waiting_for_run_lock(self):
        controller = self.make_controller()
        controller.request_stop()
        controller.recover_to_idle()
        controller.run_control.start()
        entered = threading.Event()
        errors = []
        original = controller.run_control.run_if_current

        def intercept(generation, callback):
            def delayed():
                entered.set()
                return callback()

            return original(generation, delayed)

        def resume():
            try:
                controller.resume_at_boundary()
            except Exception as error:
                errors.append(error)

        # resume captures its snapshot first, then waits at its held-input probe.
        probe_entered, probe_release = threading.Event(), threading.Event()

        def probe():
            probe_entered.set()
            if not probe_release.wait(2):
                raise RuntimeError("probe test timeout")
            return False

        controller._held_input_probe = probe
        with patch.object(controller.run_control, "run_if_current", intercept):
            worker = threading.Thread(target=resume)
            worker.start()
            self.assertTrue(probe_entered.wait(1))
            with controller._lock:
                probe_release.set()
                self.assertTrue(entered.wait(1))
                stopper = threading.Thread(target=controller.request_stop)
                stopper.start()
                deadline = time.monotonic() + 1
                while not controller._stop_pending and time.monotonic() < deadline:
                    threading.Event().wait(0.001)
                self.assertTrue(controller._stop_pending)
            worker.join(2)
            stopper.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ExecutionCancelled)
        self.assertTrue(controller.snapshot.stop_requested)
        self.assertEqual(controller.snapshot.phase, RuntimePhase.STOPPING)

    def test_slow_journal_flush_does_not_block_stop_or_allow_stale_publication(self):
        entered, release = threading.Event(), threading.Event()

        class SlowJournal:
            def append(self, *args, **kwargs):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("journal test timeout")

        controller = self.make_controller(journal=SlowJournal())
        controller.transition(RuntimePhase.INFER)
        epoch = controller.snapshot.epoch
        worker = threading.Thread(target=controller.flush)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.perf_counter()
            controller.request_stop()
            self.assertLess(time.perf_counter() - start, 0.2)
            with self.assertRaises(ExecutionCancelled):
                controller.run_if_allowed(
                    RuntimeOperation.KNOWLEDGE_BUILD,
                    epoch,
                    lambda: self.fail("late publication"),
                )
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_journal_failure_and_backpressure_fail_closed_without_mutating_artifact(
        self,
    ):
        class BrokenJournal:
            def append(self, *args, **kwargs):
                raise OSError("failure")

        controller = self.make_controller(journal=BrokenJournal())
        controller.transition(RuntimePhase.INFER)
        token = controller.begin_step("champion.1")
        controller.finish_step(token)
        with self.assertRaises(RuntimeModePersistenceError):
            controller.flush()
        self.assertEqual(controller.snapshot.phase, RuntimePhase.ERROR)
        self.assertEqual(controller.snapshot.artifact_version, "champion.1")
        self.assertFalse(controller.run_control.is_running)
        bounded = self.make_controller(journal=BrokenJournal(), max_pending_events=1)
        bounded.transition(RuntimePhase.INFER)
        with self.assertRaises(RuntimeModePersistenceError):
            bounded.request_mode(RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY))
        self.assertEqual(bounded.snapshot.phase, RuntimePhase.ERROR)
        bounded.request_stop()
        self.assertEqual(bounded.snapshot.phase, RuntimePhase.STOPPING)

    def test_inference_only_does_not_disable_outcome_verifier_or_safety(self):
        class Source:
            def read(self):
                return ScreenObservation(
                    "playing", 100, 100, features={"signature": "before"}
                ), [ActionCandidate("move", "key", "LEFT")]

        mode = self.make_controller(RuntimeMode.INFERENCE_ONLY)
        mode.transition(RuntimePhase.INFER)
        self.assertFalse(mode.permits(RuntimeOperation.ARTIFACT_EVALUATION))
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), dry_run=True)
            try:
                result = pipeline.run_and_execute()
                self.assertEqual(result.action_id, "move")
                self.assertIsNotNone(pipeline.engine.last_reliability_result)
                outcome = pipeline.engine.outcome_detector.detect(
                    ScreenObservation("before", 100, 100),
                    "move",
                    ScreenObservation("after", 100, 100, ["CLEAR"]),
                )
                self.assertEqual(outcome.status, "success")
                self.assertIsNotNone(pipeline.last_safety_result)
            finally:
                pipeline.close()

    def test_seeded_state_machine_sequences_preserve_permissions_and_stop_invariants(
        self,
    ):
        rng = random.Random(421)
        for mode in RuntimeMode:
            controller = self.make_controller(mode)
            active = None
            for _ in range(300):
                command = rng.randrange(6)
                try:
                    if command == 0:
                        controller.transition(rng.choice(tuple(RuntimePhase)))
                    elif command == 1 and active is None:
                        active = controller.begin_step("champion.1")
                    elif command == 2 and active is not None:
                        controller.finish_step(active)
                        active = None
                    elif command == 3:
                        controller.request_mode(
                            RuntimeModeConfig(rng.choice(tuple(RuntimeMode)))
                        )
                        controller.apply_pending_mode()
                    elif (
                        command == 4
                        and controller.snapshot.phase
                        in {RuntimePhase.ERROR, RuntimePhase.STOPPING}
                        and active is None
                    ):
                        controller.recover_to_idle()
                        controller.run_control.start()
                        controller.resume_at_boundary()
                    elif (
                        command == 5
                        and controller.snapshot.phase == RuntimePhase.EVALUATE
                    ):
                        controller.record_evaluation(
                            "candidate.2",
                            bool(rng.randrange(2)),
                            expected_epoch=controller.snapshot.epoch,
                        )
                except (RuntimeModeTransitionError, ExecutionCancelled):
                    pass
                state = controller.snapshot
                self.assertTrue(controller.permits(RuntimeOperation.SAFETY_CORE))
                if state.stop_requested:
                    self.assertTrue(
                        all(
                            not controller.permits(op)
                            for op in RuntimeOperation
                            if op != RuntimeOperation.SAFETY_CORE
                        )
                    )
                if controller.permits(RuntimeOperation.ACTION):
                    self.assertEqual(state.phase, RuntimePhase.INFER)
                    self.assertNotEqual(state.mode, RuntimeMode.LEARNING_ONLY)
                if state.mode in {
                    RuntimeMode.INFERENCE_ONLY,
                    RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION,
                }:
                    self.assertFalse(
                        controller.permits(RuntimeOperation.HEAVY_TRAINING)
                    )
                if active is not None:
                    self.assertEqual(state.artifact_version, active.artifact_version)


if __name__ == "__main__":
    unittest.main()
