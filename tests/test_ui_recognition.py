import tempfile
import unittest
from pathlib import Path

from ai_game_player.bright_region_detector import BrightRegionDetector
from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.ui_recognition import (
    DetectorProviderAdapter,
    KnownUiDetector,
    UiDetectionImpactEvaluator,
    UiDetectionPipeline,
    UiProviderConfig,
    UiRecognitionMemory,
    UiTransferEvaluator,
)


class FakeProvider:
    def __init__(self, name, elements=None, error=None):
        self.name = name
        self.elements = list(elements or [])
        self.error = error
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return list(self.elements)


def make_frame(width=20, height=12, bbox=(4, 3, 8, 4), value=255):
    pixels = bytearray(width * height * 4)
    left, top, region_width, region_height = bbox
    for y in range(top, top + region_height):
        for x in range(left, left + region_width):
            offset = (y * width + x) * 4
            pixels[offset : offset + 4] = bytes((value, value, value, 255))
    return ScreenFrame(width, height, bytes(pixels))


class UiRecognitionTest(unittest.TestCase):
    def test_initial_game_uses_generic_provider(self):
        provider = DetectorProviderAdapter("bright", BrightRegionDetector(min_pixels=9))
        pipeline = UiDetectionPipeline([UiProviderConfig(provider, generic=True)])
        batch = pipeline.detect_batch(make_frame())
        self.assertTrue(batch.generic_used)
        self.assertEqual(len(batch.elements), 1)
        self.assertEqual(batch.elements[0].bbox, (4, 3, 8, 4))
        self.assertIn("bright_region", batch.elements[0].source)

    def test_multiple_detectors_fuse_source_and_confidence(self):
        first = FakeProvider("shape", [DetectedElement("a", "button", (4, 3, 8, 4), "shape", 0.7, "START")])
        second = FakeProvider("edge", [DetectedElement("b", "button", (4, 3, 8, 4), "edge", 0.6, " start ")])
        pipeline = UiDetectionPipeline([UiProviderConfig(first), UiProviderConfig(second)])
        batch = pipeline.detect_batch(make_frame())
        self.assertEqual(len(batch.elements), 1)
        self.assertGreater(batch.elements[0].confidence, 0.7)
        self.assertIn("shape", batch.elements[0].source)
        self.assertIn("edge", batch.elements[0].source)

    def test_provider_failure_is_isolated(self):
        broken = FakeProvider("broken", error="model load failed")
        healthy = FakeProvider("healthy", [DetectedElement("ok", "button", (4, 3, 8, 4), "healthy", 0.8)])
        batch = UiDetectionPipeline([UiProviderConfig(broken), UiProviderConfig(healthy)]).detect_batch(make_frame())
        self.assertEqual(len(batch.elements), 1)
        status = next(value for value in batch.statuses if value.provider == "broken")
        self.assertEqual(status.state, "error")
        self.assertIn("model load failed", status.error)

    def test_memory_keeps_visual_interaction_transition_and_negative_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = UiRecognitionMemory(Path(directory) / "ui_memory.json")
            frame = make_frame()
            element = DetectedElement("start", "button", (4, 3, 8, 4), "generic", 0.95, "START")
            positive = memory.record("game-a", frame, element, interaction_id="click", transition_id="menu->play")
            negative = memory.record("game-a", frame, element, sample_kind="hard_negative", interaction_id="ignore", transition_id="none")
            loaded = memory.samples("game-a")
            self.assertEqual(len(loaded), 2)
            self.assertEqual(positive.visual_id, negative.visual_id)
            self.assertEqual(positive.interaction_id, "click")
            self.assertEqual(positive.transition_id, "menu->play")
            self.assertEqual(negative.sample_kind, "hard_negative")

    def test_known_ui_skips_generic_detector(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = UiRecognitionMemory(Path(directory) / "ui_memory.json")
            frame = make_frame()
            element = DetectedElement("start", "button", (4, 3, 8, 4), "generic", 0.95, "START")
            memory.record("game-a", frame, element)
            known = KnownUiDetector(memory, "game-a")
            generic = FakeProvider("heavy", [element])
            pipeline = UiDetectionPipeline(
                [UiProviderConfig(known, generic=False), UiProviderConfig(generic, generic=True)],
                known_threshold=0.8,
            )
            batch = pipeline.detect_batch(frame)
            self.assertFalse(batch.generic_used)
            self.assertEqual(generic.calls, 0)
            self.assertEqual(len(batch.elements), 1)
            self.assertEqual(batch.elements[0].text, "START")

    def test_unknown_ui_falls_back_to_generic_detector(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = UiRecognitionMemory(Path(directory) / "ui_memory.json")
            known = KnownUiDetector(memory, "game-a")
            element = DetectedElement("start", "button", (4, 3, 8, 4), "generic", 0.9, "START")
            generic = FakeProvider("heavy", [element])
            batch = UiDetectionPipeline(
                [UiProviderConfig(known, generic=False), UiProviderConfig(generic, generic=True)]
            ).detect_batch(make_frame())
            self.assertTrue(batch.generic_used)
            self.assertEqual(generic.calls, 1)
            self.assertEqual(len(batch.elements), 1)

    def test_cross_game_prototype_requires_multiple_games(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = UiRecognitionMemory(Path(directory) / "ui_memory.json")
            frame = make_frame()
            first = DetectedElement("a", "button", (4, 3, 8, 4), "generic", 0.9, "START")
            second = DetectedElement("b", "button", (4, 3, 8, 4), "generic", 0.8, "NEXT")
            memory.record("game-a", frame, first)
            self.assertEqual(memory.promote_cross_game(), [])
            memory.record("game-b", frame, second)
            prototypes = memory.promote_cross_game()
            self.assertEqual(len(prototypes), 1)
            self.assertEqual(prototypes[0].source_games, ("game-a", "game-b"))
            transferred = memory.detect_prototypes(frame)
            self.assertEqual(len(transferred), 1)
            self.assertEqual(transferred[0].source, "ui_cross_game_prototype")

    def test_cross_game_transfer_metrics_measure_miss_and_false_positive(self):
        reference = [
            DetectedElement("a", "button", (0, 0, 10, 10), "reference", 1.0),
            DetectedElement("b", "button", (20, 0, 10, 10), "reference", 1.0),
        ]
        detected = [
            DetectedElement("x", "button", (0, 0, 10, 10), "prototype", 0.9),
            DetectedElement("y", "button", (40, 0, 10, 10), "prototype", 0.9),
        ]
        metrics = UiTransferEvaluator().evaluate(reference, detected)
        self.assertEqual(metrics.true_positive, 1)
        self.assertEqual(metrics.false_positive, 1)
        self.assertEqual(metrics.missed, 1)
        self.assertEqual(metrics.precision, 0.5)
        self.assertEqual(metrics.recall, 0.5)

    def test_detection_impact_separates_recall_from_wrong_operation(self):
        reference = [DetectedElement("a", "button", (0, 0, 10, 10), "reference", 1.0)]
        impact = UiDetectionImpactEvaluator().assess(reference, [], "start", "quit", "quit")
        self.assertEqual(impact.recall, 0.0)
        self.assertTrue(impact.decision_error)
        self.assertTrue(impact.execution_error)


if __name__ == "__main__":
    unittest.main()
