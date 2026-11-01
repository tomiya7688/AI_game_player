import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.decision_context import (
    DecisionContextBuilder,
    DecisionTraceStore,
    EvaluationFusion,
    EvaluatorEvidence,
    RepetitionContextEvaluator,
)
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.execution_receipt import ExecutionReceipt
from ai_game_player.event_journal import EventJournal
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


def record_actual_execution(engine, observation, decision, generation=0):
    receipt = ExecutionReceipt(
        decision.snapshot_id,
        decision.action_id,
        generation,
        observation.screen_id,
        str(observation.features.get("signature", "")),
        "executed",
        True,
    )
    engine.record_execution_receipt(observation, receipt)


def context_receipt(context, action_id="start"):
    return ExecutionReceipt(
        context.snapshot_id,
        action_id,
        0,
        context.state["screen_id"],
        context.state["signature"],
        "executed",
        True,
    )


class FixedEvaluator:
    def __init__(self, name, score, confidence=1.0, reliability=1.0):
        self.name = name
        self.score = score
        self.confidence = confidence
        self.reliability = reliability

    def evaluate(self, observation, candidate, recent_history, previous_outcome):
        return EvaluatorEvidence(
            self.name,
            self.score,
            self.confidence,
            self.reliability,
            f"{self.name}-evidence",
        )


