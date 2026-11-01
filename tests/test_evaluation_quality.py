from dataclasses import FrozenInstanceError
import pickle
import unittest

from ai_game_player.evaluation_quality import NumericQuality


class NumericQualityTests(unittest.TestCase):
    def test_huge_numeric_fields_are_rejected_as_value_errors(self):
        for kwargs in (
            {"value": 10**10000},
            {"confidence": 10**10000},
            {"coverage": 10**10000},
        ):
            with self.assertRaises(ValueError):
                NumericQuality(**kwargs)

    def test_missingness_is_distinct_from_measured_neutral_quality(self):
        missing = NumericQuality()
        neutral = NumericQuality(0, 0.8, True, provenance=("metric.verified",))
        self.assertFalse(missing.available)
        self.assertTrue(neutral.available)
        self.assertEqual(missing.effective_value, neutral.effective_value)
        self.assertNotEqual(missing.to_dict(), neutral.to_dict())

    def test_support_shrinks_soft_value_without_claiming_probability_calibration(self):
        quality = NumericQuality(
            -0.8, 0.25, True, profile_version="profile.2", provenance=("source.x",)
        )
        self.assertAlmostEqual(quality.effective_value, -0.2)
        self.assertEqual(NumericQuality.from_dict(quality.to_dict()), quality)
        self.assertEqual(pickle.loads(pickle.dumps(quality)), quality)
        copied = quality.to_dict()
        copied["provenance"].append("changed")
        self.assertEqual(quality.provenance, ("source.x",))
        with self.assertRaises(FrozenInstanceError):
            quality.value = 1

    def test_partial_coverage_never_becomes_certain_and_unknown_bounds_remain_open(
        self,
    ):
        quality = NumericQuality(
            0.8, 0.2, True, coverage=0.25, missing_axes=("survival", "resources")
        )
        self.assertEqual(quality.coverage, 0.25)
        self.assertAlmostEqual(quality.effective_value, 0.16)
        self.assertAlmostEqual(quality.bounds[0], -0.64)
        self.assertAlmostEqual(quality.bounds[1], 0.96)
        self.assertEqual(NumericQuality().bounds, (-1, 1))
        self.assertEqual(NumericQuality().coverage, 0)
        with self.assertRaises(ValueError):
            NumericQuality(0.5, 0.8, True, coverage=0.2)

    def test_invalid_unknown_values_nonfinite_versions_and_raw_references_rejected(
        self,
    ):
        for values in (
            {"value": 0.1},
            {"confidence": 0.1},
            {"value": float("nan")},
            {"available": 1},
            {"value": True},
            {"value": 2, "available": True},
            {"projection_version": 2},
            {"provenance": ("raw label text",)},
            {"profile_version": "sk-secret"},
            {"provenance": ["source.x"]},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                NumericQuality(**values)
        bad = NumericQuality().to_dict()
        bad["extra"] = "unchecked"
        with self.assertRaises(ValueError):
            NumericQuality.from_dict(bad)


if __name__ == "__main__":
    unittest.main()
