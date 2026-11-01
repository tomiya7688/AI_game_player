from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from ai_game_player.frame_preprocessor import PythonFramePreprocessor
from ai_game_player.runtime.contracts import RuntimeCapability
from test_game_2048_adapter import render_fixture, read_fixture
from tools.planning_frame_parity import validate_native_preprocessing


class PlanningFrameParityTests(unittest.TestCase):
    def test_same_pixel_validation_includes_candidate_semantic_and_decision_effects(
        self,
    ):
        frame = render_fixture(read_fixture("standard.json"))
        reference = PythonFramePreprocessor()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture-library"
            path.write_bytes(b"fixture only")
            runtime = SimpleNamespace(
                library_path=path,
                info=SimpleNamespace(abi_version=1),
                descriptor=SimpleNamespace(
                    capabilities=frozenset({RuntimeCapability.FAST_CV})
                ),
                preprocess=reference.preprocess,
            )
            report = validate_native_preprocessing(frame, runtime)
            self.assertTrue(report["match"])
            self.assertTrue(report["baseline_decision_equal"])
            self.assertTrue(report["planner_input_equal"])

            def changed(frame, **options):
                batch = reference.preprocess(frame, **options)
                return replace(batch, mean_brightness=batch.mean_brightness + 1)

            runtime.preprocess = changed
            mismatch = validate_native_preprocessing(frame, runtime)
            self.assertFalse(mismatch["match"])
            self.assertFalse(mismatch["primitive_fields_equal"]["mean_brightness"])
