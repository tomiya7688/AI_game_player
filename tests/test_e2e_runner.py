import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.e2e_runner import ContinuousE2ERunner
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation


class FakeResult:
    def __init__(self, action_id: str = "advance") -> None:
        self.action_id = action_id
        self.executed = True
        self.mode = "live"
        self.detail = "fake live input"


class FakeTrace:
    def __init__(self) -> None:
        self.entry = None

    def recent(self, limit: int = 1):
        return [self.entry] if self.entry is not None and limit else []


class FakeEngine:
    def __init__(self) -> None:
        self.trace = FakeTrace()


class FakeController:
    is_running = True


class FakePipeline:
    def __init__(self) -> None:
        self.engine = FakeEngine()
        self.controller = FakeController()
        self.steps = 0
        self.outcomes = []

    def run_and_execute(self, purpose: str = ""):
        self.steps += 1
        self.engine.trace.entry = {
            "context": {
                "state": {
                    "screen_id": "sample",
                    "signature": f"before-{self.steps}",
                    "detected_element_count": 1,
                },
                "candidates": [{"action_id": "advance"}],
            },
            "decision": {"action_id": "advance", "provider": "fake", "reason": purpose},
        }
        return FakeResult()

    def record_safety_outcome(self, status: str, confidence: float, evidence: str = "") -> None:
        self.outcomes.append((status, confidence, evidence))


class E2ERunnerTest(unittest.TestCase):
    def test_reaches_milestone_and_records_closed_loop_trace(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation(
                "sample",
                100,
                80,
                features={"signature": f"after-{pipeline.steps}", "detected_elements": [{}]},
            )

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "e2e.jsonl"
            runner = ContinuousE2ERunner(
                pipeline,
                observation,
                log,
                milestone_probe=lambda: pipeline.steps >= 3,
                max_steps=6,
                max_wall_seconds=5,
                settle_seconds=0,
            )
            report = runner.run("advance")
            lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(report.success)
        self.assertEqual(report.steps, 3)
        self.assertEqual(report.stop_reason, "milestone_reached")
        self.assertTrue(report.live_input_verified)
        self.assertEqual(len([entry for entry in lines if entry["event"] == "step"]), 3)
        self.assertEqual(lines[0]["candidate_actions"], ["advance"])
        self.assertEqual(lines[0]["provider"], "fake")
        self.assertTrue(lines[0]["outcome"]["state_changed"])
        self.assertEqual(lines[-1]["event"], "summary")
        self.assertEqual(pipeline.outcomes[-1][0], "success")

    def test_repeated_observation_stops_with_classified_reason(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation("sample", 100, 80, features={"signature": "same"})

        with tempfile.TemporaryDirectory() as directory:
            runner = ContinuousE2ERunner(
                pipeline,
                observation,
                Path(directory) / "e2e.jsonl",
                loop_guard=LoopGuard(limit=3),
                max_steps=10,
                max_wall_seconds=5,
                settle_seconds=0,
            )
            report = runner.run()

        self.assertFalse(report.success)
        self.assertEqual(report.steps, 3)
        self.assertEqual(report.stop_reason, "repeated_observation")

    def test_duration_target_is_a_success_gate(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation("sample", 100, 80, features={"signature": f"sig-{pipeline.steps}"})

        with tempfile.TemporaryDirectory() as directory:
            runner = ContinuousE2ERunner(
                pipeline,
                observation,
                Path(directory) / "e2e.jsonl",
                max_steps=100,
                max_wall_seconds=2,
                minimum_duration_seconds=0.01,
                settle_seconds=0,
                step_delay_seconds=0.01,
            )
            report = runner.run()

        self.assertTrue(report.success)
        self.assertEqual(report.stop_reason, "duration_reached")
        self.assertGreaterEqual(report.steps, 1)


if __name__ == "__main__":
    unittest.main()
