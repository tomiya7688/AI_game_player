import unittest

from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.ocr_provider import (
    OcrDecisionImpactEvaluator,
    OcrFusionPipeline,
    OcrProviderConfig,
    OcrResult,
    RecognizerOcrProvider,
)
from ai_game_player.screen_capture import ScreenFrame


class FakeProvider:
    def __init__(self, name, model, results=None, preprocessing="default", error=None):
        self.name = name
        self.model = model
        self.preprocessing = preprocessing
        self.results = list(results or [])
        self.error = error
        self.calls = 0

    def recognize(self, frame, frame_id=""):
        self.calls += 1
        if self.error is not None:
            raise RuntimeError(self.error)
        return [
            OcrResult(
                text,
                bbox,
                confidence,
                self.name,
                self.model,
                self.preprocessing,
                frame_id,
            )
            for text, bbox, confidence in self.results
        ]


class LegacyRecognizer:
    def recognize(self, frame):
        return [
            {
                "text": "START",
                "x": 10,
                "y": 20,
                "width": 50,
                "height": 12,
                "confidence": 0.8,
            }
        ]


class OcrProviderTest(unittest.TestCase):
    def frame(self):
        return ScreenFrame(100, 80, bytes(100 * 80 * 4))

    def test_legacy_recognizer_adapter_adds_provenance(self):
        provider = RecognizerOcrProvider(LegacyRecognizer(), "tesseract", "tesseract-5", "gray")
        result = provider.recognize(self.frame(), "frame-a")[0]
        self.assertEqual(result.source, "tesseract")
        self.assertEqual(result.model, "tesseract-5")
        self.assertEqual(result.preprocessing, "gray")
        self.assertEqual(result.frame_id, "frame-a")
        self.assertEqual(result.bbox, (10, 20, 50, 12))

    def test_multiple_providers_fuse_agreeing_text_and_boxes(self):
        first = FakeProvider("fast", "small", [("START", (10, 20, 50, 12), 0.8)])
        second = FakeProvider("accurate", "large", [(" start ", (12, 20, 48, 12), 0.7)])
        pipeline = OcrFusionPipeline(
            [OcrProviderConfig(first), OcrProviderConfig(second, weight=0.8)],
            fallback_threshold=0.0,
        )
        batch = pipeline.recognize_batch(self.frame(), "menu")
        self.assertEqual(len(batch.results), 2)
        self.assertEqual(len(batch.elements), 1)
        self.assertGreater(batch.elements[0].confidence, 0.8)
        self.assertIn("accurate", batch.elements[0].source)
        self.assertIn("fast", batch.elements[0].source)

    def test_conflicting_text_in_same_region_stays_separate(self):
        first = FakeProvider("a", "m1", [("SAVE", (10, 10, 40, 15), 0.8)])
        second = FakeProvider("b", "m2", [("LOAD", (10, 10, 40, 15), 0.8)])
        pipeline = OcrFusionPipeline([OcrProviderConfig(first), OcrProviderConfig(second)], fallback_threshold=0.0)
        batch = pipeline.recognize_batch(self.frame())
        self.assertEqual({element.text for element in batch.elements}, {"SAVE", "LOAD"})

    def test_low_confidence_primary_triggers_fallback(self):
        primary = FakeProvider("fast", "small", [("HP 10", (5, 5, 30, 10), 0.3)])
        fallback = FakeProvider("strong", "large", [("HP 10", (5, 5, 30, 10), 0.9)])
        pipeline = OcrFusionPipeline(
            [OcrProviderConfig(primary), OcrProviderConfig(fallback, fallback=True)],
            fallback_threshold=0.6,
        )
        batch = pipeline.recognize_batch(self.frame())
        self.assertTrue(batch.fallback_used)
        self.assertEqual(fallback.calls, 1)
        self.assertGreater(batch.elements[0].confidence, 0.9)

    def test_high_confidence_primary_skips_fallback(self):
        primary = FakeProvider("fast", "small", [("START", (5, 5, 30, 10), 0.9)])
        fallback = FakeProvider("strong", "large", [("START", (5, 5, 30, 10), 0.99)])
        pipeline = OcrFusionPipeline(
            [OcrProviderConfig(primary), OcrProviderConfig(fallback, fallback=True)],
            fallback_threshold=0.6,
        )
        batch = pipeline.recognize_batch(self.frame())
        self.assertFalse(batch.fallback_used)
        self.assertEqual(fallback.calls, 0)
        status = next(value for value in batch.statuses if value.provider == "strong")
        self.assertEqual(status.state, "skipped")

    def test_provider_failure_is_isolated(self):
        broken = FakeProvider("broken", "bad", error="load failed")
        healthy = FakeProvider("healthy", "ok", [("GO", (1, 1, 20, 10), 0.8)])
        pipeline = OcrFusionPipeline([OcrProviderConfig(broken), OcrProviderConfig(healthy)], fallback_threshold=0.0)
        batch = pipeline.recognize_batch(self.frame())
        self.assertEqual([element.text for element in batch.elements], ["GO"])
        status = next(value for value in batch.statuses if value.provider == "broken")
        self.assertEqual(status.state, "error")
        self.assertIn("load failed", status.error)

    def test_temporal_consistency_boosts_repeated_evidence(self):
        provider = FakeProvider("fast", "small", [("NEXT", (10, 10, 30, 10), 0.5)])
        pipeline = OcrFusionPipeline([OcrProviderConfig(provider)], fallback_threshold=0.0, temporal_window=3)
        first = pipeline.recognize_batch(self.frame(), "f1").elements[0].confidence
        second = pipeline.recognize_batch(self.frame(), "f2").elements[0].confidence
        self.assertGreater(second, first)

    def test_frame_analyzer_preserves_raw_provenance_and_fused_elements(self):
        provider = FakeProvider("fast", "small", [("START", (10, 20, 50, 12), 0.8)], preprocessing="gray")
        pipeline = OcrFusionPipeline([OcrProviderConfig(provider)], fallback_threshold=0.0)
        observation = FrameAnalyzer(ocr=pipeline).analyze(self.frame(), "menu")
        self.assertEqual(observation.ocr_text, ["START"])
        self.assertEqual(observation.features["ocr_results"][0]["model"], "small")
        self.assertEqual(observation.features["ocr_results"][0]["preprocessing"], "gray")
        self.assertEqual(observation.features["ocr_results"][0]["frame_id"], "menu")
        self.assertEqual(observation.features["ocr_provider_status"][0]["state"], "success")
        self.assertEqual(observation.features["detected_elements"][0]["element_type"], "text")

    def test_decision_impact_separates_recognition_only_from_downstream_error(self):
        evaluator = OcrDecisionImpactEvaluator()
        recognition_only = evaluator.assess("START", "5TART", "start", "start", "start")
        self.assertEqual(recognition_only.impact, "recognition_only")
        decision_error = evaluator.assess("START", "5TART", "start", "quit", "quit")
        self.assertTrue(decision_error.recognition_error)
        self.assertTrue(decision_error.decision_error)
        self.assertTrue(decision_error.execution_error)
        self.assertEqual(decision_error.impact, "execution_error")

    def test_invalid_weight_is_rejected(self):
        provider = FakeProvider("fast", "small")
        with self.assertRaises(ValueError):
            OcrProviderConfig(provider, weight=1.1)


if __name__ == "__main__":
    unittest.main()
