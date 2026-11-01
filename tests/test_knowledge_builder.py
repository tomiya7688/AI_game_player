from dataclasses import FrozenInstanceError, replace
import itertools
import math
import random
import unittest

from ai_game_player.experience import (
    ArtifactReference,
    ExperienceEvent,
    ExperienceProvenance,
)
from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.knowledge_update_sample import KnowledgeUpdateSample


SCOPE = KnowledgeScope("test-game", "playing", "default", "player.one", "lineage.one")
TIME = "2026-11-01T12:00:00Z"


def effect_event(
    sequence,
    outcome="no_effect",
    action="score-label",
    *,
    source="session.one",
    scope=SCOPE,
    correlation="actions.one",
    generation=None,
    provenance="deterministic_action_effect",
    confidence=1.0,
    verified=True,
    execution_status="executed",
    hints=None,
):
    generation = sequence if generation is None else generation
    payload = {
        "scope": scope.to_dict(),
        "source_id": source,
        "source_sequence": sequence,
        "correlation_id": correlation,
        "correlation_sequence": generation,
        "action_generation": generation,
        "execution_status": execution_status,
        "action_id": action,
        "outcome": outcome,
        "evidence_kind": provenance,
        "verified": verified,
        "confidence": confidence,
    }
    payload.update(hints or {})
    return ExperienceEvent(
        f"event.action.{sequence}",
        "knowledge.action_outcome",
        TIME,
        "game_runtime",
        payload,
        provenance=ExperienceProvenance(
            provenance, confidence=confidence, evidence_event_id=f"effect.{sequence}"
        ),
    )


def build(events, previous=None, version="1", **kwargs):
    return KnowledgeBuilder().build(
        previous, events, scope=SCOPE, version=version, created_at=TIME, **kwargs
    )


