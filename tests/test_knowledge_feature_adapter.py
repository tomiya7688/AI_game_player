from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.knowledge_feature_adapter import KnowledgeFeatureAdapter
from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider


NOW = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/knowledge/snapshot-v1.json"


def make_snapshot(
    priors=None, *, version="1", uncertainty=0.05, unknown=False, **overrides
):
    payload = json.loads(FIXTURE.read_text())
    payload["version"] = version
    payload["snapshot_id"] = f"knowledge.version-{version}"
    payload["uncertainty"] = uncertainty
    payload["candidate_priors"] = priors or {"score-label": -0.8, "move-control": 0.0}
    stats = {}
    for action_id, prior in payload["candidate_priors"].items():
        stats[action_id] = {
            "count": 32,
            "success": 0 if unknown or prior < 0 else 32,
            "no_effect": 32 if not unknown and prior < 0 else 0,
            "failure": 0,
            "unknown": 32 if unknown else 0,
            "reward_mean": 0 if unknown else (-1 if prior < 0 else 1),
            "reward_ewma": 0 if unknown else (-0.9 if prior < 0 else 0.9),
            "alpha": 1.0 if unknown or prior < 0 else 33.0,
            "beta": 33.0 if not unknown and prior < 0 else 1.0,
        }
    payload["action_outcome_stats"] = stats
    payload["sample_count"] = 32 * len(stats)
    payload["candidate_reliability"] = {action_id: 1.0 for action_id in stats}
    payload["transition_priors"] = []
    payload["weight_hints"] = {}
    payload["threshold_hints"] = {"recognition_minimum": 0.0, "decision_margin": 0.9}
    payload["source_experience_range"] = [
        {
            "source_id": "session.learning",
            "first_sequence": 1,
            "last_sequence": payload["sample_count"],
            "event_count": payload["sample_count"],
            "sha256": "2" * 64,
        }
    ]
    payload.update(overrides)
    return KnowledgeSnapshot.from_dict(payload)


def make_adapter(snapshot, *, scope=None, **kwargs):
    expected = scope or snapshot.scope
    return KnowledgeFeatureAdapter(
        lambda _scope: snapshot,
        lambda _observation: expected,
        clock=lambda: NOW,
        **kwargs,
    )


def make_context(candidates=None):
    observation = ScreenObservation(
        "menu", 100, 80, features={"signature": "same-screen"}
    )
    items = candidates or [
        ActionCandidate("score-label", "click", "Score", x=10, y=10, confidence=0.95),
        ActionCandidate("move-control", "click", "Move", x=40, y=40, confidence=0.95),
    ]
    return observation, DecisionContextBuilder().build(observation, items, items)


