import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.safety_guard import SafetyEvent, SafetyLog


MEMORY_EVENT_LIMIT = 4096
OVERFLOW_EVENT_COUNT = 3


def safety_event(sequence: int) -> SafetyEvent:
    return SafetyEvent(
        timestamp=float(sequence),
        action_id=f"action-{sequence}",
        allowed=sequence % 2 == 0,
        code="allowed" if sequence % 2 == 0 else "blocked",
        reason="監査記録を保持します",
        state="active",
        target_pid=42,
    )


class SafetyLogRetentionTests(unittest.TestCase):
    def test_memory_only_log_retains_latest_events_in_order(self):
        log = SafetyLog()
        self.assertEqual(log.events(), [])
        for sequence in range(MEMORY_EVENT_LIMIT + OVERFLOW_EVENT_COUNT):
            log.append(safety_event(sequence))

        self.assertEqual(
            log.events(),
            [
                safety_event(sequence)
                for sequence in range(
                    OVERFLOW_EVENT_COUNT, MEMORY_EVENT_LIMIT + OVERFLOW_EVENT_COUNT
                )
            ],
        )
        snapshot = log.events()
        snapshot.clear()
        self.assertEqual(len(log.events()), MEMORY_EVENT_LIMIT)

    def test_file_audit_keeps_all_events_after_memory_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit" / "safety.jsonl"
            log = SafetyLog(path)
            expected = [
                safety_event(sequence)
                for sequence in range(MEMORY_EVENT_LIMIT + OVERFLOW_EVENT_COUNT)
            ]
            for event in expected:
                log.append(event)

            persisted = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(persisted, [event.to_dict() for event in expected])
            self.assertEqual(log.events(), expected[-MEMORY_EVENT_LIMIT:])


if __name__ == "__main__":
    unittest.main()
