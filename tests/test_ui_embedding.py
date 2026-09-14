import tempfile
import unittest
from pathlib import Path

from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.ui_embedding import (
    EmbeddingBatch,
    EmbeddingCache,
    EmbeddingMemory,
    EmbeddingPipeline,
    EmbeddingProviderConfig,
    EmbeddingRetrievalCase,
    EmbeddingRetrievalEvaluator,
    EmbeddingVector,
    GridVisualEmbeddingProvider,
    HashedTextEmbeddingProvider,
)
from ai_game_player.ui_recognition import UiPrototype


class CountingProvider:
    name = "counting"
    lane = "visual"
    model = "test-model"
    version = "1"
    preprocessing = "none"

    def __init__(self, values=(1.0, 0.0), result=True):
        self.values = tuple(values)
        self.result = result
        self.calls = 0

    def embed(self, frame, element):
        self.calls += 1
        if not self.result:
            return None
        return EmbeddingVector(
            self.lane,
            self.values,
            self.name,
            self.model,
            self.version,
            self.preprocessing,
            element.confidence,
        )


class SemanticFallbackProvider:
    name = "semantic-fallback"
    lane = "semantic"
    model = "semantic-test"
    version = "2"
    preprocessing = "test"

    def __init__(self):
        self.calls = 0

    def embed(self, frame, element):
        self.calls += 1
        return EmbeddingVector("semantic", (1.0, 0.0, 0.0, 0.0), self.name, self.model, self.version, self.preprocessing, 0.9)


