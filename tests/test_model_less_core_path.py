import shutil
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ai_game_player.baseline_provider import (
    ABSTAIN,
    OBSERVE_MORE,
    BaselineDecisionProvider,
    BaselineWithheld,
)
from ai_game_player.decision_verifier import ReliabilityStatus
from ai_game_player.observation_source import JsonObservationSource
from ai_game_player.pipeline import DecisionPipeline

FIXTURES = Path(__file__).parent / "fixtures" / "model_less"


class ModelLessCorePathTest(unittest.TestCase):
    """Issue #418 model-less CI profile.

    Runs Observation fixture -> Candidate generation (OCR) -> Baseline Decision
    -> Decision Verifier -> dry-run action, or a withheld decision, using the
    Baseline provider (which needs no learned model) and with the common network
    entry points blocked. This test does not inspect GPU state or model loaders.

    Fixture intent (Baseline score = utility + 0.3 * (recognition - 0.5), and the
    utility is equal for all candidates here): in menu.json, START (0.95) beats
    the OCR-only HELP candidate (0.7) by 0.3 * 0.25 = 0.075, above the 0.05
    margin, so the Baseline selects it. tie.json has two equal candidates, so the
    Baseline abstains. low_recognition.json has a single 0.55 candidate: allowed
    by the 0.5 ActionEvaluator threshold but below the Baseline 0.6 threshold, so
    the Baseline asks to observe more.
    """

    def setUp(self):
        # Record attempts instead of raising: production code may catch broad
        # exceptions, which would hide a raised AssertionError.
        self.network_attempts: list[str] = []

        def blocked(name):
            def _blocked(*args, **kwargs):
                self.network_attempts.append(name)
                raise OSError(f"network blocked in model-less test: {name}")

            return _blocked

        targets = [
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket.socket, "sendto"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
        ]
        guards = [patch.object(owner, name, blocked(name)) for owner, name in targets]
        guards += [
            patch("urllib.request.urlopen", blocked("urlopen")),
            patch("ai_game_player.provider.urlopen", blocked("provider.urlopen")),
        ]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self._assert_no_network_attempts)

    def _assert_no_network_attempts(self):
        self.assertEqual(self.network_attempts, [])

    def _pipeline(self, fixture: str) -> DecisionPipeline:
        directory = tempfile.mkdtemp()
        # Registered first so it runs last (cleanups are LIFO): close the
        # pipeline before deleting the directory that holds its records.
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        pipeline = DecisionPipeline(
            JsonObservationSource(FIXTURES / fixture),
            Path(directory),
            BaselineDecisionProvider(),
            dry_run=True,
            external_watchdog=False,
        )
        self.addCleanup(pipeline.close)
        return pipeline

    def test_network_guard_blocks_and_records_attempts(self):
        address = ("192.0.2.1", 80)
        calls = {
            "connect": lambda sock: sock.connect(address),
            "connect_ex": lambda sock: sock.connect_ex(address),
            "sendto": lambda sock: sock.sendto(b"x", address),
            "create_connection": lambda sock: socket.create_connection(
                address, timeout=0.1
            ),
            "getaddrinfo": lambda sock: socket.getaddrinfo("example.invalid", 80),
            "urlopen": lambda sock: urllib.request.urlopen("http://192.0.2.1/"),
        }
        for name, call in calls.items():
            with self.subTest(name), socket.socket() as sock:
                with self.assertRaises(OSError):
                    call(sock)
        self.assertEqual(self.network_attempts, list(calls))
        self.network_attempts.clear()

    def test_baseline_selects_verified_candidate_and_executes_in_dry_run(self):
        pipeline = self._pipeline("menu.json")
        result = pipeline.run_and_execute(
            [
                {
                    "text": "HELP",
                    "x": 150,
                    "y": 10,
                    "width": 30,
                    "height": 12,
                    "confidence": 0.7,
                }
            ],
            purpose="start the game",
        )
        self.assertEqual(result.mode, "dry_run")
        self.assertFalse(result.executed)
        self.assertEqual(result.action_id, "start")
        trace = pipeline.engine.trace.recent(1)[0]
        self.assertTrue(
            trace["decision"]["provider"].startswith("baseline_weighted_score/")
        )
        self.assertIn("R3_highest_weighted_score", trace["decision"]["reason"])
        self.assertEqual(
            pipeline.engine.last_reliability_result.status, ReliabilityStatus.TRUST
        )
        candidates = trace["context"]["candidates"]
        configured = {"start", "options"}
        generated = [c for c in candidates if c["action_id"] not in configured]
        self.assertEqual([c["label"] for c in generated], ["HELP"], candidates)
        self.assertEqual(
            [entry.action_id for entry in pipeline.load_execution_history()],
            ["start"],
        )

    def test_baseline_abstains_without_executing_on_a_tie(self):
        pipeline = self._pipeline("tie.json")
        with self.assertRaises(BaselineWithheld) as raised:
            pipeline.run_and_execute(purpose="choose a side")
        self.assertEqual(raised.exception.decision.kind, ABSTAIN)
        self.assertEqual(pipeline.load_execution_history(), [])

    def test_baseline_observes_more_without_executing_when_recognition_is_weak(self):
        pipeline = self._pipeline("low_recognition.json")
        with self.assertRaises(BaselineWithheld) as raised:
            pipeline.run_and_execute(purpose="start the game")
        self.assertEqual(raised.exception.decision.kind, OBSERVE_MORE)
        self.assertEqual(pipeline.load_execution_history(), [])


if __name__ == "__main__":
    unittest.main()
