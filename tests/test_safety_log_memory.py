import unittest

from ai_game_player.safety_guard import SafetyEvent, SafetyLog


class SafetyLogMemoryTests(unittest.TestCase):
    def test_in_memory_events_keep_only_the_configured_tail(self) -> None:
        log = SafetyLog(max_in_memory_events=3)

        for index in range(5):
            log.append(
                SafetyEvent(
                    timestamp=float(index),
                    action_id=str(index),
                    allowed=True,
                    code="allowed",
                    reason="test",
                    state="active",
                    target_pid=None,
                )
            )

        self.assertEqual(["2", "3", "4"], [event.action_id for event in log.events()])

    def test_non_positive_memory_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SafetyLog(max_in_memory_events=0)


if __name__ == "__main__":
    unittest.main()
