import unittest
from types import SimpleNamespace

from tools.observation_timing import ObservationTiming
from tools.run_2048_campaign import ReferenceGame2048Episode
from ai_game_player.campaign_runner import CampaignFailure


class ObservationTimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.timing = ObservationTiming(lambda: self.now)

    def test_accumulates_repeated_stages_without_renewing_any_lease(self):
        for _ in range(2):
            with self.timing.measure("capture"):
                self.now += 0.5
        with self.timing.measure("frame_analysis"):
            self.now += 9
        self.assertEqual(self.timing.snapshot(), {
            "elapsed_ms": 10000.0,
            "stage_ms": {"capture": 1000.0, "frame_analysis": 9000.0},
            "active_stage": None,
        })

    def test_records_exception_duration_and_propagates_original_error(self):
        error = RuntimeError("original analysis failure")
        with self.assertRaises(RuntimeError) as raised:
            with self.timing.measure("frame_analysis"):
                self.assertEqual("frame_analysis", self.timing.snapshot()["active_stage"])
                self.now = 6
                raise error
        self.assertIs(error, raised.exception)
        self.assertEqual(6000, self.timing.snapshot()["stage_ms"]["frame_analysis"])
        self.assertIsNone(self.timing.active_stage)

    def test_action_failure_keeps_stop_reason_and_slow_observation_stage(self):
        with self.timing.measure("board_adaptation"):
            self.now = 9
        episode = ReferenceGame2048Episode.__new__(ReferenceGame2048Episode)
        episode.source = SimpleNamespace(observation_timing=self.timing)

        def blocked(**_arguments):
            raise RuntimeError("FailSafeRuntime blocked action")

        episode.pipeline = SimpleNamespace(
            run_and_execute=blocked, last_safety_result=None,
            executor=SimpleNamespace(fail_safe_runtime=SimpleNamespace(
                state=SimpleNamespace(value="recovery_required"), reason="lease_expired"
            )),
        )
        with self.assertRaises(CampaignFailure) as raised:
            episode.act()
        self.assertEqual("safety_block", raised.exception.category)
        self.assertIn("reason=lease_expired", raised.exception.detail)
        self.assertIn('"board_adaptation": 9000.0', raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
