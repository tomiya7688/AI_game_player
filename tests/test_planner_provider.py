from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionWithheld
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.planner_observation import PlannerObservationInput
from ai_game_player.planner_provider import PlannerDecisionProvider
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_result import WeightedValue
from tools.planning_policy_fixture import make_belief, make_plan
from test_decision_orchestration import Source, Backend


class Adapter:
    def __init__(self):
        self.generation = 0
        self.epoch = 0
        self.version = 0

    def prepare(self, context, observation, action_epoch, run_generation, now_ns):
        self.generation, self.epoch = run_generation, action_epoch
        self.version += 1
        meta = observation.features["capture_metadata"]
        base = make_belief()
        scope = replace(
            base.context,
            snapshot_id=context.snapshot_id,
            frame_ids=(meta["frame_id"],),
            frame_sequence=meta["monotonic_ns"],
            turn=action_epoch,
            action_epoch=action_epoch,
            source_id=meta["source_id"],
            source_generation=meta["source_generation"],
        )
        candidates = tuple(
            replace(
                c,
                context=scope,
                evidence_refs=tuple(
                    replace(e, context=scope, frame_id=scope.frame_ids[0])
                    for e in c.evidence_refs
                ),
            )
            for c in base.candidates
        )
        belief = replace(
            base,
            context=scope,
            candidates=candidates,
            hypothesis_set_version=self.version,
        )
        self.current = belief
        return PlannerObservationInput(belief, "fixture", "goal", "0" * 64)

    def current_belief(self, context):
        return self.current if self.current.context == context else None

    def semantic_state(self, observation, epoch):
        return (
            "0" * 64 if not observation.features.get("changed_state") else "1" * 64,
            epoch,
        )


class Planner:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = 0
        self.after = None

    def plan(self, belief, allowed, **kwargs):
        self.calls += 1
        result = replace(
            make_plan(
                belief,
                {
                    a: tuple((WeightedValue(0.8, 1),) for _ in belief.candidates)
                    for a in allowed
                },
            ),
            run_generation=self.adapter.generation,
        )
        if self.after:
            result = self.after(result)
        return result


class ChangingSource(Source):
    def __init__(self, change=None):
        super().__init__()
        self.change = change

    def read_fresh(self):
        observation, candidates = super().read_fresh()
        features = dict(observation.features)
        if self.change == "state":
            features["changed_state"] = True
        elif self.change == "signature":
            features["signature"] = "moved"
        elif self.change == "source":
            features["capture_metadata"] = dict(
                features["capture_metadata"], source_id="other_source"
            )
        elif self.change == "turn":
            features["changed_state"] = True
        elif self.change == "candidate":
            candidates = [replace(candidates[0], label="RIGHT")]
        elif self.change == "unsafe":
            candidates = [replace(candidates[0], dangerous=True)]
        return replace(observation, features=features), candidates


class PlannerProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = Adapter()
        self.planner = Planner(self.adapter)
        self.provider = PlannerDecisionProvider(
            self.planner, self.adapter, enabled=True, clock_ns=lambda: 1000
        )

    def pipeline(self, change=None, *, dry=False):
        source, backend = ChangingSource(change), Backend()
        pipeline = DecisionPipeline(
            source, Path(self.tmp.name), self.provider, dry_run=dry
        )
        pipeline.executor.live_executor = backend
        self.addCleanup(pipeline.close)
        return pipeline, source, backend

    def test_select_checks_fresh_frame_executes_one_and_replans_after_real_observation(
        self,
    ):
        pipeline, source, backend = self.pipeline()
        pipeline.run_and_execute()
        self.assertEqual(len(backend.actions), 1)
        self.assertEqual(source.fresh_reads, 1)
        self.assertEqual(self.provider._action_epoch, 1)
        self.assertEqual(self.provider._episode.usage, (0, 0))
        with self.assertRaises(DecisionWithheld):
            pipeline.run_and_execute()  # same cached pre-input frame cannot follow actual input
        self.assertEqual(len(backend.actions), 1)
        source.frame += 1  # explicit new post-action observation
        pipeline.run_and_execute()
        self.assertEqual(len(backend.actions), 2)
        self.assertEqual(self.planner.calls, 2)
        self.assertEqual(self.provider.last_planning_audit["actual_outcome"], False)

    def test_same_epoch_state_signature_candidate_or_source_changes_withhold_without_receipt(
        self,
    ):
        for change in ("state", "signature", "candidate", "source", "turn", "unsafe"):
            with self.subTest(change=change):
                pipeline, source, backend = self.pipeline(change)
                result = DecisionOrchestrator(pipeline, max_reobservations=0).run(
                    execute=True
                )
                self.assertIn(
                    result.outcome,
                    (PlanningOutcome.ABSTAIN, PlanningOutcome.OBSERVE_MORE),
                )
                self.assertEqual(backend.actions, [])
                self.assertEqual(pipeline.execution_history.load(), [])
                self.assertFalse(
                    any(
                        t.get("execution_receipt")
                        for t in pipeline.engine.trace.recent(10)
                    )
                )

    def test_dryrun_never_reads_extra_frame_or_acknowledges_real_action(self):
        pipeline, source, backend = self.pipeline(dry=True)
        result = pipeline.run_and_execute()
        self.assertEqual(result.mode, "dry_run")
        self.assertEqual(source.fresh_reads, 0)
        self.assertEqual(backend.actions, [])
        self.assertEqual(self.provider._action_epoch, 0)

    def test_stale_generation_and_expired_result_are_nonactions(self):
        for transform in (
            lambda r: replace(r, run_generation=r.run_generation + 1),
            lambda r: replace(r, created_at_ns=100, expires_at_ns=999),
        ):
            with self.subTest(transform=transform):
                self.planner.after = transform
                pipeline, _, backend = self.pipeline()
                result = DecisionOrchestrator(pipeline).run(execute=True)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(backend.actions, [])

    def test_predictive_terminal_cannot_be_used_as_actual_outcome(self):
        pipeline, source, backend = self.pipeline()
        self.assertFalse(hasattr(self.provider, "assess_outcome"))
        pipeline.run_and_execute()
        source.frame += 1
        observation, _ = source.read()
        actual = pipeline.complete_execution_observation(observation)
        self.assertIsNotNone(actual)
        self.assertEqual(actual.outcome.status, "unknown")
        self.assertEqual(len(backend.actions), 1)

    def test_final_dispatch_expiry_blocks_before_backend_and_records_no_actual_send(
        self,
    ):
        pipeline, source, backend = self.pipeline()
        original = self.provider.check_planned_dispatch

        def expire(generation):
            self.provider.clock_ns = lambda: 10**10
            original(generation)

        with patch.object(self.provider, "check_planned_dispatch", side_effect=expire):
            result = DecisionOrchestrator(pipeline).run(execute=True)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(backend.actions, [])
        receipts = [
            t["execution_receipt"]
            for t in pipeline.engine.trace.recent(10)
            if t.get("execution_receipt")
        ]
        self.assertEqual(receipts[0]["execution_status"], "not_executed")
        self.assertFalse(receipts[0]["input_dispatch_started"])

    def test_disabled_or_unavailable_uses_baseline_without_planner(self):
        for enabled, adapter in ((False, self.adapter), (True, None)):
            self.provider = PlannerDecisionProvider(
                self.planner, adapter, enabled=enabled
            )
            pipeline, source, backend = self.pipeline(dry=True)
            pipeline.run_and_execute()
            self.assertEqual(self.planner.calls, 0)
            self.assertEqual(source.fresh_reads, 0)

    def test_stop_rearm_during_fresh_capture_never_dispatches_old_selection(self):
        pipeline, source, backend = self.pipeline()
        capture = source.read_fresh

        def stop_rearm():
            result = capture()
            pipeline.controller.stop()
            pipeline.controller.start()
            return result

        source.read_fresh = stop_rearm
        with self.assertRaises(RuntimeError):
            pipeline.run_and_execute()
        self.assertEqual(backend.actions, [])
        self.assertEqual(pipeline.execution_history.load(), [])

    def test_trust_and_false_verification_flag_do_not_discharge_safety_requests(self):
        pipeline, _, backend = self.pipeline()
        evaluate = pipeline.safety_evaluator.evaluate

        def unresolved(*args):
            return replace(
                evaluate(*args),
                requires_verification=False,
                verification_requests=("reobserve_target",),
            )

        with patch.object(
            pipeline.safety_evaluator, "evaluate", side_effect=unresolved
        ):
            with self.assertRaisesRegex(RuntimeError, "verification"):
                pipeline.run_and_execute()
        self.assertEqual(backend.actions, [])

    def test_cached_frame_cannot_complete_actual_outcome_before_next_decision(self):
        pipeline, source, _ = self.pipeline()
        pipeline.run_and_execute()
        observation, _ = source.read()
        with self.assertRaises(DecisionWithheld):
            pipeline.complete_execution_observation(observation)
        self.assertTrue(pipeline.engine.has_pending_execution_observation)
        self.assertIsNone(pipeline.engine.last_execution_observation)
        source.frame += 1
        observation, _ = source.read()
        self.assertIsNotNone(pipeline.complete_execution_observation(observation))
        self.assertFalse(pipeline.engine.has_pending_execution_observation)

    def test_external_belief_writer_invalidates_plan_even_when_pixels_match(self):
        for boundary in ("result", "fresh", "dispatch"):
            for changed_version in (False, True):
                with self.subTest(boundary=boundary, changed_version=changed_version):
                    pipeline, source, backend = self.pipeline()

                    def update():
                        old = self.adapter.current
                        candidates = tuple(
                            replace(
                                c, weight=replace(c.weight, value=c.weight.value * 0.9)
                            )
                            for c in old.candidates
                        )
                        self.adapter.current = replace(
                            old,
                            candidates=candidates,
                            residual_unknown_mass=0.1,
                            hypothesis_set_version=old.hypothesis_set_version
                            + int(changed_version),
                        )

                    if boundary == "result":

                        def after(result):
                            update()
                            return result

                        self.planner.after = after
                    elif boundary == "fresh":
                        capture = source.read_fresh

                        def changed_capture():
                            result = capture()
                            update()
                            return result

                        source.read_fresh = changed_capture
                    else:
                        dispatch = self.provider.check_planned_dispatch

                        def changed_dispatch(generation):
                            update()
                            dispatch(generation)

                        self.provider.check_planned_dispatch = changed_dispatch
                    result = DecisionOrchestrator(pipeline).run(execute=True)
                    self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                    self.assertIn(
                        "planning_belief_or_registration_changed", result.reason_codes
                    )
                    self.assertEqual(backend.actions, [])
                    # Restore per-subtest adapters to avoid closure chains from earlier boundaries.
                    self.adapter = Adapter()
                    self.planner = Planner(self.adapter)
                    self.provider = PlannerDecisionProvider(
                        self.planner, self.adapter, enabled=True, clock_ns=lambda: 1000
                    )

    def test_registration_replacement_invalidates_selection(self):
        self.planner.registrations = {"fixture": "registered_v1"}

        def change_registration(result):
            self.planner.registrations["fixture"] = "registered_v2"
            return result

        self.planner.after = change_registration
        pipeline, _, backend = self.pipeline()
        result = DecisionOrchestrator(pipeline).run(execute=True)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(backend.actions, [])

    def test_stale_sensor_store_rejection_is_a_nonaction_not_baseline_fallback(self):
        pipeline, source, backend = self.pipeline()
        with patch.object(
            self.adapter, "prepare", side_effect=ValueError("snapshot_frame_mismatch")
        ):
            result = DecisionOrchestrator(pipeline).run(execute=True)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(result.reason_codes, ("planning_observation_rejected",))
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(backend.actions, [])

    def test_prediction_journal_event_is_separate_from_actual_execution(self):
        import json
        import sqlite3
        from hashlib import sha256
        from ai_game_player.event_journal import EventJournal
        from ai_game_player.planning_wire import result_from_dict

        source = ChangingSource()
        journal = EventJournal(Path(self.tmp.name) / "events.db", session_id="fixture")
        pipeline = DecisionPipeline(
            source,
            Path(self.tmp.name) / "journal-runtime",
            self.provider,
            event_journal=journal,
        )
        self.addCleanup(pipeline.close)
        pipeline.run_and_execute()  # dry-run still predicts, but provides no actual OS input
        with sqlite3.connect(journal.path) as connection:
            events = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT envelope_json FROM events ORDER BY sequence"
                )
            ]
        predictions = [
            event for event in events if event["event_type"] == "planning.prediction"
        ]
        self.assertEqual(len(predictions), 1)
        reference = pipeline.engine.trace.recent(1)[0]["planning_prediction"]
        self.assertNotIn("prediction", reference)
        self.assertEqual(reference["event_id"], predictions[0]["event_id"])
        self.assertEqual(reference["snapshot_id"], predictions[0]["snapshot_id"])
        self.assertLess(len(json.dumps(reference)), 2048)
        canonical = json.dumps(
            predictions[0]["payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(reference["content_sha256"], sha256(canonical).hexdigest())
        restored = result_from_dict(predictions[0]["payload"]["prediction"])
        self.assertEqual(restored.context.snapshot_id, reference["snapshot_id"])
        self.assertEqual(restored.selected_action_id, "move")
        self.assertFalse(predictions[0]["payload"]["actual_outcome"])
        self.assertEqual(
            predictions[0]["payload"]["prediction"]["selected_action_id"], "move"
        )
        self.assertFalse(
            any(event["event_type"] == "knowledge.action_outcome" for event in events)
        )

    def test_prediction_event_write_failure_cannot_dispatch_input(self):
        from ai_game_player.event_journal import EventJournal, EventJournalError

        source, backend = ChangingSource(), Backend()
        journal = EventJournal(
            Path(self.tmp.name) / "prediction-failure.db", session_id="fixture"
        )
        pipeline = DecisionPipeline(
            source,
            Path(self.tmp.name) / "failed-runtime",
            self.provider,
            event_journal=journal,
        )
        pipeline.executor.dry_run = False
        pipeline.executor.live_executor = backend
        self.addCleanup(pipeline.close)
        append = journal.append

        def fail_prediction(event_type, **kwargs):
            if event_type == "planning.prediction":
                raise EventJournalError("prediction persistence failed")
            return append(event_type, **kwargs)

        with patch.object(journal, "append", side_effect=fail_prediction):
            with self.assertRaisesRegex(
                EventJournalError, "prediction persistence failed"
            ):
                pipeline.run_and_execute()
        self.assertEqual(backend.actions, [])
        self.assertEqual(pipeline.execution_history.load(), [])
        self.assertFalse(pipeline.engine.has_pending_execution_observation)
        self.assertEqual(pipeline.engine.trace.recent(10), [])

    def test_reference_storage_preserves_fixed_input_decisions_receipts_and_event_counts(
        self,
    ):
        import json
        import sqlite3
        from ai_game_player.decision_context import DecisionTraceStore
        from ai_game_player.event_journal import EventJournal

        def run(name):
            adapter = Adapter()
            provider = PlannerDecisionProvider(
                Planner(adapter), adapter, enabled=True, clock_ns=lambda: 1000
            )
            journal = EventJournal(
                Path(self.tmp.name) / (name + ".db"), session_id=name
            )
            source, backend = ChangingSource(), Backend()
            pipeline = DecisionPipeline(
                source, Path(self.tmp.name) / name, provider, event_journal=journal
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = backend
            try:
                for _ in range(5):
                    pipeline.run_and_execute()
                    source.frame += 1
                    pipeline.complete_execution_observation(source.read()[0])
                entries = pipeline.engine.trace.recent(10)
                result = (
                    [item.action_id for item in backend.actions],
                    [
                        entry["execution_receipt"]["execution_status"]
                        for entry in entries
                        if entry.get("execution_receipt")
                    ],
                )
            finally:
                pipeline.close()
            with sqlite3.connect(journal.path) as connection:
                events = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT envelope_json FROM events ORDER BY sequence"
                    )
                ]
            return result, [event["event_type"] for event in events].count(
                "planning.prediction"
            )

        with patch.object(
            DecisionTraceStore,
            "_planning_trace_value",
            lambda self, context, audit: audit,
        ):
            before = run("full")
        after = run("reference")
        self.assertEqual(before, after)
        self.assertEqual(after, ((["move"] * 5, ["executed"] * 5), 5))
