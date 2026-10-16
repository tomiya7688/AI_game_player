import unittest

from ai_game_player.unknownness import (
    UnknownnessEvidence,
    UnknownnessMonitor,
    UnknownnessScope,
    UnknownnessScopeAssessment,
    UnknownnessType,
    UnknownnessResult,
)


def evidence(
    signal_id: str,
    score: float,
    *,
    scope: UnknownnessScope = UnknownnessScope.PERCEPTUAL,
    unknown_type: UnknownnessType = UnknownnessType.LOW_RECOGNITION_CONFIDENCE,
    confidence: float = 1.0,
    weight: float = 1.0,
) -> UnknownnessEvidence:
    return UnknownnessEvidence(
        signal_id,
        unknown_type,
        scope,
        score,
        confidence,
        weight,
        "test-detector",
        f"{signal_id} reports normalized unknownness {score}",
    )


class UnknownnessEvidenceTests(unittest.TestCase):
    def test_serializes_normalized_signal_with_provenance(self):
        item = evidence("recognition", 0.8)

        payload = item.to_dict()

        self.assertEqual(payload["schema"], "unknownness-evidence/v1")
        self.assertEqual(payload["unknown_type"], "low_recognition_confidence")
        self.assertEqual(payload["scope"], "perceptual")
        self.assertEqual(payload["unknown_score"], 0.8)
        self.assertEqual(payload["source"], "test-detector")

    def test_rejects_invalid_scores_confidence_weight_and_provenance(self):
        invalid_values = (
            {"unknown_score": -0.1},
            {"unknown_score": 1.1},
            {"unknown_score": True},
            {"unknown_score": 10**1000},
            {"evidence_confidence": float("nan")},
            {"evidence_confidence": float("inf")},
            {"weight": 0.0},
            {"weight": -1.0},
            {"weight": float("inf")},
            {"weight": 10**1000},
            {"source": " "},
            {"explanation": ""},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                values = {
                    "signal_id": "valid-signal",
                    "unknown_type": UnknownnessType.RETRIEVAL_MISS,
                    "scope": UnknownnessScope.KNOWLEDGE,
                    "unknown_score": 0.5,
                    "evidence_confidence": 0.9,
                    "weight": 1.0,
                    "source": "test-detector",
                    "explanation": "A retrieval result was missing",
                }
                values.update(overrides)
                with self.assertRaises(ValueError):
                    UnknownnessEvidence(**values)


class UnknownnessMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = UnknownnessMonitor()

    def test_fuses_signals_by_weight_and_evidence_confidence(self):
        result = self.monitor.evaluate(
            "frame-1",
            (
                evidence("recognition", 1.0, weight=3.0, confidence=1.0),
                evidence(
                    "retrieval",
                    0.0,
                    weight=1.0,
                    confidence=0.5,
                    unknown_type=UnknownnessType.RETRIEVAL_MISS,
                ),
            ),
        )

        perceptual = next(item for item in result.scopes if item.scope is UnknownnessScope.PERCEPTUAL)
        semantic = next(item for item in result.scopes if item.scope is UnknownnessScope.SEMANTIC)
        self.assertEqual(perceptual.score, 0.857143)
        self.assertEqual(perceptual.confidence, 0.875)
        self.assertIsNone(semantic.score)
        self.assertEqual(semantic.confidence, 0.0)
        self.assertTrue(result.instant_unknown)
        self.assertEqual(result.instant_scopes, (UnknownnessScope.PERCEPTUAL,))
        self.assertEqual(result.instant_types, (UnknownnessType.LOW_RECOGNITION_CONFIDENCE,))

    def test_weighted_fusion_does_not_average_across_unrelated_scopes(self):
        result = self.monitor.evaluate(
            "frame-1",
            (
                evidence("unknown-screen", 0.9),
                evidence("known-action", 0.0, scope=UnknownnessScope.ACTION),
            ),
        )

        self.assertEqual(result.instant_scopes, (UnknownnessScope.PERCEPTUAL,))
        self.assertFalse(next(item for item in result.scopes if item.scope is UnknownnessScope.ACTION).instant_unknown)

    def test_represents_each_supported_cause_and_scope(self):
        causes_and_scopes = (
            (UnknownnessType.LOW_RECOGNITION_CONFIDENCE, UnknownnessScope.PERCEPTUAL),
            (UnknownnessType.PROTOTYPE_DISTANCE, UnknownnessScope.PERCEPTUAL),
            (UnknownnessType.RETRIEVAL_MISS, UnknownnessScope.KNOWLEDGE),
            (UnknownnessType.EVALUATOR_DISAGREEMENT, UnknownnessScope.SEMANTIC),
            (UnknownnessType.TRANSITION_AMBIGUITY, UnknownnessScope.TRANSITION),
            (UnknownnessType.EVALUATOR_DISAGREEMENT, UnknownnessScope.ACTION),
        )
        result = self.monitor.evaluate(
            "frame-1",
            tuple(
                evidence(
                    f"signal-{index}",
                    0.8,
                    scope=scope,
                    unknown_type=unknown_type,
                )
                for index, (unknown_type, scope) in enumerate(causes_and_scopes)
            ),
        )

        self.assertEqual(result.instant_types, tuple(UnknownnessType))
        self.assertEqual(set(result.instant_scopes), {scope for _, scope in causes_and_scopes})

    def test_marks_unknown_instantly_and_latches_after_consecutive_observations(self):
        first = self.monitor.evaluate("frame-1", (evidence("recognition", 0.8),))
        second = self.monitor.evaluate("frame-2", (evidence("recognition", 0.8),))

        self.assertTrue(first.instant_unknown)
        self.assertFalse(first.persistent_unknown)
        self.assertTrue(second.persistent_unknown)
        self.assertEqual(second.persistent_types, (UnknownnessType.LOW_RECOGNITION_CONFIDENCE,))
        self.assertEqual(len(second.to_dict()["scopes"]), len(UnknownnessScope))

    def test_transient_unknown_does_not_latch_after_a_low_observation(self):
        self.monitor.evaluate("frame-1", (evidence("recognition", 0.9),))

        recovered = self.monitor.evaluate("frame-2", (evidence("recognition", 0.1),))

        self.assertFalse(recovered.instant_unknown)
        self.assertFalse(recovered.persistent_unknown)

    def test_latched_unknown_requires_consecutive_low_observations_to_clear(self):
        self.monitor.evaluate("frame-1", (evidence("recognition", 0.9),))
        latched = self.monitor.evaluate("frame-2", (evidence("recognition", 0.9),))
        first_low = self.monitor.evaluate("frame-3", (evidence("recognition", 0.1),))
        second_low = self.monitor.evaluate("frame-4", (evidence("recognition", 0.1),))

        self.assertTrue(latched.persistent_unknown)
        self.assertTrue(first_low.persistent_unknown)
        self.assertFalse(second_low.scopes[0].instant_unknown)
        self.assertFalse(second_low.persistent_unknown)
        self.assertEqual(second_low.persistent_scopes, ())

    def test_missing_or_neutral_evidence_does_not_clear_latched_unknown(self):
        self.monitor.evaluate("frame-1", (evidence("recognition", 0.9),))
        self.monitor.evaluate("frame-2", (evidence("recognition", 0.9),))

        missing = self.monitor.evaluate("frame-3", ())
        neutral = self.monitor.evaluate("frame-4", (evidence("recognition", 0.5),))

        self.assertTrue(missing.persistent_unknown)
        self.assertTrue(neutral.persistent_unknown)

    def test_neutral_observation_breaks_consecutive_recovery_count(self):
        self.monitor.evaluate("frame-1", (evidence("recognition", 0.9),))
        self.monitor.evaluate("frame-2", (evidence("recognition", 0.9),))
        self.monitor.evaluate("frame-3", (evidence("recognition", 0.1),))
        self.monitor.evaluate("frame-4", (evidence("recognition", 0.5),))
        first_recovery = self.monitor.evaluate("frame-5", (evidence("recognition", 0.1),))
        second_recovery = self.monitor.evaluate("frame-6", (evidence("recognition", 0.1),))

        self.assertTrue(first_recovery.persistent_unknown)
        self.assertFalse(second_recovery.persistent_unknown)

    def test_exposes_all_contributing_reasons_and_serializable_result(self):
        result = self.monitor.evaluate(
            "frame-1",
            (
                evidence("recognition", 0.9),
                evidence("retrieval", 0.8, unknown_type=UnknownnessType.RETRIEVAL_MISS),
            ),
        )

        self.assertEqual(
            result.instant_types,
            (UnknownnessType.LOW_RECOGNITION_CONFIDENCE, UnknownnessType.RETRIEVAL_MISS),
        )
        payload = result.to_dict()
        self.assertEqual(payload["schema"], "unknownness/v1")
        self.assertEqual(payload["instant_types"], [
            "low_recognition_confidence",
            "retrieval_miss",
        ])
        self.assertEqual(len(payload["scopes"]), len(UnknownnessScope))

    def test_rejects_reused_observation_and_duplicate_signal_ids(self):
        item = evidence("recognition", 0.9)
        self.monitor.evaluate("frame-1", (item,))

        with self.assertRaises(ValueError):
            self.monitor.evaluate("frame-1", ())
        with self.assertRaises(ValueError):
            self.monitor.evaluate("frame-2", (item, item))

    def test_rejects_non_iterable_evidence_with_a_value_error(self):
        with self.assertRaises(ValueError):
            self.monitor.evaluate("frame-1", None)

    def test_reset_clears_latch_and_observation_identity(self):
        self.monitor.evaluate("frame-1", (evidence("recognition", 0.9),))
        self.monitor.evaluate("frame-2", (evidence("recognition", 0.9),))

        self.monitor.reset()
        result = self.monitor.evaluate("frame-1", ())

        self.assertFalse(result.persistent_unknown)

    def test_rejects_invalid_monitor_thresholds_and_observation_counts(self):
        for arguments in (
            {"unknown_threshold": 1.1},
            {"clear_threshold": 0.65},
            {"confirm_observations": 0},
            {"confirm_observations": True},
            {"recovery_observations": 1.5},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    UnknownnessMonitor(**arguments)

    def test_result_rejects_duplicate_scope_assessments(self):
        assessment = UnknownnessScopeAssessment(
            UnknownnessScope.PERCEPTUAL,
            None,
            0.0,
            False,
            False,
        )

        with self.assertRaises(ValueError):
            UnknownnessResult("frame-1", (assessment, assessment))

    def test_result_rejects_missing_scope_assessments(self):
        assessment = UnknownnessScopeAssessment(
            UnknownnessScope.PERCEPTUAL,
            None,
            0.0,
            False,
            False,
        )

        with self.assertRaises(ValueError):
            UnknownnessResult("frame-1", (assessment,))

    def test_result_rejects_mutable_scope_collection(self):
        assessment = UnknownnessScopeAssessment(
            UnknownnessScope.PERCEPTUAL,
            None,
            0.0,
            False,
            False,
        )

        with self.assertRaises(ValueError):
            UnknownnessResult("frame-1", [assessment])

    def test_scope_assessment_rejects_non_boolean_state_and_foreign_evidence(self):
        with self.assertRaises(ValueError):
            UnknownnessScopeAssessment(
                UnknownnessScope.PERCEPTUAL,
                None,
                0.0,
                "false",
                False,
            )
        with self.assertRaises(ValueError):
            UnknownnessScopeAssessment(
                UnknownnessScope.PERCEPTUAL,
                0.2,
                0.9,
                False,
                False,
                evidence=(evidence("action", 0.2, scope=UnknownnessScope.ACTION),),
            )


if __name__ == "__main__":
    unittest.main()