class KnowledgeBuilderTest(unittest.TestCase):
    def test_sample_projection_is_immutable_and_policy_version_is_recorded(self):
        event = effect_event(1, hints={"roi_hints": {"target": [0, 0, 0.5, 0.5]}})
        sample = KnowledgeUpdateSample.from_event(event)
        event.payload["roi_hints"]["target"][2] = 1
        self.assertEqual(sample.roi_hints[0][1], (0, 0, 0.5, 0.5))
        with self.assertRaises(FrozenInstanceError):
            sample.outcome = "success"
        first = build([effect_event(1)]).snapshot.to_dict()
        self.assertEqual(
            first["update_rule_version"], "1." + KnowledgeBuilderPolicy().fingerprint
        )
        self.assertNotEqual(
            KnowledgeBuilderPolicy().fingerprint,
            KnowledgeBuilderPolicy(ewma_rate=0.2).fingerprint,
        )

    def test_no_effect_is_learned_without_declaring_action_unsafe(self):
        snapshot = build([effect_event(i) for i in range(1, 21)]).snapshot
        data = snapshot.to_dict()
        stats = data["action_outcome_stats"]["score-label"]
        self.assertEqual(stats["count"], 20)
        self.assertEqual(stats["no_effect"], 20)
        self.assertEqual(stats["failure"], 0)
        self.assertEqual(stats["reward_mean"], -0.25)
        self.assertLess(data["candidate_priors"]["score-label"], -0.5)
        self.assertNotIn("allowed_action_ids", data)
        self.assertNotIn("safety", data)
        self.assertEqual(snapshot.sample_count, 20)
        self.assertGreater(snapshot.uncertainty, 0)

    def test_repeated_success_and_failure_change_soft_priors_in_opposite_directions(
        self,
    ):
        events = [
            effect_event(i, "success", "move")
            if i % 2
            else effect_event(i, "failure", "bad-move")
            for i in range(1, 41)
        ]
        data = build(events).snapshot.to_dict()
        self.assertGreater(data["candidate_priors"]["move"], 0.5)
        self.assertLess(data["candidate_priors"]["bad-move"], -0.5)
        self.assertAlmostEqual(data["action_outcome_stats"]["move"]["alpha"], 21)
        self.assertAlmostEqual(data["action_outcome_stats"]["bad-move"]["beta"], 21)

    def test_pixel_change_passive_score_and_model_claims_remain_unknown(self):
        for provenance in ("pixel_change", "score_change", "model_prediction"):
            with self.subTest(source=provenance):
                data = build(
                    [effect_event(1, "success", provenance=provenance)]
                ).snapshot.to_dict()
                stats = data["action_outcome_stats"]["score-label"]
                self.assertEqual(stats["unknown"], 1)
                self.assertEqual(stats["alpha"], 1)
                self.assertEqual(stats["beta"], 1)
                self.assertEqual(stats["reward_mean"], 0)
                self.assertEqual(data["candidate_priors"], {})
                self.assertEqual(data["uncertainty"], 1)

    def test_unverified_low_confidence_and_missing_evidence_do_not_learn_effect(self):
        examples = [
            effect_event(1, "success", verified=False),
            effect_event(1, "success", confidence=0.3),
            replace(effect_event(1, "success"), provenance=None),
            replace(
                effect_event(1, "success"),
                provenance=ExperienceProvenance(
                    "deterministic_action_effect", confidence=1
                ),
            ),
        ]
        for event in examples:
            with self.subTest(event=event):
                data = build([event]).snapshot.to_dict()
                self.assertEqual(
                    data["action_outcome_stats"]["score-label"]["unknown"], 1
                )
                self.assertEqual(data["candidate_priors"], {})

    def test_unexecuted_actions_are_excluded_and_unknown_dispatch_does_not_gain_reward(
        self,
    ):
        for status in ("dry_run", "planned", "cancelled", "before_dispatch_failure"):
            with self.subTest(status=status):
                result = build([effect_event(1, "success", execution_status=status)])
                self.assertIsNone(result.snapshot)
                self.assertEqual(result.ignored_events, 1)
        data = build(
            [effect_event(1, "success", execution_status="dispatch_unknown")]
        ).snapshot.to_dict()
        self.assertEqual(data["action_outcome_stats"]["score-label"]["unknown"], 1)
        self.assertEqual(data["candidate_priors"], {})

    def test_same_generation_and_receipt_mismatch_is_rejected(self):
        event = effect_event(1)
        payload = dict(event.payload, action_generation=2)
        with self.assertRaisesRegex(ValueError, "generation"):
            build([replace(event, payload=payload)])

    def test_identical_experience_and_old_sequences_never_count_again_after_restore(
        self,
    ):
        original = build([effect_event(i) for i in range(1, 11)]).snapshot
        restored = KnowledgeSnapshot(original.to_bytes())
        result = build([effect_event(i) for i in range(1, 11)], restored, "2")
        self.assertIs(result.snapshot, restored)
        self.assertEqual(result.duplicate_events, 10)
        self.assertEqual(result.accepted_events, 0)

    def test_correlated_frames_and_detectors_are_one_sample_and_checkpoint_survives(
        self,
    ):
        first = build(
            [effect_event(1), effect_event(2, "success", generation=1)]
        ).snapshot
        self.assertEqual(first.sample_count, 1)
        restored = KnowledgeSnapshot(first.to_bytes())
        result = build(
            [effect_event(3, "success", source="detector.two", generation=1)],
            restored,
            "2",
        )
        self.assertEqual(result.snapshot.sample_count, 1)
        self.assertEqual(result.duplicate_events, 1)
        self.assertEqual(
            result.snapshot.to_dict()["builder_state"]["source_watermarks"][
                "detector.two"
            ],
            3,
        )
        resumed = build(
            [effect_event(4, "success", generation=2)], result.snapshot, "3"
        ).snapshot
        self.assertEqual(resumed.sample_count, 2)

    def test_unknown_effect_does_not_dilute_known_reward_mean(self):
        events = [
            effect_event(1, "success"),
            effect_event(2, "unknown"),
            effect_event(3, "unknown"),
        ]
        data = build(events).snapshot.to_dict()
        self.assertEqual(data["action_outcome_stats"]["score-label"]["reward_mean"], 1)
        self.assertGreaterEqual(data["uncertainty"], 2 / 3)

    def test_one_outlier_and_first_prior_have_bounded_influence(self):
        initial = build([effect_event(i, "success") for i in range(1, 101)]).snapshot
        updated = build([effect_event(101, "failure")], initial, "2").snapshot
        before = initial.to_dict()
        after = updated.to_dict()
        self.assertLessEqual(
            abs(
                after["candidate_priors"]["score-label"]
                - before["candidate_priors"]["score-label"]
            ),
            0.1,
        )
        self.assertLessEqual(
            abs(
                after["action_outcome_stats"]["score-label"]["reward_ewma"]
                - before["action_outcome_stats"]["score-label"]["reward_ewma"]
            ),
            0.2,
        )
        first = build([effect_event(1, "failure")]).snapshot.to_dict()
        self.assertLess(abs(first["candidate_priors"]["score-label"]), 0.05)
        self.assertEqual(
            initial.to_bytes(), KnowledgeSnapshot(initial.to_bytes()).to_bytes()
        )

    def test_transition_reliability_and_numeric_hints_update_with_finite_bounds(self):
        events = [
            effect_event(
                i,
                "success",
                hints={
                    "candidate_reliability": 0.9,
                    "from_state": "playing",
                    "to_state": "progress" if i % 2 else "playing",
                    "weight_hints": {"utility": 10},
                    "threshold_hints": {"decision_margin": 1},
                    "roi_hints": {"target": [0.6, 0.6, 0.2, 0.2]},
                },
            )
            for i in range(1, 5)
        ]
        data = build(events).snapshot.to_dict()
        self.assertAlmostEqual(
            sum(item["probability"] for item in data["transition_priors"]), 1
        )
        self.assertTrue(all(item["count"] == 2 for item in data["transition_priors"]))
        self.assertGreater(data["candidate_reliability"]["score-label"], 0.5)
        self.assertLessEqual(data["weight_hints"]["utility"], 1.4000000001)
        x, y, width, height = data["roi_hints"]["target"]
        self.assertLessEqual(x + width, 1)
        self.assertLessEqual(y + height, 1)

    def test_untrusted_input_cannot_create_transition_or_hint(self):
        event = effect_event(
            1,
            "success",
            provenance="pixel_change",
            hints={
                "from_state": "playing",
                "to_state": "win",
                "weight_hints": {"utility": 10},
            },
        )
        data = build([event]).snapshot.to_dict()
        self.assertEqual(data["transition_priors"], [])
        self.assertEqual(data["weight_hints"], {})

    def test_empty_irrelevant_and_sensitive_stream_preserve_static_or_existing_snapshot(
        self,
    ):
        self.assertIsNone(build([]).snapshot)
        irrelevant = replace(effect_event(1), event_type="screen.observed")
        self.assertEqual(build([irrelevant]).ignored_events, 1)
        secret = replace(
            effect_event(1),
            artifacts=(ArtifactReference("frame.raw", "a" * 64, "image/png", True),),
        )
        self.assertEqual(build([secret]).ignored_events, 1)
        existing = build([effect_event(1)]).snapshot
        self.assertIs(build([], existing, "2").snapshot, existing)

    def test_scope_version_and_retention_failures_never_modify_old_snapshot(self):
        existing = build([effect_event(1)], expires_at="2026-11-02T00:00:00Z").snapshot
        original = existing.to_bytes()
        with self.assertRaises(ValueError):
            build(
                [effect_event(2, scope=replace(SCOPE, game_id="another"))],
                existing,
                "2",
            )
        with self.assertRaises(ValueError):
            build([effect_event(2)], existing, "1")
        with self.assertRaises(ValueError):
            build([effect_event(2)], existing, "2", expires_at="2026-11-03T00:00:00Z")
        self.assertEqual(existing.to_bytes(), original)

    def test_batch_and_memory_capacities_fail_atomically_and_bound_iterator_consumption(
        self,
    ):
        policy = KnowledgeBuilderPolicy(
            max_batch_events=3,
            max_actions=1,
            max_sources=1,
            max_correlations=1,
            max_transitions=1,
        )
        builder = KnowledgeBuilder(policy)
        consumed = []

        def stream():
            for i in itertools.count(1):
                consumed.append(i)
                yield effect_event(i)

        with self.assertRaisesRegex(ValueError, "batch"):
            builder.build(None, stream(), scope=SCOPE, version="1", created_at=TIME)
        self.assertEqual(consumed, [1, 2, 3, 4])
        existing = builder.build(
            None, [effect_event(1)], scope=SCOPE, version="1", created_at=TIME
        ).snapshot
        for event in (
            effect_event(2, action="another"),
            effect_event(2, source="another"),
            effect_event(2, correlation="another"),
        ):
            before = existing.to_bytes()
            with self.assertRaisesRegex(ValueError, "capacity"):
                builder.build(
                    existing, [event], scope=SCOPE, version="2", created_at=TIME
                )
            self.assertEqual(existing.to_bytes(), before)

    def test_bad_payload_and_safety_override_hints_are_rejected_without_echoing_content(
        self,
    ):
        base = effect_event(1)
        for changes in (
            {"raw_frame": "private"},
            {"confidence": float("nan")},
            {"confidence": 10**400},
            {"source_sequence": True},
            {"outcome": []},
            {"weight_hints": {"disable_safety": 1}},
            {"roi_hints": {"target": [0.9, 0, 0.2, 1]}},
            {"action_id": "sk-private-credential"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as raised:
                build([replace(base, payload=dict(base.payload, **changes))])
            self.assertNotIn("private", str(raised.exception))

    def test_policy_rejects_unbounded_or_invalid_settings(self):
        for changes in (
            {"max_batch_events": 0},
            {"max_actions": True},
            {"max_sources": 1025},
            {"ewma_rate": 1.1},
            {"minimum_confidence": float("inf")},
            {"prior_evidence_scale": 0},
            {"prior_evidence_scale": 10**400},
            {"no_effect_reward": 1},
            {"trusted_evidence_sources": set()},
            {"allow_sensitive_sources": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                KnowledgeBuilderPolicy(**changes)

    def test_seeded_incremental_batches_match_whole_stream_and_provenance_has_no_raw_data(
        self,
    ):
        rng = random.Random(530)
        events = [
            effect_event(
                i,
                rng.choice(("success", "failure", "no_effect", "unknown")),
                action=f"action.{i % 3}",
            )
            for i in range(1, 61)
        ]
        full = build(events).snapshot.to_dict()
        previous = None
        for index in range(0, len(events), 10):
            previous = build(
                events[index : index + 10], previous, str(index + 1)
            ).snapshot
        chunked = previous.to_dict()
        for key in (
            "action_outcome_stats",
            "candidate_priors",
            "transition_priors",
            "source_experience_range",
            "builder_state",
            "uncertainty",
        ):
            self.assertEqual(full[key], chunked[key], key)
        ranges = chunked["source_experience_range"]
        self.assertEqual(
            sum(item["event_count"] for item in ranges), previous.sample_count
        )
        self.assertEqual(ranges[0]["first_sequence"], 1)
        self.assertEqual(ranges[0]["last_sequence"], 60)
        self.assertEqual(len(ranges[0]["sha256"]), 64)
        self.assertTrue(math.isfinite(chunked["uncertainty"]))


if __name__ == "__main__":
    unittest.main()