class DecisionContextTest(unittest.TestCase):
    def observation(self, signature="state-a"):
        return ScreenObservation(
            "menu",
            320,
            200,
            ["START", "OPTIONS"],
            {
                "signature": signature,
                "perceptual_hash": "abcd",
                "mean_brightness": 120,
                "detected_elements": [{"raw": "not-for-decision-model"}],
                "secret_raw_feature": {"large": [1, 2, 3]},
            },
        )

    def candidate(self, action_id="start", label="START", confidence=0.91):
        return ActionCandidate(action_id, "wait", label, confidence=confidence)

    def test_context_is_compact_and_keeps_recognition_separate_from_utility(self):
        candidate = self.candidate()
        builder = DecisionContextBuilder(
            evaluators=[FixedEvaluator("utility", 0.4, 0.8, 0.75)]
        )
        context = builder.build(
            self.observation(),
            [candidate],
            [candidate],
            previous_outcome=OutcomeAssessment("unknown", 0.2, "uncertain"),
            current_goal="clear game",
            short_term_goal="start game",
        )
        payload = context.to_dict()
        packaged = payload["candidates"][0]
        self.assertEqual(packaged["recognition_confidence"], 0.91)
        self.assertEqual(packaged["evaluation"]["score"], 0.4)
        self.assertEqual(packaged["evaluation"]["evidence"][0]["reliability"], 0.75)
        self.assertNotIn("secret_raw_feature", payload["state"])
        self.assertNotIn("detected_elements", payload["state"])
        self.assertNotIn("x", packaged)
        self.assertEqual(payload["goal"]["short_term_goal"], "start game")
        self.assertEqual(payload["schema"], "decision-context/v1")

    def test_evaluation_fusion_detects_conflict(self):
        candidate = self.candidate()
        builder = DecisionContextBuilder(
            evaluators=[
                FixedEvaluator("positive", 0.8),
                FixedEvaluator("negative", -0.7),
            ],
            fusion=EvaluationFusion(conflict_threshold=0.3),
        )
        context = builder.build(self.observation(), [candidate], [candidate])
        packaged = context.candidates[0]
        self.assertTrue(packaged.evaluator_conflict)
        self.assertIn("evaluator_conflict", packaged.uncertainty)
        self.assertIn("candidate_evaluator_conflict", context.uncertainty)

    def test_decision_context_records_batch_duplicate_rejection_reason(self):
        duplicate_candidates = [
            ActionCandidate("duplicate", "wait", "First"),
            ActionCandidate("duplicate", "wait", "Second"),
        ]

        context = DecisionContextBuilder().build(
            self.observation(),
            duplicate_candidates,
            [],
        )

        self.assertEqual(
            [candidate.allowed for candidate in context.candidates], [False, False]
        )
        self.assertEqual(
            [candidate.evaluations[0].evidence for candidate in context.candidates],
            ["duplicate_action_id", "duplicate_action_id"],
        )

    def test_candidate_knowledge_keeps_source_confidence_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.json"
            store = KnowledgeStore(path)
            entry = store.add("transition", "START button", "opens the game menu", 0.82)
            candidate = self.candidate()
            context = DecisionContextBuilder(
                store, evaluators=[FixedEvaluator("utility", 0.2)]
            ).build(self.observation(), [candidate], [candidate])
            evidence = context.candidates[0].knowledge[0]
            self.assertEqual(evidence.evidence_id, entry["id"])
            self.assertEqual(evidence.source, "knowledge_store")
            self.assertEqual(evidence.confidence, 0.82)
            self.assertEqual(evidence.provenance, str(path))

    def test_engine_reuses_previous_outcome_and_avoids_stalled_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory))
            candidates = [
                self.candidate("primary", "PRIMARY", 0.95),
                self.candidate("alternate", "ALTERNATE", 0.70),
            ]
            before = self.observation("same")
            first = engine.step(before, candidates)
            record_actual_execution(engine, before, first)
            after = self.observation("same")
            engine.complete_execution_observation(after, 0)
            second = engine.step(after, candidates)
            self.assertEqual(first.action_id, "primary")
            self.assertEqual(second.action_id, "alternate")
            trace = json.loads(
                (Path(directory) / "decision_trace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(trace), 2)
            self.assertEqual(trace[1]["previous_outcome"]["status"], "ongoing")
            self.assertFalse(trace[1]["previous_outcome"]["state_changed"])

    def test_blocked_decision_keeps_prior_action_outcome_in_trace_and_context(self):
        class SequentialProvider:
            def __init__(self):
                self.action_ids = iter(("advance", "blocked", "continue"))
                self.contexts = []

            def choose_context(self, context, personality=""):
                self.contexts.append(context)
                return ActionDecision(
                    next(self.action_ids),
                    "Choose a current allowed action",
                    "test-provider",
                    context.snapshot_id,
                    context.state["screen_id"],
                    context.state["signature"],
                )

        before = ScreenObservation(
            "menu",
            320,
            200,
            features={"signature": "state-a", "state": {"progress": 1}},
        )
        after = ScreenObservation(
            "menu",
            320,
            200,
            features={"signature": "state-b", "state": {"progress": 2}},
        )
        candidates = [
            self.candidate("advance", "ADVANCE"),
            self.candidate("blocked", "BLOCKED"),
            self.candidate("continue", "CONTINUE"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            provider = SequentialProvider()
            engine = GamePlayerEngine(Path(directory), provider=provider)
            first = engine.step(before, candidates)
            record_actual_execution(engine, before, first)
            engine.complete_execution_observation(after, 0)
            engine.step(after, candidates)
            engine.mark_last_decision_not_executed("verification required")

            trace_after_block = engine.trace.recent()
            history_after_block = engine.trace.recent_actions()
            engine.step(after, candidates)
            next_context = provider.contexts[-1]

        self.assertEqual(trace_after_block[0]["action_outcome"]["status"], "changed")
        self.assertEqual(trace_after_block[1]["execution_status"], "blocked")
        self.assertEqual(
            [entry["action_id"] for entry in history_after_block], ["advance"]
        )
        self.assertEqual(next_context.recent_history[0]["action_id"], "advance")
        self.assertEqual(next_context.recent_history[0]["outcome"], "changed")
        self.assertTrue(next_context.previous_outcome["state_changed"])

    def test_terminal_outcome_trace_preserves_simultaneous_state_change(self):
        for terminal_text, terminal_status in (
            ("VICTORY", "success"),
            ("GAME OVER", "failure"),
        ):
            with (
                self.subTest(terminal_status=terminal_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                before = ScreenObservation(
                    "menu",
                    320,
                    200,
                    ["PLAYING"],
                    {
                        "signature": "state-a",
                        "state": {"progress": 1},
                        "perceptual_hash": "0000",
                    },
                )
                after = ScreenObservation(
                    "menu",
                    320,
                    200,
                    [terminal_text],
                    {
                        "signature": "state-b",
                        "state": {"progress": 2},
                        "perceptual_hash": "ffff",
                    },
                )
                candidates = [self.candidate("advance", "ADVANCE")]

                engine = GamePlayerEngine(Path(directory))
                first = engine.step(before, candidates)
                record_actual_execution(engine, before, first)
                engine.complete_execution_observation(after, 0)
                engine.step(after, candidates)
                trace = engine.trace.recent()

                self.assertEqual(trace[0]["action_outcome"]["status"], terminal_status)
                self.assertTrue(trace[0]["action_outcome"]["state_changed"])

    def test_trace_binds_snapshot_decision_evaluations_and_knowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = KnowledgeStore(root / "knowledge.json")
            evidence = store.add("strategy", "START", "safe opening action", 0.9)
            engine = GamePlayerEngine(root)
            decision = engine.step(self.observation(), [self.candidate()])
            trace = json.loads(
                (root / "decision_trace.json").read_text(encoding="utf-8")
            )[0]
            self.assertEqual(trace["decision"]["action_id"], decision.action_id)
            self.assertEqual(trace["snapshot_id"], trace["context"]["snapshot_id"])
            self.assertEqual(trace["used_evidence_ids"], [evidence["id"]])
            candidate = trace["context"]["candidates"][0]
            self.assertTrue(candidate["evaluation"]["evidence"])
            self.assertEqual(candidate["knowledge"][0]["evidence_id"], evidence["id"])

    def test_rejected_provider_proposal_is_excluded_from_action_history(self):
        primary = self.candidate("start", "START")
        alternate = self.candidate("options", "OPTIONS")
        rejected_proposal = {
            "snapshot_id": "rejected-snapshot",
            "screen_id": "menu",
            "state_signature": "state-a",
            "action_id": "start",
            "decision": None,
            "reliability": {"status": "REJECT"},
        }
        completed_action = {
            "snapshot_id": "completed-snapshot",
            "screen_id": "menu",
            "state_signature": "state-a",
            "action_id": "options",
            "decision": {"action_id": "options"},
            "reliability": {"status": "TRUST"},
            "action_generation": 0,
            "execution_receipt": ExecutionReceipt(
                "completed-snapshot", "options", 0, "menu", "state-a", "executed", True
            ).to_dict(),
        }

        context = DecisionContextBuilder(
            evaluators=[RepetitionContextEvaluator()]
        ).build(
            self.observation(),
            [primary, alternate],
            [primary, alternate],
            recent_history=[rejected_proposal, completed_action],
        )

        self.assertEqual(
            [entry["action_id"] for entry in context.recent_history], ["options"]
        )
        self.assertEqual(context.candidates[0].utility_score, 0.0)
        self.assertLess(context.candidates[1].utility_score, 0.0)

    def test_recent_action_limit_is_applied_after_rejection_filtering(self):
        candidate = self.candidate()
        context = DecisionContextBuilder().build(
            self.observation(), [candidate], [candidate]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision_trace.json")
            store.append(
                context,
                ActionDecision("start", "accepted", "test-provider"),
                reliability={"status": "TRUST"},
            )
            self.assertTrue(store.record_execution_receipt(context_receipt(context)))
            for _ in range(5):
                store.append_rejection(
                    context,
                    {"action_id": "start", "status": "REJECT"},
                )

            recent_actions = store.recent_actions(5)

        self.assertEqual(len(recent_actions), 1)
        self.assertEqual(recent_actions[0]["action_id"], "start")

    def test_partial_or_misbound_receipt_cannot_authorize_outcome(self):
        candidate = self.candidate()
        context = DecisionContextBuilder().build(
            self.observation(), [candidate], [candidate]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decision_trace.json"
            store = DecisionTraceStore(path)
            store.append(context, ActionDecision("start", "planned", "test"))
            planned = store.recent()[0]
            valid = context_receipt(context).to_dict()
            for invalid in (
                {"execution_status": "executed", "eligible_for_learning": True},
                {**valid, "snapshot_id": "other-snapshot"},
                {**valid, "screen_id": "other-screen"},
                {**valid, "action_generation": True},
            ):
                with self.subTest(receipt=invalid):
                    path.write_text(
                        json.dumps([{**planned, "execution_receipt": invalid}]),
                        encoding="utf-8",
                    )
                    self.assertFalse(
                        store.record_action_outcome(
                            context.snapshot_id, "start", {"status": "success"}
                        )
                    )
                    self.assertEqual(store.recent_actions(), [])

    def test_blocked_decision_stays_in_audit_trace_but_not_action_history(self):
        candidate = self.candidate()
        context = DecisionContextBuilder().build(
            self.observation(), [candidate], [candidate]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision_trace.json")
            store.append(
                context,
                ActionDecision("start", "accepted", "test-provider"),
                reliability={"status": "TRUST"},
            )

            marked = store.mark_action_not_executed(
                context.snapshot_id, "start", "verification required"
            )
            audit_trace = store.recent()
            recent_actions = store.recent_actions()

        self.assertTrue(marked)
        self.assertEqual(audit_trace[0]["execution_status"], "blocked")
        self.assertEqual(
            audit_trace[0]["execution_block_reason"], "verification required"
        )
        self.assertEqual(recent_actions, [])

    def test_journal_trace_patches_keep_outcomes_and_blocked_status(self):
        candidate = self.candidate()
        context = DecisionContextBuilder().build(
            self.observation(), [candidate], [candidate]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = EventJournal(
                root / "session.sqlite3", session_id="decision-trace-patches"
            )
            adapter = LegacyEventAdapter(
                journal,
                root / "decision_trace.json",
                "decision.trace",
                migrate_legacy=False,
            )
            store = DecisionTraceStore(root / "decision_trace.json", adapter)
            store.append(
                context,
                ActionDecision("start", "accepted", "test-provider"),
                reliability={"status": "TRUST"},
            )

            self.assertTrue(store.record_execution_receipt(context_receipt(context)))
            self.assertTrue(
                store.record_action_outcome(
                    context.snapshot_id,
                    "start",
                    {"status": "success", "state_changed": True},
                )
            )
            self.assertFalse(
                store.mark_action_not_executed(
                    context.snapshot_id, "start", "must not erase actual dispatch"
                )
            )
            blocked_context = DecisionContextBuilder().build(
                self.observation(), [candidate], [candidate]
            )
            store.append(
                blocked_context,
                ActionDecision("start", "planned only", "test-provider"),
            )
            self.assertTrue(
                store.mark_action_not_executed(
                    blocked_context.snapshot_id,
                    "start",
                    "live verification required",
                )
            )

            recent_trace = store.recent()
            recent_actions = store.recent_actions()
            journal.close()

        self.assertEqual(len(recent_trace), 2)
        self.assertEqual(recent_trace[0]["action_outcome"]["status"], "success")
        self.assertEqual(recent_trace[1]["execution_status"], "blocked")
        self.assertEqual(
            recent_trace[1]["execution_block_reason"], "live verification required"
        )
        self.assertEqual([row["action_id"] for row in recent_actions], ["start"])

    def test_fusion_uses_confidence_and_reliability_independently(self):
        score, confidence, conflict = EvaluationFusion().fuse(
            [
                EvaluatorEvidence("trusted", 1.0, 1.0, 1.0, "a"),
                EvaluatorEvidence("weak", -1.0, 0.2, 0.5, "b"),
            ]
        )
        self.assertGreater(score, 0.7)
        self.assertGreater(confidence, 0.7)
        self.assertTrue(conflict)


if __name__ == "__main__":
    unittest.main()
