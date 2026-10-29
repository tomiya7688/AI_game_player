import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.e2e_runner import run_continuous_e2e
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.no_allowed_candidates_error import NoAllowedCandidatesError
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.provider import RuleProvider


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

    def record_safety_outcome(
        self, status: str, confidence: float, evidence: str = ""
    ) -> None:
        self.outcomes.append((status, confidence, evidence))


class CompletionBoundaryPipeline(FakePipeline):
    def __init__(self, error, final_steps=1, mode="live", stop_on_failure=False):
        super().__init__()
        self.error = error
        self.final_steps = final_steps
        self.mode = mode
        self.stop_on_failure = stop_on_failure
        self.completed = False
        self.run_attempts = 0

    def run_and_execute(self, purpose=""):
        self.run_attempts += 1
        if self.steps == self.final_steps:
            self.completed = True
            if self.stop_on_failure:
                self.controller.is_running = False
            raise self.error
        result = super().run_and_execute(purpose)
        result.mode = self.mode
        return result


class E2ERunnerTest(unittest.TestCase):
    def test_reaches_milestone_and_records_closed_loop_trace(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation(
                "sample",
                100,
                80,
                features={
                    "signature": f"after-{pipeline.steps}",
                    "detected_elements": [{}],
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "e2e.jsonl"
            report = run_continuous_e2e(
                pipeline,
                observation,
                log,
                milestone_probe=lambda: pipeline.steps >= 3,
                max_steps=6,
                max_wall_seconds=5,
                settle_seconds=0,
                purpose="advance",
            )
            lines = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(report["success"])
        self.assertEqual(report["steps"], 3)
        self.assertEqual(report["stop_reason"], "milestone_reached")
        self.assertTrue(report["live_input_verified"])
        self.assertEqual(len([entry for entry in lines if entry["event"] == "step"]), 3)
        self.assertEqual(lines[0]["candidate_actions"], ["advance"])
        self.assertEqual(lines[0]["provider"], "fake")
        self.assertTrue(lines[0]["outcome"]["state_changed"])
        self.assertEqual(lines[-1]["event"], "summary")
        self.assertEqual(pipeline.outcomes[-1][0], "success")

    def run_boundary_case(self, pipeline, milestone_probe=None):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "e2e.jsonl"
            report = run_continuous_e2e(
                pipeline,
                lambda: ScreenObservation(
                    "sample", 100, 80, features={"signature": f"after-{pipeline.steps}"}
                ),
                log,
                milestone_probe=milestone_probe or (lambda: pipeline.completed),
                max_steps=30,
                max_wall_seconds=5,
                settle_seconds=0,
            )
            events = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
        return report, events

    def test_rule_engine_preserves_empty_candidate_error_type(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory), RuleProvider())
            with self.assertRaises(NoAllowedCandidatesError) as raised:
                engine.step(ScreenObservation("sample", 100, 80), [])
            self.assertIsInstance(raised.exception, ValueError)

    def test_rejected_existing_candidates_do_not_use_completion_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory), RuleProvider())
            with self.assertRaises(ValueError) as raised:
                engine.step(
                    ScreenObservation("sample", 100, 80),
                    [
                        ActionCandidate(
                            "dangerous", "click", "blocked", 10, 10, 0.9, dangerous=True
                        )
                    ],
                )
            self.assertNotIsInstance(raised.exception, NoAllowedCandidatesError)

    def test_completion_during_next_candidate_read_ends_without_extra_input(self):
        pipeline = CompletionBoundaryPipeline(
            NoAllowedCandidatesError("許可された操作候補がありません"), final_steps=24
        )
        report, events = self.run_boundary_case(pipeline)
        self.assertTrue(pipeline.completed)
        self.assertTrue(report["success"])
        self.assertEqual(report["steps"], 24)
        self.assertEqual(report["stop_reason"], "milestone_reached")
        self.assertTrue(report["live_input_verified"])
        self.assertEqual(pipeline.steps, 24)
        self.assertEqual(pipeline.run_attempts, 25)
        self.assertEqual(
            len([event for event in events if event["event"] == "step"]), 24
        )
        self.assertEqual(events[-2]["event"], "milestone")
        self.assertEqual(events[-2]["trigger"], "no_allowed_candidates")
        self.assertEqual(len(pipeline.outcomes), 24)

    def test_completed_probe_does_not_replace_other_failures(self):
        for error, category in (
            (ValueError("候補の形式が不正"), "recognition_or_candidate_failure"),
            (RuntimeError("safety blocked"), "safety_block"),
            (RuntimeError("capture failed"), "target_or_capture_failure"),
            (RuntimeError("controller stopped"), "controller_stopped"),
        ):
            with self.subTest(error=error):
                pipeline = CompletionBoundaryPipeline(error)
                probes = []
                report, _ = self.run_boundary_case(
                    pipeline,
                    lambda: probes.append(pipeline.completed) or pipeline.completed,
                )
                self.assertFalse(report["success"])
                self.assertEqual(report["stop_reason"], category)
                self.assertEqual(probes, [False])

    def test_empty_candidates_require_completion_live_input_and_running_controller(
        self,
    ):
        for options in (
            {"mode": "dry_run"},
            {"final_steps": 0},
            {"stop_on_failure": True},
            {"milestone_probe": lambda: False},
        ):
            with self.subTest(options=options):
                options = dict(options)
                probe = options.pop("milestone_probe", None)
                pipeline = CompletionBoundaryPipeline(
                    NoAllowedCandidatesError("許可された操作候補がありません"),
                    **options,
                )
                report, _ = self.run_boundary_case(pipeline, probe)
                self.assertFalse(report["success"])
                self.assertEqual(
                    report["stop_reason"], "recognition_or_candidate_failure"
                )

    def test_milestone_recheck_error_invalid_value_and_stop_fail_closed(self):
        for failure in ("error", "invalid", "stopped"):
            with self.subTest(failure=failure):
                pipeline = CompletionBoundaryPipeline(
                    NoAllowedCandidatesError("許可された操作候補がありません")
                )

                def probe():
                    if not pipeline.completed:
                        return False
                    if failure == "error":
                        raise RuntimeError("milestone state unavailable")
                    if failure == "invalid":
                        return "complete"
                    pipeline.controller.is_running = False
                    return True

                report, events = self.run_boundary_case(pipeline, probe)
                self.assertFalse(report["success"])
                if failure == "error":
                    self.assertEqual(report["stop_reason"], "milestone_probe_failure")
                    self.assertIn("milestone state unavailable", events[-2]["error"])
                    self.assertIn(
                        "許可された操作候補がありません", events[-2]["preceding_error"]
                    )
                else:
                    self.assertEqual(
                        report["stop_reason"], "recognition_or_candidate_failure"
                    )

    def test_repeated_observation_stops_with_classified_reason(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation("sample", 100, 80, features={"signature": "same"})

        with tempfile.TemporaryDirectory() as directory:
            report = run_continuous_e2e(
                pipeline,
                observation,
                Path(directory) / "e2e.jsonl",
                loop_guard=LoopGuard(limit=3),
                max_steps=10,
                max_wall_seconds=5,
                settle_seconds=0,
            )

        self.assertFalse(report["success"])
        self.assertEqual(report["steps"], 3)
        self.assertEqual(report["stop_reason"], "repeated_observation")

    def test_duration_target_is_a_success_gate(self):
        pipeline = FakePipeline()

        def observation():
            return ScreenObservation(
                "sample", 100, 80, features={"signature": f"sig-{pipeline.steps}"}
            )

        with tempfile.TemporaryDirectory() as directory:
            report = run_continuous_e2e(
                pipeline,
                observation,
                Path(directory) / "e2e.jsonl",
                max_steps=100,
                max_wall_seconds=2,
                minimum_duration_seconds=0.01,
                settle_seconds=0,
                step_delay_seconds=0.01,
            )

        self.assertTrue(report["success"])
        self.assertEqual(report["stop_reason"], "duration_reached")
        self.assertGreaterEqual(report["steps"], 1)


if __name__ == "__main__":
    unittest.main()