class KnowledgeFeatureAdapterTests(unittest.TestCase):
    def test_no_effect_prior_changes_real_pipeline_choice_and_trace_version(self):
        learned_before = make_snapshot(
            {"score-label": 0.8, "move-control": 0.0}, version="before"
        )
        learned_after = make_snapshot(
            {"score-label": -0.8, "move-control": 0.0}, version="after"
        )
        observation, context = make_context()

        class Source:
            def read(self):
                return observation, [
                    ActionCandidate(
                        c.action_id,
                        c.kind,
                        c.label,
                        x=10 + index * 20,
                        y=20,
                        confidence=0.95,
                    )
                    for index, c in enumerate(context.candidates)
                ]

        selections = []
        for snapshot in (learned_before, learned_after):
            with tempfile.TemporaryDirectory() as directory:
                pipeline = DecisionPipeline(
                    Source(),
                    Path(directory),
                    BaselineDecisionProvider(),
                    knowledge_adapter=make_adapter(snapshot),
                )
                try:
                    result = pipeline.run_and_execute()
                    self.assertFalse(result.executed)
                    selections.append(result.action_id)
                    records = pipeline.engine.trace.recent(1)
                    projection = records[0]["context"]["knowledge_projection"]
                    self.assertEqual(projection["snapshot_version"], snapshot.version)
                    self.assertEqual(
                        projection["provenance"]["update_rule"], "beta-ewma"
                    )
                    self.assertEqual(
                        set(records[0]["context"]["allowed_action_ids"]),
                        {"score-label", "move-control"},
                    )
                finally:
                    pipeline.close()
        self.assertEqual(selections, ["score-label", "move-control"])

    def test_canonical_no_effect_stream_changes_next_pipeline_decision_without_hard_filter(
        self,
    ):
        # Reuse the canonical event fixture rather than a private learning schema.
        from test_knowledge_builder import effect_event

        scope = make_snapshot().scope
        learned = (
            KnowledgeBuilder()
            .build(
                None,
                [effect_event(index, scope=scope) for index in range(1, 21)],
                scope=scope,
                version="learned.20",
                created_at="2026-11-01T12:00:00Z",
            )
            .snapshot
        )

        class Source:
            def read(self):
                return ScreenObservation(
                    "menu", 100, 80, features={"signature": "same-screen"}
                ), [
                    ActionCandidate(
                        "score-label", "click", "Score", x=10, y=10, confidence=0.99
                    ),
                    ActionCandidate(
                        "move-control", "click", "Move", x=40, y=40, confidence=0.8
                    ),
                ]

        adapter = KnowledgeFeatureAdapter(
            lambda _: learned,
            lambda _: scope,
            clock=lambda: datetime(2026, 11, 1, 12, 1, tzinfo=timezone.utc),
        )
        choices = []
        for selected_adapter in (None, adapter):
            with tempfile.TemporaryDirectory() as directory:
                pipeline = DecisionPipeline(
                    Source(),
                    Path(directory),
                    BaselineDecisionProvider(),
                    knowledge_adapter=selected_adapter,
                )
                try:
                    choices.append(pipeline.run_and_execute().action_id)
                    record = pipeline.engine.trace.recent(1)[0]
                    self.assertEqual(
                        set(record["context"]["allowed_action_ids"]),
                        {"score-label", "move-control"},
                    )
                finally:
                    pipeline.close()
        self.assertEqual(choices, ["score-label", "move-control"])
        self.assertEqual(
            learned.to_dict()["action_outcome_stats"]["score-label"]["no_effect"], 20
        )

    def test_snapshot_is_captured_once_and_fixed_until_next_step(self):
        first = make_snapshot(
            {"score-label": 0.8, "move-control": 0.0}, version="first"
        )
        second = make_snapshot(
            {"score-label": -0.8, "move-control": 0.0}, version="second"
        )
        holder = [first]
        calls = []
        observation, context = make_context()

        def capture(scope):
            calls.append(scope)
            return holder[0]

        adapter = KnowledgeFeatureAdapter(
            capture, lambda _: first.scope, clock=lambda: NOW
        )
        adapted = adapter.apply(context, observation)
        holder[0] = second
        baseline = BaselineDecisionProvider()
        decision = baseline.decide(adapted)
        self.assertEqual(decision.selected_action_id, "score-label")
        self.assertEqual(decision.knowledge_snapshot_version, "first")
        self.assertEqual(len(calls), 1)
        later = adapter.apply(replace(context, snapshot_id="next-step"), observation)
        self.assertEqual(baseline.decide(later).selected_action_id, "move-control")
        self.assertEqual(
            later.knowledge_projection.to_dict()["snapshot_version"], "second"
        )
        mutable = adapted.knowledge_projection.to_dict()
        mutable["priors"]["score-label"] = -1
        self.assertEqual(baseline.decide(adapted).selected_action_id, "score-label")
        with self.assertRaises(FrozenInstanceError):
            adapted.knowledge_projection.expose_to_models = True

    def test_expired_stale_future_scope_mismatch_and_missing_use_static_fallback(self):
        observation, context = make_context()
        cases = (
            (make_snapshot(expires_at="2026-11-01T00:00:30Z"), {}, "expired"),
            (make_snapshot(), {"max_age_seconds": 10}, "stale"),
            (make_snapshot(created_at="2026-11-01T00:02:00Z"), {}, "future_snapshot"),
        )
        baseline = BaselineDecisionProvider(
            min_margin=0,
            knowledge_snapshot={"version": "legacy", "priors": {"score-label": 1}},
        )
        for snapshot, kwargs, reason in cases:
            with self.subTest(reason=reason):
                adapted = make_adapter(snapshot, **kwargs).apply(context, observation)
                self.assertEqual(
                    adapted.knowledge_projection.to_dict()["reason"], reason
                )
                decision = baseline.decide(adapted)
                self.assertEqual(decision.selected_action_id, "move-control")
                self.assertIsNone(decision.knowledge_snapshot_version)
        snapshot = make_snapshot()
        for field in ("game_id", "scene_id", "profile_id", "player_id", "lineage_id"):
            scope = replace(snapshot.scope, **{field: "other"})
            adapted = make_adapter(snapshot, scope=scope).apply(context, observation)
            self.assertEqual(
                adapted.knowledge_projection.to_dict()["reason"], "scope_mismatch"
            )
        missing = KnowledgeFeatureAdapter(
            lambda _: None, lambda _: snapshot.scope, clock=lambda: NOW
        ).apply(context, observation)
        self.assertEqual(missing.knowledge_projection.to_dict()["reason"], "missing")

    def test_unknown_results_and_low_confidence_do_not_become_hard_filters(self):
        observation, context = make_context()
        baseline = BaselineDecisionProvider(min_margin=0)
        for snapshot in (make_snapshot(unknown=True), make_snapshot(uncertainty=1)):
            adapted = make_adapter(snapshot).apply(context, observation)
            self.assertIs(adapted.candidates, context.candidates)
            self.assertIs(adapted.allowed_action_ids, context.allowed_action_ids)
            self.assertIs(adapted.state, context.state)
            self.assertIs(adapted.previous_outcome, context.previous_outcome)
            self.assertEqual(adapted.knowledge_projection.to_dict()["confidence"], 0)
            self.assertEqual(
                adapted.knowledge_projection.to_dict()["priors"]["score-label"], 0
            )
            self.assertEqual(
                set(baseline.decide(adapted).score_by_action),
                set(context.allowed_action_ids),
            )
            self.assertEqual(baseline.min_recognition, 0.6)
            self.assertEqual(baseline.min_margin, 0)

    def test_projection_cannot_cross_steps_or_override_allowed_boundary(self):
        observation, context = make_context()
        adapted = make_adapter(make_snapshot()).apply(context, observation)
        stale_context = replace(adapted, snapshot_id="other-step")
        decision = BaselineDecisionProvider(min_margin=0).decide(stale_context)
        self.assertIsNone(decision.knowledge_snapshot_version)
        safe = ActionCandidate("move-control", "wait", "Wait", confidence=0.95)
        blocked = ActionCandidate(
            "score-label", "click", "Unsafe", x=10, y=10, confidence=1, dangerous=True
        )
        original = DecisionContextBuilder().build(observation, [safe, blocked], [safe])
        snapshot = make_snapshot({"score-label": 1.0, "move-control": -1.0})
        adapted = make_adapter(snapshot).apply(original, observation)
        self.assertNotIn(
            "score-label", adapted.knowledge_projection.to_dict()["priors"]
        )
        self.assertEqual(
            BaselineDecisionProvider(min_margin=0).decide(adapted).selected_action_id,
            "move-control",
        )
        self.assertFalse(adapted.candidates[1].allowed)

    def test_model_context_is_opt_in_while_local_trace_keeps_provenance(self):
        observation, context = make_context()
        snapshot = make_snapshot()
        private = make_adapter(snapshot).apply(context, observation)
        self.assertNotIn("knowledge_projection", private.to_dict())
        self.assertEqual(
            private.to_dict(include_knowledge=True)["knowledge_projection"][
                "snapshot_version"
            ],
            "1",
        )
        public = make_adapter(snapshot, expose_to_models=True).apply(
            context, observation
        )
        payload = public.to_dict()["knowledge_projection"]
        self.assertEqual(payload["scope"], snapshot.scope.to_dict())
        self.assertIn("roi_hints", payload)
        self.assertIn("threshold_hints", payload)
        self.assertEqual(payload["evidence_type"], "knowledge_soft_evidence")
        self.assertLessEqual(
            len(json.dumps(payload, separators=(",", ":")).encode()), 4096
        )
        self.assertNotIn("raw_frame", json.dumps(payload))

    def test_model_projection_reports_omissions_and_has_a_fixed_byte_budget(self):
        priors = {f"action-{index:03d}-" + "x" * 110: -0.4 for index in range(100)}
        snapshot = make_snapshot(priors)
        observation, context = make_context(
            [
                ActionCandidate(action_id, "wait", "Known action", confidence=0.9)
                for action_id in priors
            ]
        )
        adapted = make_adapter(snapshot, expose_to_models=True).apply(
            context, observation
        )
        full = adapted.to_dict(include_knowledge=True)["knowledge_projection"]
        compact = adapted.to_dict()["knowledge_projection"]
        self.assertEqual(full["truncated"]["candidate_priors"], 36)
        self.assertLessEqual(
            len(json.dumps(compact, separators=(",", ":")).encode()), 4096
        )
        self.assertGreater(compact["omitted"]["candidate_priors"], 36)
        self.assertEqual(len(adapted.allowed_action_ids), 100)

    def test_existing_context_provider_gets_optional_compressed_knowledge_in_pipeline(
        self,
    ):
        observation, _context = make_context()
        snapshot = make_snapshot()

        class Source:
            def read(self):
                return observation, [
                    ActionCandidate("score-label", "wait", "Score", confidence=0.95)
                ]

        class ContextModel:
            def __init__(self):
                self.requests = []

            def choose_context(self, context, personality=""):
                self.requests.append(context.to_dict())
                return RuleProvider().choose_context(context, personality)

        for exposed in (False, True):
            provider = ContextModel()
            with tempfile.TemporaryDirectory() as directory:
                pipeline = DecisionPipeline(
                    Source(),
                    Path(directory),
                    provider,
                    knowledge_adapter=make_adapter(snapshot, expose_to_models=exposed),
                )
                try:
                    pipeline.run_and_execute()
                    self.assertEqual(
                        "knowledge_projection" in provider.requests[0], exposed
                    )
                    trace = pipeline.engine.trace.recent(1)[0]["context"][
                        "knowledge_projection"
                    ]
                    self.assertEqual(trace["snapshot_version"], "1")
                    self.assertIn("source_experience_range", trace["provenance"])
                    if exposed:
                        self.assertIn(
                            "evidence_type",
                            provider.requests[0]["knowledge_projection"],
                        )
                        self.assertNotIn(
                            "source_experience_range",
                            provider.requests[0]["knowledge_projection"]["provenance"],
                        )
                finally:
                    pipeline.close()

    def test_source_failure_invalid_clock_and_corrupt_snapshot_fail_soft_without_leaks(
        self,
    ):
        snapshot = make_snapshot()
        observation, context = make_context()

        def broken(_scope):
            raise RuntimeError("secret-and-private-path")

        for source, reason in (
            (broken, "source_unavailable"),
            (lambda _: {}, "invalid_snapshot"),
        ):
            adapter = KnowledgeFeatureAdapter(
                source, lambda _: snapshot.scope, clock=lambda: NOW
            )
            payload = adapter.project(context, observation).to_dict()
            self.assertEqual(payload["reason"], reason)
            self.assertNotIn("secret-and-private-path", json.dumps(payload))
        adapter = KnowledgeFeatureAdapter(
            lambda _: snapshot,
            lambda _: snapshot.scope,
            clock=lambda: NOW.replace(tzinfo=None),
        )
        self.assertEqual(
            adapter.project(context, observation).to_dict()["reason"], "invalid_clock"
        )
        for age in (0, -1, True, float("nan")):
            with self.assertRaises(ValueError):
                make_adapter(snapshot, max_age_seconds=age)

    def test_weight_hints_are_step_local_and_do_not_change_thresholds(self):
        snapshot = make_snapshot(
            weight_hints={"utility": 2.0, "recognition": 0.0, "risk": 10.0}
        )
        observation, context = make_context()
        provider = BaselineDecisionProvider(min_margin=0)
        before = dict(provider.weights)
        adapted = make_adapter(snapshot).apply(context, observation)
        weights = provider._step_weights(adapted)
        self.assertGreater(weights["utility"], before["utility"])
        self.assertLess(weights["recognition"], before["recognition"])
        self.assertNotIn("risk", weights)
        provider.decide(adapted)
        self.assertEqual(provider.weights, before)
        self.assertEqual(provider.min_recognition, 0.6)


if __name__ == "__main__":
    unittest.main()
