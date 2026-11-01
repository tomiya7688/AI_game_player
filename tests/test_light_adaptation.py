from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import json
from hashlib import sha256

from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_promotion import KnowledgePromotionGate
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_identity import (
    LearningIdentityKind,
    LearningIdentityPolicy,
)
from ai_game_player.light_adaptation import LightAdaptationRunner
from ai_game_player.light_adaptation_budget import (
    LightAdaptationBudget,
    LightResourceSample,
)
from ai_game_player.light_execution_projection import (
    LightExecutionProjector,
    VerifiedActionEffect,
    VerifiedEffectSource,
)
from ai_game_player.light_utility import UtilityMeasurement
from ai_game_player.runtime_mode_config import (
    RuntimeMode,
    RuntimeModeConfig,
    RuntimeOperation,
    RuntimePhase,
)
from ai_game_player.runtime_mode_controller import RuntimeModeController
from ai_game_player.execution_receipt import ExecutionObservation, ExecutionReceipt
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.models import ActionCandidate
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.action_executor import ExecutionResult
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRuntimeConfiguration,
)
from ai_game_player.run_control import RunController
from ai_game_player.models import ScreenObservation
from ai_game_player.experience import ExperienceEvent
from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_light_validation import (
    KnowledgeLightValidationPolicy,
    light_canonical_events,
)
from ai_game_player.artifact_store import ArtifactIntegrityError
from ai_game_player.light_pending_store import read_pending_deltas

from test_knowledge_builder import effect_event
from test_knowledge_feature_adapter import make_context


SCOPE = KnowledgeScope("generic-game", "menu", "test-profile", "local", "isolated")
IDENTITY = LearningIdentityPolicy(LearningIdentityKind.STABLE_ID, SCOPE.profile_id)