class UiEmbeddingTest(unittest.TestCase):
    def frame(self, value=128):
        pixel = bytes((value, value, value, 255))
        return ScreenFrame(32, 24, pixel * (32 * 24))

    def element(self, text="START", confidence=0.9):
        return DetectedElement("button", "button", (4, 4, 16, 10), "test", confidence, text)

    def vector(self, values, lane="visual", model="m", version="1", preprocessing="p"):
        return EmbeddingVector(lane, tuple(values), "test", model, version, preprocessing, 1.0)

    def test_visual_and_semantic_embeddings_stay_separate_with_metadata(self):
        pipeline = EmbeddingPipeline(
            [
                EmbeddingProviderConfig(GridVisualEmbeddingProvider(4)),
                EmbeddingProviderConfig(HashedTextEmbeddingProvider(16)),
            ]
        )
        batch = pipeline.embed(self.frame(), self.element())
        self.assertEqual(set(batch.lanes()), {"visual", "semantic"})
        visual = batch.lanes()["visual"][0]
        semantic = batch.lanes()["semantic"][0]
        self.assertEqual(visual.dimension, 16)
        self.assertEqual(semantic.dimension, 16)
        self.assertEqual(visual.model, "brightness-grid")
        self.assertEqual(visual.version, "1")
        self.assertEqual(semantic.preprocessing, "casefold-whitespace")
        self.assertFalse(visual.compatible_with(semantic))

    def test_exact_crop_cache_skips_repeated_provider_call(self):
        provider = CountingProvider()
        cache = EmbeddingCache()
        pipeline = EmbeddingPipeline([EmbeddingProviderConfig(provider)], cache)
        first = pipeline.embed(self.frame(), self.element())
        second = pipeline.embed(self.frame(), self.element())
        self.assertEqual(provider.calls, 1)
        self.assertFalse(first.statuses[0].cache_hit)
        self.assertTrue(second.statuses[0].cache_hit)
        self.assertEqual(len(cache), 1)

    def test_fallback_runs_only_for_missing_lane(self):
        visual = CountingProvider()
        empty_semantic = HashedTextEmbeddingProvider(8)
        fallback = SemanticFallbackProvider()
        pipeline = EmbeddingPipeline(
            [
                EmbeddingProviderConfig(visual),
                EmbeddingProviderConfig(empty_semantic),
                EmbeddingProviderConfig(fallback, fallback=True),
            ]
        )
        batch = pipeline.embed(self.frame(), self.element(text=None))
        self.assertEqual({vector.lane for vector in batch.vectors}, {"visual", "semantic"})
        self.assertEqual(fallback.calls, 1)
        fallback_status = next(status for status in batch.statuses if status.provider == fallback.name)
        self.assertEqual(fallback_status.state, "success")

    def test_fallback_is_skipped_when_primary_lane_exists(self):
        primary = HashedTextEmbeddingProvider(8)
        fallback = SemanticFallbackProvider()
        pipeline = EmbeddingPipeline(
            [EmbeddingProviderConfig(primary), EmbeddingProviderConfig(fallback, fallback=True)]
        )
        batch = pipeline.embed(self.frame(), self.element("PLAY"))
        self.assertEqual(fallback.calls, 0)
        fallback_status = next(status for status in batch.statuses if status.provider == fallback.name)
        self.assertEqual(fallback_status.state, "skipped")

    def test_nearest_neighbor_keeps_embedding_spaces_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = EmbeddingMemory(Path(directory) / "embedding.json")
            element = self.element()
            memory.add("game-a", "start", element, EmbeddingBatch((self.vector((1.0, 0.0)),), ()))
            memory.add("game-a", "quit", element, EmbeddingBatch((self.vector((0.0, 1.0)),), ()))
            memory.add("game-a", "wrong-model", element, EmbeddingBatch((self.vector((1.0, 0.0), model="other"),), ()))
            neighbors = memory.nearest(self.vector((0.99, 0.01)), game_id="game-a")
            self.assertEqual(neighbors[0].identity_id, "start")
            self.assertNotIn("wrong-model", [neighbor.identity_id for neighbor in neighbors])

    def test_same_game_and_cross_game_retrieval_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = EmbeddingMemory(Path(directory) / "embedding.json")
            element = self.element()
            start = self.vector((1.0, 0.0))
            quit_vector = self.vector((0.0, 1.0))
            memory.add("game-a", "start", element, EmbeddingBatch((start,), ()))
            memory.add("game-a", "quit", element, EmbeddingBatch((quit_vector,), ()))
            memory.add("game-b", "start", element, EmbeddingBatch((self.vector((0.98, 0.02)),), ()))
            memory.add("game-b", "quit", element, EmbeddingBatch((self.vector((0.02, 0.98)),), ()))
            evaluator = EmbeddingRetrievalEvaluator()
            case = EmbeddingRetrievalCase(self.vector((0.95, 0.05)), "start", "game-a")
            same = evaluator.evaluate(memory, [case], top_k=1)
            cross = evaluator.evaluate(memory, [case], top_k=1, cross_game=True)
            self.assertEqual(same.accuracy_at_1, 1.0)
            self.assertEqual(cross.accuracy_at_1, 1.0)

    def test_ui_prototype_builds_model_specific_centroid(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = EmbeddingMemory(Path(directory) / "embedding.json")
            element = self.element()
            memory.add("game-a", "visual-start", element, EmbeddingBatch((self.vector((1.0, 0.0)),), ()))
            memory.add("game-b", "visual-start", element, EmbeddingBatch((self.vector((0.8, 0.2)),), ()))
            ui_prototype = UiPrototype(
                "prototype-start",
                "visual-start",
                "button",
                (0.1, 0.1, 0.5, 0.2),
                ("game-a", "game-b"),
                0.9,
                "START",
            )
            built = memory.build_from_ui_prototypes([ui_prototype])
            self.assertEqual(len(built), 1)
            self.assertEqual(built[0].ui_prototype_id, "prototype-start")
            self.assertEqual(built[0].source_games, ("game-a", "game-b"))
            nearest = memory.nearest_prototypes(self.vector((1.0, 0.0)), top_k=1)
            self.assertEqual(nearest[0].ui_prototype_id, "prototype-start")
            self.assertEqual(nearest[0].identity_id, "visual-start")

    def test_persistent_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            vector = self.vector((1.0, 0.0))
            EmbeddingCache(path).put("key", vector)
            loaded = EmbeddingCache(path).get("key")
            self.assertEqual(loaded, vector)


if __name__ == "__main__":
    unittest.main()
