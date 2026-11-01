from dataclasses import dataclass, FrozenInstanceError, replace
import gc
import json
from pathlib import Path
import threading
import tracemalloc
import unittest

from ai_game_player.belief_candidate_store import (
    BeliefCandidateStore,
    BeliefStoreError,
    BeliefStorePolicy,
)
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityStatus
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)


@dataclass(frozen=True)
class CardState:
    hand: tuple[str, ...]
    player: int = 0


@dataclass(frozen=True)
class PositionState:
    position: tuple[float, float]


class BeliefCandidateStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000
        self.clock = lambda: self.now
        self.context = BeliefContext(
            "cards",
            "table",
            "session1",
            "snapshot1",
            ("frame1",),
            1,
            1,
            1,
            "capture",
            1,
            "clock1",
        )

    def store(self, **policy):
        return BeliefCandidateStore(
            CardState,
            clock_id="clock1",
            clock_ns=self.clock,
            policy=BeliefStorePolicy(**policy),
        )

    def hypothesis(
        self,
        name="h1",
        *,
        state=None,
        value=0.5,
        context=None,
        kind=BeliefWeightKind.CALIBRATED_PROBABILITY,
        evidence=None,
        origin=HypothesisOrigin.OBSERVATION_HYPOTHESIS,
        freshness=None,
    ):
        context = context or self.context
        state = state or CardState((name,))
        ref = TypedStateRef(context.game_id, "state-v1", type(state), state)
        observed = self.now - 10 if freshness is None else freshness.observed_at_ns
        if evidence is None:
            prediction = origin == HypothesisOrigin.PREDICTION
            evidence = (
                BeliefEvidenceRef(
                    "e1",
                    "same-frame",
                    BeliefEvidenceKind.PREDICTION
                    if prediction
                    else BeliefEvidenceKind.OBSERVATION,
                    "recognizer",
                    "v1",
                    context,
                    observed,
                    None if prediction else context.frame_ids[-1],
                ),
            )
        return WorldStateHypothesis(
            name,
            ref,
            context,
            BeliefWeight(
                kind,
                value,
                "calibrated-v1"
                if kind == BeliefWeightKind.CALIBRATED_PROBABILITY
                else None,
            ),
            origin,
            evidence,
            freshness or BeliefFreshness(observed, observed + 1_000_000_000),
        )

    def test_2048_fixture_adapter_uses_existing_typed_state_and_preserves_8_16(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/belief/ambiguous_2048.json").read_text()
        )
        context = replace(self.context, game_id=fixture["game_id"])
        # Optional game adapter translates synthetic recognition alternatives; Core has no board shape.
        candidates = tuple(
            self.hypothesis(
                item["id"],
                context=context,
                state=Game2048State(item["cells"]),
                value=item["probability"],
            )
            for item in fixture["candidates"]
        )
        store = BeliefCandidateStore(
            Game2048State, clock_id="clock1", clock_ns=self.clock
        )
        snapshot = store.publish(
            context, candidates, unknown_mass=fixture["unknown_mass"]
        )
        self.assertEqual(
            [item.state_ref.state.cells[0][2] for item in snapshot.candidates], [8, 16]
        )
        self.assertAlmostEqual(snapshot.residual_unknown_mass, 0.1)
        self.assertEqual(snapshot.weight_kind, BeliefWeightKind.CALIBRATED_PROBABILITY)
        self.assertEqual(snapshot.schema_version, 1)
        self.assertFalse(snapshot.reobserve_requested)
        self.assertGreater(snapshot.entropy_lower_bound_bits, 1.0)
        self.assertEqual(
            json.loads(snapshot.to_bytes())["candidates"][0]["origin"],
            "observation_hypothesis",
        )

    def test_unrelated_card_and_position_states_use_same_generic_contract(self):
        card_store = self.store()
        card = card_store.publish(self.context, [self.hypothesis()])
        position_context = replace(self.context, game_id="fps")
        position = self.hypothesis(
            context=position_context, state=PositionState((1.5, 2.0))
        )
        fps_store = BeliefCandidateStore(
            PositionState, clock_id="clock1", clock_ns=self.clock
        )
        self.assertEqual(
            fps_store.publish(position_context, [position])
            .candidates[0]
            .state_ref.state.position,
            (1.5, 2.0),
        )
        self.assertIsInstance(card.candidates[0].state_ref.state, CardState)
        with self.assertRaises(BeliefStoreError):
            card_store.publish(position_context, [position])

    def test_belief_payload_never_overrides_actual_allowed_actions_or_verifier(self):
        belief = self.store().publish(self.context, [self.hypothesis(value=1.0)])
        observation = ScreenObservation(
            "cards",
            100,
            80,
            features={"signature": "cards-v1", "belief_snapshot": belief.to_dict()},
        )
        safe = ActionCandidate("wait", "wait", "Wait", confidence=0.9)
        unsafe = ActionCandidate(
            "unsafe", "click", "Unsafe", x=10, y=10, confidence=1.0, dangerous=True
        )
        decision_context = DecisionContextBuilder().build(
            observation, [safe, unsafe], [safe]
        )
        self.assertEqual(decision_context.allowed_action_ids, ("wait",))
        verifier = DecisionVerifier()
        for action_id, expected in (
            ("wait", ReliabilityStatus.TRUST),
            ("unsafe", ReliabilityStatus.REJECT),
        ):
            decision = ActionDecision(
                action_id,
                "Choose the allowed action",
                "fixture",
                decision_context.snapshot_id,
                "cards",
                "cards-v1",
            )
            self.assertEqual(
                verifier.verify(decision, decision_context, observation).status,
                expected,
            )

    def test_snapshot_direct_construction_cannot_break_version_mass_or_freshness(self):
        snapshot = self.store().publish(self.context, [self.hypothesis()])
        for changes in (
            {"schema_version": True},
            {"schema_version": 2},
            {"residual_unknown_mass": 0.0},
            {"expires_at_ns": snapshot.expires_at_ns + 1},
            {"candidates": list(snapshot.candidates)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(snapshot, **changes)
        with self.assertRaises(FrozenInstanceError):
            snapshot.candidates = ()

    def test_versioned_schema_declares_all_snapshot_fields_and_weight_semantics(self):
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "config/belief_snapshot.schema.json"
            ).read_text()
        )
        snapshot = self.store().publish(self.context, [self.hypothesis()])
        self.assertEqual(set(schema["required"]), set(snapshot.to_dict()))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            schema["properties"]["weight_kind"]["enum"], list(BeliefWeightKind)
        )
        self.assertEqual(
            schema["$defs"]["hypothesis"]["properties"]["origin"]["enum"],
            list(HypothesisOrigin),
        )

    def test_same_context_prediction_can_be_replaced_without_inventing_a_new_frame(
        self,
    ):
        store = self.store()
        prior = store.publish(
            self.context, [self.hypothesis(origin=HypothesisOrigin.PREDICTION)]
        )
        posterior = self.hypothesis(
            origin=HypothesisOrigin.POSTERIOR_HYPOTHESIS,
            kind=BeliefWeightKind.RELATIVE_SCORE,
        )
        observed = store.publish(self.context, [posterior])
        self.assertEqual(observed.context, prior.context)
        self.assertEqual(
            observed.candidates[0].origin, HypothesisOrigin.POSTERIOR_HYPOTHESIS
        )
        self.assertEqual(observed.residual_unknown_mass, 1.0)

    def test_monotonic_clock_regression_is_rejected_without_replacing_snapshot(self):
        store = self.store()
        snapshot = store.publish(self.context, [self.hypothesis()])
        self.now -= 1
        with self.assertRaisesRegex(BeliefStoreError, "clock_regressed"):
            store.publish(self.context, [self.hypothesis()])
        self.now += 1
        self.assertIs(store.read(self.context), snapshot)

    def test_typed_state_is_deeply_immutable_and_canonical(self):
        state = CardState(("A", "K"))
        reference = TypedStateRef("cards", "v1", CardState, state)
        self.assertEqual(
            reference.digest,
            TypedStateRef("cards", "v1", CardState, CardState(("A", "K"))).digest,
        )
        self.assertNotEqual(
            reference.digest, TypedStateRef("cards", "v2", CardState, state).digest
        )
        with self.assertRaises(FrozenInstanceError):
            reference.state = CardState(("Q",))
        with self.assertRaises(FrozenInstanceError):
            state.player = 1
        for bad in (
            CardState(["A"]),
            [1],
            {"state": 1},
            (float("nan"),),
            (float("inf"),),
        ):
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                TypedStateRef("cards", "v1", type(bad), bad)
        with self.assertRaises(ValueError):
            TypedStateRef("cards", "v1", CardState, PositionState((1.0, 2.0)))

    def test_undecorated_mutable_subclass_and_undeclared_state_fields_are_rejected(
        self,
    ):
        class MutableCard(CardState):
            pass

        state = MutableCard(("A",))
        state.mutable_extra = []
        with self.assertRaises(ValueError):
            TypedStateRef("cards", "v1", MutableCard, state)
        extra = CardState(("K",))
        object.__setattr__(extra, "hidden_payload", [])
        with self.assertRaises(ValueError):
            TypedStateRef("cards", "v1", CardState, extra)

    def test_expected_prior_version_prevents_late_same_context_overwrite(self):
        store = self.store()
        first = store.publish(self.context, [self.hypothesis()], expected_version=0)
        second = store.publish(
            self.context,
            [self.hypothesis(value=0.4)],
            expected_version=first.hypothesis_set_version,
        )
        with self.assertRaisesRegex(BeliefStoreError, "stale_expected_version"):
            store.publish(
                self.context,
                [self.hypothesis(value=0.9)],
                expected_version=first.hypothesis_set_version,
            )
        self.assertIs(store.read(self.context), second)
        with self.assertRaises(ValueError):
            store.publish(self.context, [], expected_version=True)

    def test_cycles_depth_nodes_scalar_bytes_and_nonfinite_weights_are_rejected(self):
        cycle = CardState(())
        object.__setattr__(cycle, "hand", (cycle,))
        deep = ()
        for _ in range(18):
            deep = (deep,)
        for state in (
            cycle,
            deep,
            tuple(range(2049)),
            "x" * 65537,
            b"x" * 65537,
            2**300,
        ):
            with self.subTest(type=type(state)), self.assertRaises(ValueError):
                TypedStateRef("cards", "v1", type(state), state)
        for value in (float("nan"), float("inf"), -0.1, True, 10**500):
            with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                BeliefWeight(BeliefWeightKind.RELATIVE_SCORE, value)
        for weight in (
            (BeliefWeightKind.CALIBRATED_PROBABILITY, 1.1, "v1"),
            (BeliefWeightKind.CALIBRATED_PROBABILITY, 0.5, None),
            (BeliefWeightKind.RELATIVE_SCORE, 0.5, "v1"),
            (BeliefWeightKind.UNKNOWN, 0, None),
        ):
            with self.assertRaises(ValueError):
                BeliefWeight(*weight)

    def test_top_k_keeps_absolute_probability_and_moves_dropped_mass_to_unknown(self):
        store = self.store(max_candidates=1)
        snapshot = store.publish(
            self.context,
            [self.hypothesis("a", value=0.6), self.hypothesis("b", value=0.3)],
            unknown_mass=0.1,
        )
        self.assertEqual(snapshot.candidates[0].weight.value, 0.6)
        self.assertAlmostEqual(snapshot.residual_unknown_mass, 0.4)
        self.assertEqual(snapshot.ranking_weights, (1.0,))
        self.assertAlmostEqual(snapshot.residual_unknown_mass, 0.4)
        self.assertEqual(snapshot.diagnostics.dropped_candidates, 1)

    def test_score_normalization_is_not_probability_and_large_scores_do_not_overflow(
        self,
    ):
        store = self.store(max_candidates=1)
        first = self.hypothesis("a", kind=BeliefWeightKind.RELATIVE_SCORE, value=1e308)
        second = self.hypothesis("b", kind=BeliefWeightKind.RELATIVE_SCORE, value=1e308)
        snapshot = store.publish(self.context, [first, second])
        self.assertEqual(snapshot.weight_kind, BeliefWeightKind.RELATIVE_SCORE)
        self.assertEqual(snapshot.residual_unknown_mass, 1.0)
        self.assertTrue(snapshot.reobserve_requested)
        self.assertIsNone(snapshot.entropy_lower_bound_bits)
        self.assertEqual(snapshot.ranking_weights, (1.0,))
        snapshot = self.store().publish(self.context, [first, second])
        self.assertEqual(snapshot.ranking_weights, (0.5, 0.5))

    def test_exact_id_resends_do_not_add_mass_but_conflicting_id_is_rejected(self):
        store = self.store()
        hypothesis = self.hypothesis(value=0.7)
        snapshot = store.publish(self.context, [hypothesis, hypothesis])
        self.assertEqual(len(snapshot.candidates), 1)
        self.assertAlmostEqual(snapshot.residual_unknown_mass, 0.3)
        self.assertEqual(snapshot.diagnostics.duplicate_ids, 1)
        with self.assertRaisesRegex(
            BeliefStoreError, "conflicting_hypothesis_identity"
        ):
            store.publish(
                self.context,
                [
                    hypothesis,
                    replace(
                        hypothesis,
                        weight=BeliefWeight(
                            BeliefWeightKind.CALIBRATED_PROBABILITY,
                            0.2,
                            "calibrated-v1",
                        ),
                    ),
                ],
            )
        self.assertIs(store.read(self.context), snapshot)

    def test_same_world_aliases_keep_max_and_unmerged_path_mass_remains_unknown(self):
        a = self.hypothesis("a", state=CardState(("A",)), value=0.3)
        b = replace(
            a,
            hypothesis_id="b",
            weight=BeliefWeight(
                BeliefWeightKind.CALIBRATED_PROBABILITY, 0.4, "calibrated-v1"
            ),
        )
        snapshot = self.store().publish(self.context, [a, b], unknown_mass=0.3)
        self.assertEqual(snapshot.candidates[0].hypothesis_id, "b")
        self.assertEqual(snapshot.candidates[0].weight.value, 0.4)
        self.assertAlmostEqual(snapshot.residual_unknown_mass, 0.6)
        self.assertEqual(snapshot.diagnostics.duplicate_worlds, 1)
        # #564 may sum certified mutually exclusive paths first, then publish one hypothesis.
        merged = replace(
            b,
            weight=BeliefWeight(
                BeliefWeightKind.CALIBRATED_PROBABILITY, 0.7, "calibrated-v1"
            ),
        )
        self.assertAlmostEqual(
            self.store().publish(self.context, [merged]).residual_unknown_mass, 0.3
        )

    def test_correlated_recognizer_refs_are_counted_once_and_never_multiplied(self):
        base = self.hypothesis(value=0.6)
        first = base.evidence_refs[0]
        second = replace(first, evidence_id="e2", source_id="ocr", source_version="v2")
        combined = replace(base, evidence_refs=(first, second, first))
        snapshot = self.store().publish(self.context, [combined])
        self.assertEqual(snapshot.diagnostics.evidence_refs, 2)
        self.assertEqual(snapshot.diagnostics.correlation_groups, 1)
        self.assertEqual(snapshot.candidates[0].weight.value, 0.6)
        other = self.hypothesis(
            "other",
            value=0.3,
            evidence=(replace(first, source_version="contradiction"),),
        )
        with self.assertRaisesRegex(BeliefStoreError, "conflicting_evidence_identity"):
            self.store().publish(self.context, [base, other])
        with self.assertRaises(ValueError):
            replace(
                base,
                evidence_refs=(first, replace(first, source_version="contradiction")),
            )

    def test_mixed_scope_frame_turn_epoch_generation_and_snapshot_are_rejected(self):
        base = self.hypothesis()
        for changes in (
            {"scene_id": "other"},
            {"session_id": "other"},
            {"turn": 2},
            {"action_epoch": 2},
            {"source_generation": 2},
            {"snapshot_id": "other"},
            {"frame_ids": ("other",)},
            {"clock_id": "other"},
        ):
            context = replace(self.context, **changes)
            with self.subTest(changes=changes), self.assertRaises(BeliefStoreError):
                self.store().publish(self.context, [self.hypothesis(context=context)])
            with self.assertRaises(ValueError):
                replace(
                    base,
                    evidence_refs=(
                        replace(
                            base.evidence_refs[0],
                            context=context,
                            frame_id=context.frame_ids[-1],
                        ),
                    ),
                )
        for changes in (
            {"frame_ids": ["frame1"]},
            {"frame_ids": ("frame1", "frame1")},
            {"turn": True},
            {"scene_id": "../bad"},
        ):
            with self.assertRaises(ValueError):
                replace(self.context, **changes)

    def test_probability_mass_and_calibration_or_origin_mixtures_are_rejected(self):
        base = self.hypothesis(value=0.7)
        for other in (
            self.hypothesis("b", value=0.7),
            self.hypothesis("b", kind=BeliefWeightKind.RELATIVE_SCORE),
            replace(
                self.hypothesis("b"),
                weight=BeliefWeight(
                    BeliefWeightKind.CALIBRATED_PROBABILITY, 0.1, "other-calibration"
                ),
            ),
            self.hypothesis("b", origin=HypothesisOrigin.PREDICTION),
        ):
            with self.assertRaises(BeliefStoreError):
                self.store().publish(self.context, [base, other])
        with self.assertRaises(BeliefStoreError):
            self.store().publish(self.context, [base], unknown_mass=0.4)

    def test_prediction_is_never_relabelled_as_observed_fact(self):
        prediction = self.hypothesis(origin=HypothesisOrigin.PREDICTION, value=1.0)
        result = self.store().publish(self.context, [prediction])
        self.assertTrue(result.reobserve_requested)
        self.assertEqual(result.candidates[0].origin, HypothesisOrigin.PREDICTION)
        self.assertEqual(
            result.to_dict()["candidates"][0]["evidence_refs"][0]["kind"], "prediction"
        )
        with self.assertRaises(ValueError):
            replace(prediction, origin=HypothesisOrigin.OBSERVATION_HYPOTHESIS)
        with self.assertRaises(ValueError):
            replace(prediction.evidence_refs[0], frame_id="frame1")
        with self.assertRaises(ValueError):
            replace(self.hypothesis(), origin=HypothesisOrigin.PREDICTION)

    def test_empty_all_zero_all_stale_and_low_coverage_request_reobservation(self):
        for candidates in (
            [],
            [self.hypothesis(value=0)],
            [self.hypothesis(freshness=BeliefFreshness(0, 1))],
        ):
            snapshot = self.store().publish(self.context, candidates)
            self.assertEqual(snapshot.candidates, ())
            self.assertEqual(snapshot.weight_kind, BeliefWeightKind.UNKNOWN)
            self.assertEqual(snapshot.residual_unknown_mass, 1.0)
            self.assertTrue(snapshot.reobserve_requested)
        self.assertTrue(
            self.store()
            .publish(self.context, [self.hypothesis(value=0.2)])
            .reobserve_requested
        )

    def test_old_evidence_cannot_be_refreshed_by_new_ttl_or_distant_fused_frame(self):
        base = self.hypothesis()
        old = replace(base.evidence_refs[0], captured_at_ns=0)
        snapshot = self.store().publish(
            self.context, [replace(base, evidence_refs=(old,))]
        )
        self.assertEqual(snapshot.candidates, ())
        earlier = replace(
            base.evidence_refs[0],
            evidence_id="old-frame",
            captured_at_ns=self.now - 300_000_000,
        )
        span = replace(base, evidence_refs=(base.evidence_refs[0], earlier))
        self.assertEqual(self.store().publish(self.context, [span]).candidates, ())
        future = replace(base, freshness=BeliefFreshness(self.now + 1, self.now + 10))
        with self.assertRaisesRegex(BeliefStoreError, "future_evidence_timestamp"):
            self.store().publish(self.context, [future])

    def test_read_rejects_expired_context_and_cross_process_clock_origin(self):
        store = self.store()
        snapshot = store.publish(self.context, [self.hypothesis()])
        self.assertIs(store.read(self.context), snapshot)
        self.assertIsNone(store.read(replace(self.context, snapshot_id="other")))
        self.now = snapshot.expires_at_ns
        self.assertIsNone(store.read(self.context))
        for method in (store.reset, lambda context: store.publish(context, [])):
            with self.assertRaisesRegex(BeliefStoreError, "clock_origin_mismatch"):
                method(replace(self.context, clock_id="new-process-clock"))

    def test_new_frame_replaces_distribution_and_restart_requires_explicit_reset(self):
        store = self.store()
        first = store.publish(self.context, [self.hypothesis()])
        next_context = replace(
            self.context,
            snapshot_id="snapshot2",
            frame_ids=("frame2",),
            frame_sequence=2,
            turn=2,
            action_epoch=2,
        )
        second = store.publish(
            next_context, [self.hypothesis("next", context=next_context, value=0.4)]
        )
        self.assertEqual(len(second.candidates), 1)
        self.assertEqual(second.candidates[0].hypothesis_id, "next")
        self.assertEqual(
            second.hypothesis_set_version, first.hypothesis_set_version + 1
        )
        self.assertAlmostEqual(second.residual_unknown_mass, 0.6)
        with self.assertRaisesRegex(BeliefStoreError, "stale_context"):
            store.publish(self.context, [self.hypothesis()])
        restart = replace(self.context, source_generation=2)
        with self.assertRaisesRegex(BeliefStoreError, "scope_reset_required"):
            store.publish(restart, [])
        self.assertEqual(store.reset(restart).residual_unknown_mass, 1.0)
        self.assertEqual(
            len(store.publish(restart, [self.hypothesis(context=restart)]).candidates),
            1,
        )
        self.assertIsNone(store.read(next_context))

    def test_same_frame_with_changed_snapshot_and_reused_snapshot_are_rejected(self):
        store = self.store()
        store.publish(self.context, [self.hypothesis()])
        for context in (
            replace(self.context, snapshot_id="other"),
            replace(self.context, frame_sequence=2),
        ):
            with self.assertRaises(BeliefStoreError):
                store.publish(context, [self.hypothesis(context=context)])

    def test_byte_budget_prunes_without_renormalizing_and_single_state_is_bounded(self):
        store = self.store(max_snapshot_bytes=4096)
        candidates = [
            self.hypothesis(
                str(index), state=CardState(("x" * 600 + str(index),)), value=0.25
            )
            for index in range(4)
        ]
        snapshot = store.publish(self.context, candidates)
        self.assertLess(len(snapshot.candidates), 4)
        self.assertLessEqual(len(snapshot.to_bytes()), 4096)
        self.assertAlmostEqual(
            snapshot.residual_unknown_mass, 1 - len(snapshot.candidates) * 0.25
        )
        with self.assertRaisesRegex(BeliefStoreError, "candidate_state_byte_limit"):
            self.store(max_state_bytes=10).publish(self.context, [self.hypothesis()])

    def test_input_overflow_and_timeout_leave_previous_snapshot_unchanged(self):
        store = self.store(max_candidates=1, max_input_candidates=3)
        old = store.publish(self.context, [self.hypothesis()])
        yielded = []

        def endless():
            while True:
                yielded.append(1)
                yield self.hypothesis()

        with self.assertRaisesRegex(BeliefStoreError, "input_candidate_limit"):
            store.publish(self.context, endless())
        self.assertEqual(len(yielded), 4)
        self.assertIs(store.read(self.context), old)
        with self.assertRaisesRegex(BeliefStoreError, "update_timeout"):
            store.publish(self.context, [], deadline_ns=self.now)
        self.assertIs(store.read(self.context), old)

    def test_reset_racing_slow_generation_cannot_publish_old_scope(self):
        store = self.store()
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow_candidates():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test deadline")
            yield self.hypothesis()

        def publish():
            try:
                store.publish(self.context, slow_candidates())
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=publish)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reset = store.reset(replace(self.context, scene_id="new-scene"))
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(str(errors[0]), "concurrent_snapshot_update")
        self.assertIs(store.read(reset.context), reset)

    def test_churn_and_restarts_retain_one_bounded_snapshot(self):
        store = self.store(
            max_candidates=2, max_input_candidates=8, max_snapshot_bytes=8192
        )
        gc.collect()
        tracemalloc.start()
        try:
            for iteration in range(300):
                context = replace(
                    self.context,
                    snapshot_id=f"snapshot{iteration}",
                    frame_sequence=iteration,
                    frame_ids=(f"frame{iteration}",),
                    source_generation=iteration // 25,
                )
                if iteration % 25 == 0:
                    store.reset(context)
                candidates = [
                    self.hypothesis(str(index), context=context, value=0.125)
                    for index in range(8)
                ]
                snapshot = store.publish(context, candidates)
                self.assertLessEqual(len(snapshot.candidates), 2)
                self.assertLessEqual(len(snapshot.to_bytes()), 8192)
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(current, 1_000_000)
        self.assertLess(peak, 3_000_000)
        self.assertGreater(snapshot.hypothesis_set_version, 300)