def wait_for(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("bounded worker condition did not complete")


def mode_controller(mode=RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION):
    controller = RuntimeModeController(
        RuntimeModeConfig(mode), held_input_probe=lambda: False
    )
    controller.transition(RuntimePhase.INFER)
    return controller


def event(sequence, *, action="score-label", outcome="no_effect"):
    value = effect_event(sequence, outcome=outcome, action=action)
    return replace(value, payload={**value.payload, "scope": SCOPE.to_dict()})


def observed(context, *, generation=0, signature="after", status="executed"):
    receipt = ExecutionReceipt(
        context.snapshot_id,
        "score-label",
        generation,
        "menu",
        "before",
        status,
        status == "executed",
    )
    outcome = OutcomeEvent("score-label", "unknown", 0.1, False, True, (), "unverified")
    return ExecutionObservation(receipt, outcome, "menu", signature)


class LightAdaptationTests(unittest.TestCase):
    def test_corrupt_startup_knowledge_keeps_static_inference_and_preserves_bad_pointer(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY
            )
            initial.close()
            pointer = initial.gate.root / SCOPE.key / "state.json"
            pointer.write_text("{corrupt", encoding="utf-8")
            runner = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY
            )
            runner.start()
            try:
                observation, context = make_context()
                self.assertEqual(
                    runner.adapter.apply(
                        context, observation
                    ).knowledge_projection.to_dict()["status"],
                    "fallback",
                )
                self.assertFalse(runner.enqueue(event(1)))
                self.assertEqual(runner.metrics.updates, 0)
                self.assertEqual(pointer.read_text(), "{corrupt")
            finally:
                runner.close()

    def test_parallel_session_fact_checkpoint_merges_and_discards_only_acknowledged_facts(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            budget = LightAdaptationBudget(
                batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
            )
            first = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY, budget=budget
            )
            second = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY, budget=budget
            )
            stale = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY, budget=budget
            )
            first.enqueue(event(1))
            second.enqueue(event(2))
            stale.enqueue(event(1))
            first.close()
            second.close()
            self.assertEqual(
                [
                    fact.event_id
                    for fact in read_pending_deltas(first._pending_path, SCOPE)
                ],
                [event(1).event_id, event(2).event_id],
            )
            recovered = LightAdaptationRunner(
                SCOPE, root, mode_controller(), identity=IDENTITY, budget=budget
            )
            recovered.enqueue(event(3))
            recovered.enqueue(event(4))
            recovered.start()
            try:
                wait_for(lambda: recovered._pending is not None)
                self.assertTrue(recovered.publish_at_boundary())
                wait_for(lambda: recovered._authorized is None)
            finally:
                recovered.close()
            stale.close()
            self.assertEqual(read_pending_deltas(first._pending_path, SCOPE), ())
            self.assertEqual(first.gate.capture(SCOPE).snapshot.sample_count, 4)

    def test_private_actor_and_model_metadata_are_rejected_before_fact_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE, Path(directory), mode_controller(), identity=IDENTITY
            )
            value = event(1)
            private = replace(
                value,
                provenance=replace(
                    value.provenance,
                    actor_id="sensitive.actor",
                    model_id="private.model",
                    model_version="1",
                ),
            )
            self.assertFalse(runner.enqueue(private))
            runner.close()
            self.assertFalse(runner._pending_path.exists())
            self.assertEqual(runner.metrics.enqueued, 0)

    def test_report_retains_numeric_batch_for_identical_reconstruction_and_tamper_rejection(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory),
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(batch_size=4, max_update_ms=500),
            )
            before = runner.capture(SCOPE)
            try:
                for n in range(1, 5):
                    runner.enqueue(event(n))
                runner._update_batch()
                prepared = runner._pending[0]
                payload = runner.gate.reports.read(prepared.report)
                report = json.loads(payload)
                canonical = report["canonical_events"]
                encoded = json.dumps(
                    canonical,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                self.assertEqual(
                    sha256(encoded).hexdigest(), report["canonical_batch_sha256"]
                )
                restored = tuple(
                    ExperienceEvent.from_dict(value) for value in canonical
                )
                rebuilt = (
                    KnowledgeBuilder(KnowledgeBuilderPolicy(max_batch_events=64))
                    .build(
                        before,
                        restored,
                        scope=SCOPE,
                        version=prepared.snapshot.version,
                        created_at=prepared.snapshot.created_at,
                    )
                    .snapshot
                )
                policy = KnowledgeLightValidationPolicy(**report["policy"])
                rebuilt = policy.bound_candidate(before, rebuilt)
                self.assertEqual(rebuilt.sha256, prepared.snapshot.sha256)
                self.assertEqual(
                    rebuilt.to_dict()["source_experience_range"],
                    prepared.snapshot.to_dict()["source_experience_range"],
                )
                self.assertFalse(report["paired_replay_performed"])
                artifact_payload = (
                    runner.gate.reports._object_path(prepared.report.sha256) / "payload"
                )
                artifact_payload.write_bytes(
                    payload.replace(b"no_effect", b"successxx", 1)
                )
                prepared.authorize(0)
                with self.assertRaises(ArtifactIntegrityError):
                    runner.gate.acknowledge_publication(prepared)
                self.assertEqual(
                    runner.gate.capture(SCOPE).snapshot.sha256, before.sha256
                )
            finally:
                runner.close()

    def test_report_numeric_contract_rejects_unnecessary_model_metadata(self):
        value = event(1)
        with self.assertRaises(ValueError):
            light_canonical_events(
                (replace(value, model_id="model", model_version="1"),)
            )

    def test_unknown_backend_held_probe_never_executes_custom_property(self):
        observation, context = make_context()

        class Source:
            def read(self):
                return observation, [
                    ActionCandidate(
                        c.action_id,
                        c.kind,
                        c.label,
                        x=10 + 20 * i,
                        y=20,
                        confidence=0.95,
                    )
                    for i, c in enumerate(context.candidates)
                ]

        class UnknownBackend:
            @property
            def has_held_inputs(self):
                raise AssertionError("custom property must not run at publication")

        with tempfile.TemporaryDirectory() as directory:
            config = SessionRuntimeConfiguration(
                "ローカル規則",
                "",
                "",
                Path(directory),
                True,
                None,
                "mouse",
                None,
                knowledge_scope=SCOPE,
                learning_identity=IDENTITY,
                adaptation_budget=LightAdaptationBudget(
                    batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                ),
            )
            runtime = RuntimeComposition().create_session_runtime(
                config, source=Source(), controller=RunController()
            )
            try:
                initial = runtime.adaptation.runner.capture(SCOPE)
                for n in range(1, 5):
                    runtime.adaptation.runner.enqueue(event(n))
                wait_for(lambda: runtime.adaptation.runner._pending is not None)
                runtime.pipeline.executor.dry_run = False
                runtime.pipeline.executor.live_executor = UnknownBackend()
                self.assertFalse(runtime.adaptation.runner.publish_at_boundary())
                self.assertIs(runtime.adaptation.runner.capture(SCOPE), initial)
            finally:
                runtime.close()

    def test_stop_checkpoints_facts_without_learning_and_only_mode_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = LightAdaptationRunner(
                SCOPE,
                root,
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                ),
            )
            for n in range(1, 5):
                runner.enqueue(event(n))
            runner.mode.request_stop()
            runner.close()
            checkpoint = runner._pending_path.read_bytes()
            self.assertEqual(runner.gate.capture(SCOPE).snapshot.sample_count, 0)
            only = LightAdaptationRunner(
                SCOPE,
                root,
                mode_controller(RuntimeMode.INFERENCE_ONLY),
                identity=IDENTITY,
            )
            only.start()
            only.close()
            self.assertEqual(only.metrics.enqueued, 0)
            self.assertEqual(only._pending_path.read_bytes(), checkpoint)
            recovered = LightAdaptationRunner(
                SCOPE,
                root,
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                ),
            )
            recovered.start()
            try:
                wait_for(lambda: recovered._pending is not None)
                prepared = recovered._pending[0]
                report = json.loads(recovered.gate.reports.read(prepared.report))
                self.assertEqual(
                    [value["event_id"] for value in report["canonical_events"]],
                    [event(n).event_id for n in range(1, 5)],
                )
                self.assertTrue(recovered.publish_at_boundary())
                wait_for(lambda: recovered._authorized is None)
                self.assertEqual(recovered.gate.capture(SCOPE).snapshot.sample_count, 4)
            finally:
                recovered.close()

    def test_dynamic_ocr_ids_share_only_explicit_unique_label_profile(self):
        class Source:
            frame = 0

            def read(self):
                self.frame += 1
                return ScreenObservation(
                    "menu", 100, 80, features={"signature": f"before.{self.frame}"}
                ), [
                    ActionCandidate(
                        f"ocr.{self.frame}.score",
                        "click",
                        "Score",
                        x=10,
                        y=20,
                        confidence=0.95,
                    ),
                    ActionCandidate(
                        f"ocr.{self.frame}.move",
                        "click",
                        "Move",
                        x=40,
                        y=20,
                        confidence=0.95,
                    ),
                ]

        class Backend:
            has_held_inputs = False

            def execute(self, candidate):
                return ExecutionResult(
                    candidate.action_id, True, "fixture", "verified fixture input"
                )

        with tempfile.TemporaryDirectory() as directory:
            control = RunController()
            config = SessionRuntimeConfiguration(
                "ローカル規則",
                "",
                "",
                Path(directory),
                True,
                None,
                "mouse",
                None,
                knowledge_scope=SCOPE,
                learning_identity=LearningIdentityPolicy(
                    LearningIdentityKind.UNIQUE_LABEL, SCOPE.profile_id
                ),
                adaptation_budget=LightAdaptationBudget(
                    batch_size=4,
                    max_update_ms=500,
                    idle_flush_seconds=0.01,
                    min_publish_seconds=0.01,
                ),
            )
            runtime = RuntimeComposition().create_session_runtime(
                config, source=Source(), controller=control
            )
            runtime.pipeline.executor.dry_run = False
            runtime.pipeline.executor.live_executor = Backend()
            try:
                for number in range(4):
                    result = runtime.run_and_execute()
                    self.assertEqual(result.action_id, f"ocr.{number + 1}.score")
                    context = runtime.pipeline.engine.last_decision_context
                    after = ScreenObservation(
                        "menu", 100, 80, features={"signature": f"after.{number}"}
                    )
                    evidence = VerifiedActionEffect(
                        context.snapshot_id,
                        result.action_id,
                        control.rearm_token,
                        f"after.{number}",
                        "no_effect",
                        VerifiedEffectSource.DETERMINISTIC,
                        1,
                    )
                    runtime.complete_execution_observation(
                        after,
                        expected_rearm_token=control.rearm_token,
                        verified_effect=evidence,
                    )
                wait_for(lambda: runtime.adaptation.runner._pending is not None)
                self.assertEqual(runtime.run().action_id, "ocr.5.move")
                context = runtime.pipeline.engine.last_decision_context
                self.assertEqual(len(context.allowed_action_ids), 2)
                self.assertLess(
                    context.knowledge_projection.to_dict()["priors"]["ocr.5.score"], 0
                )
            finally:
                runtime.close()

    def test_cpu_memory_deadline_pressure_and_write_budget_defer(self):
        budget = LightAdaptationBudget()
        self.assertEqual(
            LightResourceSample(cpu_percent=99).reason(budget), "cpu_pressure"
        )
        self.assertEqual(
            LightResourceSample(available_memory_bytes=0).reason(budget),
            "memory_pressure",
        )
        self.assertEqual(
            LightResourceSample(deadline_remaining_ms=1).reason(budget),
            "inference_deadline",
        )
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory),
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4, max_writes_per_minute=1, max_update_ms=500
                ),
            )
            for n in range(1, 5):
                runner.enqueue(event(n))
            runner._update_batch()
            self.assertEqual(runner.metrics.reason, "write_budget")
            self.assertIsNone(runner._pending)
            self.assertEqual(runner.gate.capture(SCOPE).snapshot.sample_count, 0)
            runner.close()

    def test_verified_evidence_survives_mixed_unknown_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory),
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                ),
            )
            try:
                for n in range(1, 5):
                    runner.enqueue(
                        event(n, outcome="unknown" if n == 4 else "no_effect")
                    )
                runner._update_batch()
                self.assertIsNone(runner._pending)
                self.assertEqual(len(runner._waiting_events), 4)
                for n in range(5, 9):
                    runner.enqueue(
                        event(n, outcome="unknown" if n == 8 else "no_effect")
                    )
                runner._update_batch()
                self.assertIsNotNone(runner._pending)
                snapshot = runner._pending[0].snapshot
                stats = snapshot.to_dict()["action_outcome_stats"]["score-label"]
                self.assertEqual(stats["no_effect"], 4)
                self.assertEqual(stats["unknown"], 1)
                self.assertEqual(runner.metrics.queued, 3)
            finally:
                runner.close()

    def test_production_session_accumulates_receipts_and_freezes_next_step_version(
        self,
    ):
        initial, context = make_context()

        class Source:
            def read(self):
                return initial, [
                    ActionCandidate(
                        c.action_id,
                        c.kind,
                        c.label,
                        x=10 + 20 * i,
                        y=20,
                        confidence=0.95,
                    )
                    for i, c in enumerate(context.candidates)
                ]

        class Backend:
            has_held_inputs = False

            def execute(self, candidate):
                return ExecutionResult(candidate.action_id, True, "fixture", "recorded")

        for only in (False, True):
            with (
                self.subTest(inference_only=only),
                tempfile.TemporaryDirectory() as directory,
            ):
                controller = RunController()
                config = SessionRuntimeConfiguration(
                    "ローカル規則",
                    "",
                    "",
                    Path(directory),
                    True,
                    None,
                    "mouse",
                    None,
                    runtime_mode=RuntimeModeConfig(
                        RuntimeMode.INFERENCE_ONLY
                        if only
                        else RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION
                    ),
                    knowledge_scope=SCOPE,
                    learning_identity=IDENTITY,
                    adaptation_budget=LightAdaptationBudget(
                        batch_size=4,
                        max_update_ms=500,
                        idle_flush_seconds=0.01,
                        min_publish_seconds=0.01,
                    ),
                )
                runtime = RuntimeComposition().create_session_runtime(
                    config, source=Source(), controller=controller
                )
                runtime.pipeline.executor.dry_run = False
                runtime.pipeline.executor.live_executor = Backend()
                try:
                    for n in range(4):
                        result = runtime.run_and_execute()
                        current = runtime.pipeline.engine.last_decision_context
                        after = ScreenObservation(
                            "menu", 100, 80, features={"signature": f"background.{n}"}
                        )
                        evidence = VerifiedActionEffect(
                            current.snapshot_id,
                            result.action_id,
                            controller.rearm_token,
                            f"background.{n}",
                            "no_effect",
                            VerifiedEffectSource.DETERMINISTIC,
                            1,
                        )
                        execution = runtime.complete_execution_observation(
                            after,
                            expected_rearm_token=controller.rearm_token,
                            verified_effect=evidence,
                        )
                        self.assertTrue(execution.receipt.eligible_for_learning)
                    if only:
                        self.assertEqual(runtime.adaptation.runner.metrics.updates, 0)
                        self.assertIsNotNone(runtime.pipeline.engine.last_outcome_event)
                        self.assertFalse(
                            (Path(directory) / "light_adaptation").exists()
                        )
                    else:
                        wait_for(lambda: runtime.adaptation.runner._pending is not None)
                        pending_version = runtime.adaptation.runner._pending[
                            0
                        ].snapshot.version
                        runtime.run()
                        projection = runtime.pipeline.engine.last_decision_context.knowledge_projection.to_dict()
                        self.assertEqual(
                            projection["snapshot_version"], pending_version
                        )
                        self.assertTrue(
                            any(value < 0 for value in projection["priors"].values())
                        )
                        self.assertFalse(runtime.adaptation.mode.snapshot.step_active)
                finally:
                    runtime.close()
                self.assertFalse(runtime.adaptation.runner._worker.is_alive())

    def test_nonblocking_queue_resource_pressure_and_numeric_utility(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory),
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    queue_capacity=8,
                    batch_size=4,
                    max_update_ms=200,
                    min_publish_seconds=0.01,
                    idle_flush_seconds=0.01,
                ),
            )
            runner.set_resources(LightResourceSample(pressure=True))
            runner.start()
            latencies = []
            try:
                for _ in range(500):
                    started = time.perf_counter_ns()
                    runner.record_utility(UtilityMeasurement("decision_pipeline", 2.0))
                    latencies.append((time.perf_counter_ns() - started) / 1e6)
                self.assertEqual(runner.metrics.queued, 8)
                self.assertGreaterEqual(runner.metrics.dropped, 492)
                self.assertLess(sorted(latencies)[474], 10)
                self.assertEqual(runner.utility_estimates, ())
                runner.set_resources(LightResourceSample())
                wait_for(lambda: bool(runner.utility_estimates))
                self.assertEqual(runner.utility_estimates[0].quality_count, 0)
                self.assertIsNone(runner.utility_estimates[0].quality)
            finally:
                runner.close()
            self.assertFalse(runner._worker.is_alive())

    def test_inference_only_never_enqueues_builds_or_creates_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "not-created"
            runner = LightAdaptationRunner(
                SCOPE,
                root,
                mode_controller(RuntimeMode.INFERENCE_ONLY),
                identity=IDENTITY,
            )
            runner.start()
            try:
                self.assertFalse(runner.enqueue(event(1)))
                self.assertFalse(
                    runner.record_utility(UtilityMeasurement("pipeline", 1))
                )
                self.assertFalse(runner.publish_at_boundary())
                self.assertEqual(runner.metrics.queued, 0)
                self.assertEqual(runner.metrics.updates, 0)
                self.assertFalse(root.exists())
            finally:
                runner.close()

    def test_real_pipeline_reads_published_prior_only_on_next_step(self):
        observation, context = make_context()

        class Source:
            def read(self):
                return observation, [
                    ActionCandidate(
                        c.action_id,
                        c.kind,
                        c.label,
                        x=10 + 20 * i,
                        y=20,
                        confidence=0.95,
                    )
                    for i, c in enumerate(context.candidates)
                ]

        with tempfile.TemporaryDirectory() as directory:
            mode = mode_controller()
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory) / "learning",
                mode,
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4,
                    max_update_ms=500,
                    min_publish_seconds=0.01,
                    idle_flush_seconds=0.01,
                ),
            )
            pipeline = DecisionPipeline(
                Source(),
                Path(directory) / "game",
                RuleProvider(),
                knowledge_adapter=runner.adapter,
            )
            try:
                first = pipeline.run()
                self.assertEqual(first.action_id, "score-label")
                key = IDENTITY.keys(context)["score-label"]
                for n in range(1, 5):
                    self.assertTrue(runner.enqueue(event(n, action=key)))
                runner.start()
                wait_for(lambda: runner._pending is not None)
                self.assertEqual(pipeline.run().action_id, "score-label")
                self.assertTrue(runner.publish_at_boundary())
                self.assertEqual(pipeline.run().action_id, "move-control")
                self.assertEqual(
                    len(pipeline.engine.last_decision_context.allowed_action_ids), 2
                )
                wait_for(lambda: runner._authorized is None)
                restarted = KnowledgePromotionGate(runner.store, runner.gate.root)
                self.assertEqual(
                    restarted.capture(SCOPE).snapshot.version,
                    runner.capture(SCOPE).version,
                )
            finally:
                runner.close()
                pipeline.close()

    def test_stale_mode_and_stop_candidates_never_become_durable_active(self):
        for stop in (False, True):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                mode = mode_controller()
                runner = LightAdaptationRunner(
                    SCOPE,
                    Path(directory),
                    mode,
                    identity=IDENTITY,
                    budget=LightAdaptationBudget(
                        batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                    ),
                )
                initial = runner.capture(SCOPE)
                for n in range(1, 5):
                    runner.enqueue(event(n))
                runner.start()
                try:
                    wait_for(lambda: runner._pending is not None)
                    prepared = runner._pending[0]
                    if stop:
                        original = mode.run_if_allowed

                        def stop_during_publication(operation, epoch, callback):
                            if operation == RuntimeOperation.PROMOTION:
                                mode.request_stop()
                            return original(operation, epoch, callback)

                        with patch.object(
                            mode, "run_if_allowed", stop_during_publication
                        ):
                            self.assertFalse(runner.publish_at_boundary())
                    else:
                        mode.request_mode(RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY))
                        mode.apply_pending_mode()
                        self.assertFalse(runner.publish_at_boundary())
                    self.assertIsNone(prepared.authorized_epoch)
                    reopened = KnowledgePromotionGate(runner.store, runner.gate.root)
                    self.assertEqual(
                        reopened.capture(SCOPE).snapshot.sha256, initial.sha256
                    )
                finally:
                    runner.close()

    def test_checkpoint_failure_keeps_runtime_valid_and_closes_learning_until_recovery(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            runner = LightAdaptationRunner(
                SCOPE,
                Path(directory),
                mode_controller(),
                identity=IDENTITY,
                budget=LightAdaptationBudget(
                    batch_size=4, max_update_ms=500, idle_flush_seconds=0.01
                ),
            )
            for n in range(1, 5):
                runner.enqueue(event(n))
            runner.start()
            try:
                wait_for(lambda: runner._pending is not None)
                with patch.object(
                    runner.gate, "acknowledge_publication", side_effect=OSError("disk")
                ):
                    self.assertTrue(runner.publish_at_boundary())
                    valid = runner.capture(SCOPE)
                    wait_for(
                        lambda: runner.metrics.reason == "publication_checkpoint_failed"
                    )
                    self.assertFalse(runner.enqueue(event(5)))
                    self.assertIs(runner.capture(SCOPE), valid)
                    self.assertEqual(
                        runner.gate.capture(SCOPE).snapshot.version, "static.1"
                    )
                runner.retry_checkpoint()
                wait_for(lambda: runner._authorized is None)
                self.assertEqual(
                    runner.gate.capture(SCOPE).snapshot.version, valid.version
                )
            finally:
                runner.close()

    def test_profile_label_collisions_hold_learning_and_raw_label_is_not_key(self):
        _, context = make_context()
        policy = LearningIdentityPolicy(
            LearningIdentityKind.UNIQUE_LABEL, SCOPE.profile_id
        )
        keys = policy.keys(context)
        self.assertNotIn("Score", str(keys))
        self.assertEqual(len(keys), 2)
        duplicate = replace(context.candidates[1], label=context.candidates[0].label)
        self.assertEqual(
            policy.keys(
                replace(context, candidates=(context.candidates[0], duplicate))
            ),
            {},
        )
        self.assertEqual(LearningIdentityPolicy().keys(context), {})

    def test_projector_uses_receipt_generation_and_separate_action_sequence(self):
        _, context = make_context()
        projector = LightExecutionProjector(SCOPE, IDENTITY)
        builder = KnowledgeBuilder()
        snapshot = None
        all_events = []
        for n in range(160):
            current = replace(context, snapshot_id=f"frame.{n}")
            projector.bind(current)
            execution = observed(current, generation=0 if n < 140 else 1)
            evidence = VerifiedActionEffect(
                current.snapshot_id,
                "score-label",
                execution.receipt.action_generation,
                "after",
                "no_effect",
                VerifiedEffectSource.DETERMINISTIC,
                1,
            )
            projected = projector.project(execution, evidence)
            self.assertIsNotNone(projected)
            self.assertIsNone(projector.project(execution, evidence))
            all_events.append(projected)
        for n in range(0, 160, 16):
            snapshot = builder.build(
                snapshot,
                all_events[n : n + 16],
                scope=SCOPE,
                version=f"test.{n}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ).snapshot
        values = snapshot.to_dict()
        self.assertEqual(snapshot.sample_count, 160)
        self.assertEqual(len(values["builder_state"]["correlation_watermarks"]), 1)
        duplicate = builder.build(
            snapshot,
            all_events[-1:],
            scope=SCOPE,
            version="dupe",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(duplicate.duplicate_events, 1)

    def test_unverified_background_change_and_mismatched_effect_remain_unknown(self):
        _, context = make_context()
        for mismatch in (False, True):
            projector = LightExecutionProjector(SCOPE, IDENTITY)
            projector.bind(context)
            execution = observed(context)
            evidence = (
                VerifiedActionEffect(
                    context.snapshot_id,
                    "score-label",
                    1,
                    "after",
                    "success",
                    VerifiedEffectSource.DETERMINISTIC,
                    1,
                )
                if mismatch
                else None
            )
            projected = projector.project(execution, evidence)
            self.assertEqual(projected.payload["outcome"], "unknown")
            self.assertFalse(projected.payload["verified"])
        projector = LightExecutionProjector(SCOPE, IDENTITY)
        projector.bind(context)
        self.assertIsNone(projector.project(observed(context, status="dry_run")))

    def test_budget_rejects_unbounded_or_inconsistent_values(self):
        for kwargs in (
            {"max_cpu_percent": float("nan")},
            {"queue_capacity": 4096},
            {"max_event_bytes": 999999},
            {"min_publish_seconds": 0},
            {"batch_size": 65},
        ):
            with self.assertRaises(ValueError):
                LightAdaptationBudget(**kwargs)


if __name__ == "__main__":
    unittest.main()
