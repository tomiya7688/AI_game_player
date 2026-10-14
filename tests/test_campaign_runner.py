import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.campaign_runner import (
    CampaignActionResult,
    CampaignConfig,
    CampaignFailure,
    CampaignRunner,
    CampaignState,
    PR_CAMPAIGN_DURATION_SECONDS,
    RELEASE_CAMPAIGN_DURATION_SECONDS,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeEpisode:
    def __init__(self, states, *, failure=None):
        self.states = list(states)
        self.failure = failure
        self.closed = False
        self.action_count = 0
        self.outcomes = []

    def observe(self):
        if self.failure is not None:
            raise self.failure
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def act(self):
        self.action_count += 1
        return CampaignActionResult("move-up", True, "live", "fake live action")

    def record_outcome(self, status, confidence, evidence):
        self.outcomes.append((status, confidence, evidence))

    def close(self):
        self.closed = True


class CampaignRunnerTests(unittest.TestCase):
    def run_campaign(self, config, factory):
        temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        clock = FakeClock()
        runner = CampaignRunner(config, clock=clock, sleeper=clock.advance)
        report = runner.run(
            factory,
            root / "trace.jsonl",
            root / "report.json",
            metadata={"commit": "test-sha"},
        )
        trace = [
            json.loads(line)
            for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        return report, trace, root

    def test_profiles_use_the_issue_600_second_and_30_minute_targets(self):
        self.assertEqual(PR_CAMPAIGN_DURATION_SECONDS, 600)
        self.assertEqual(RELEASE_CAMPAIGN_DURATION_SECONDS, 1800)

    def test_configuration_rejects_non_numeric_and_boolean_time_values(self):
        for duration in (True, "600", float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                CampaignConfig("test", duration)
        with self.assertRaises(ValueError):
            CampaignConfig("test", 1, step_delay_seconds=True)

    def test_duration_profile_records_metrics_and_live_input(self):
        clock = FakeClock()
        episodes = []

        def create_episode(_episode_number):
            episode = FakeEpisode(
                [CampaignState("board-1", False, 10, 4), CampaignState("board-2", False, 20, 8)]
            )
            episodes.append(episode)
            return episode

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            runner = CampaignRunner(
                CampaignConfig("pr", 0.2, step_delay_seconds=0.1),
                clock=clock,
                sleeper=clock.advance,
            )
            report = runner.run(create_episode, root / "trace.jsonl", root / "report.json")
            records = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertTrue(report["success"])
        self.assertEqual(report["stop_reason"], "duration_reached")
        self.assertTrue(report["live_input_verified"])
        self.assertFalse(report["milestone_enabled"])
        self.assertFalse(report["milestone_reached"])
        self.assertEqual(report["actions_executed"], 2)
        self.assertEqual(report["last_score"], 20)
        self.assertEqual(report["peak_score"], 20)
        self.assertEqual(report["score_samples"], 3)
        self.assertEqual(report["max_tile"], 8)
        self.assertEqual(report["latency_ms"]["samples"], 2)
        self.assertTrue(episodes[0].closed)
        self.assertEqual(records[-1]["event"], "campaign_summary")
        self.assertTrue(all("frame" not in record for record in records))

    def test_milestone_probe_stops_after_the_matching_observation(self):
        episode = FakeEpisode(
            [CampaignState("board-before", False, 10, 4), CampaignState("board-target", False, 20, 8)]
        )
        clock = FakeClock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(
                CampaignConfig("test", 10, step_delay_seconds=0.1),
                clock=clock,
                sleeper=clock.advance,
            ).run(
                lambda _number: episode,
                root / "trace.jsonl",
                root / "report.json",
                milestone_probe=lambda state: state.max_tile == 8,
            )
            trace = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertTrue(report["success"])
        self.assertTrue(report["milestone_enabled"])
        self.assertTrue(report["milestone_reached"])
        self.assertEqual(report["stop_reason"], "milestone_reached")
        self.assertEqual(report["actions_executed"], 1)
        self.assertLess(report["elapsed_seconds"], report["duration_target_seconds"])
        self.assertEqual([record["event"] for record in trace].count("milestone_reached"), 1)
        self.assertTrue(episode.closed)

    def test_milestone_probe_rejects_non_boolean_results(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(CampaignConfig("test", 1)).run(
                lambda _number: FakeEpisode(
                    [CampaignState("board-before", False, 0, 2), CampaignState("board-after", False, 0, 4)]
                ),
                root / "trace.jsonl",
                root / "report.json",
                milestone_probe=lambda _state: 1,
            )

        self.assertFalse(report["success"])
        self.assertTrue(report["milestone_enabled"])
        self.assertFalse(report["milestone_reached"])
        self.assertEqual(report["failure_category"], "milestone_failure")

    def test_milestone_probe_exception_is_classified(self):
        def raise_during_probe(_state):
            raise RuntimeError("level milestone could not be evaluated")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(CampaignConfig("test", 1)).run(
                lambda _number: FakeEpisode(
                    [CampaignState("board-before", False, 0, 2), CampaignState("board-after", False, 0, 4)]
                ),
                root / "trace.jsonl",
                root / "report.json",
                milestone_probe=raise_during_probe,
            )

        self.assertFalse(report["success"])
        self.assertEqual(report["failure_category"], "milestone_failure")
        self.assertIn("level milestone could not be evaluated", report["failure_detail"])

    def test_terminal_screen_closes_and_restarts_the_episode(self):
        episodes = []

        def create_episode(_episode_number):
            state_sequence = (
                [
                    CampaignState("playing", False, 0, 2),
                    CampaignState("game-over", True, 128, 64),
                ]
                if not episodes
                else [CampaignState("new-game", False, 0, 2)]
            )
            episode = FakeEpisode(state_sequence)
            episodes.append(episode)
            return episode

        clock = FakeClock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(
                CampaignConfig("test", 0.2, terminal_restart_limit=1, step_delay_seconds=0.1),
                clock=clock,
                sleeper=clock.advance,
            ).run(create_episode, root / "trace.jsonl", root / "report.json")
            trace = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertTrue(report["success"])
        self.assertEqual(report["terminal_restart_count"], 1)
        self.assertEqual(report["episode_count"], 2)
        self.assertTrue(episodes[0].closed)
        self.assertTrue(episodes[1].closed)
        self.assertEqual([record["event"] for record in trace].count("episode_terminal"), 1)

    def test_stall_recovery_is_bounded_and_failure_is_reported(self):
        episodes = []

        def create_episode(_episode_number):
            episode = FakeEpisode([CampaignState("unchanged", False, 0, 2)])
            episodes.append(episode)
            return episode

        clock = FakeClock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(
                CampaignConfig(
                    "test",
                    10,
                    recovery_limit=1,
                    stall_action_limit=2,
                    step_delay_seconds=0,
                ),
                clock=clock,
                sleeper=clock.advance,
            ).run(create_episode, root / "trace.jsonl", root / "report.json")

        self.assertFalse(report["success"])
        self.assertEqual(report["stop_reason"], "stall_limit")
        self.assertEqual(report["recovery_count"], 1)
        self.assertEqual(report["episode_count"], 2)
        self.assertTrue(all(episode.closed for episode in episodes))

    def test_failure_classifier_preserves_hard_failure_category(self):
        def fail_to_start(_episode_number):
            raise CampaignFailure("recognition_failure", "the game board could not be recognized")

        report, _trace, _root = self.run_campaign(
            CampaignConfig("test", 1),
            fail_to_start,
        )
        self.assertFalse(report["success"])
        self.assertEqual(report["failure_category"], "recognition_failure")

    def test_terminal_restart_limit_stops_before_a_second_episode(self):
        episodes = []

        def create_episode(_episode_number):
            episode = FakeEpisode([CampaignState("terminal", True, 0, 2)])
            episodes.append(episode)
            return episode

        clock = FakeClock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(
                CampaignConfig("test", 1, terminal_restart_limit=0),
                clock=clock,
                sleeper=clock.advance,
            ).run(create_episode, root / "trace.jsonl", root / "report.json")

        self.assertEqual(report["stop_reason"], "terminal_restart_limit")
        self.assertEqual(report["episode_count"], 1)
        self.assertTrue(episodes[0].closed)

    def test_dry_run_does_not_satisfy_live_input_acceptance(self):
        class DryRunEpisode(FakeEpisode):
            def act(self):
                return CampaignActionResult("move-up", False, "dry_run", "input was not sent")

        clock = FakeClock()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report = CampaignRunner(
                CampaignConfig("test", 1),
                clock=clock,
                sleeper=clock.advance,
            ).run(
                lambda _number: DryRunEpisode([CampaignState("board", False, 0, 2)]),
                root / "trace.jsonl",
                root / "report.json",
            )

        self.assertFalse(report["success"])
        self.assertEqual(report["failure_category"], "live_input_not_verified")
        self.assertEqual(report["actions_executed"], 0)

    def test_existing_artifact_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            report_path.write_text("keep this report", encoding="utf-8")
            with self.assertRaises(CampaignFailure) as raised:
                CampaignRunner(CampaignConfig("test", 1)).run(
                    lambda _number: self.fail("existing report must be checked before episode start"),
                    root / "trace.jsonl",
                    report_path,
                )
            preserved_report = report_path.read_text(encoding="utf-8")

        self.assertEqual(raised.exception.category, "campaign_configuration")
        self.assertEqual(preserved_report, "keep this report")


if __name__ == "__main__":
    unittest.main()
