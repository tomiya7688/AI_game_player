import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_game_player.event_journal import EVENT_ENVELOPE_SCHEMA_VERSION, EventEnvelope
from tools import benchmark_event_journal


def benchmark_event(sequence: int, event_id: str, monotonic_ns: int) -> EventEnvelope:
    return EventEnvelope(
        schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
        event_id=event_id,
        session_id=benchmark_event_journal.BENCHMARK_SESSION_ID,
        sequence=sequence,
        timestamp_utc="2026-10-05T10:20:30Z",
        monotonic_ns=monotonic_ns,
        monotonic_epoch_id="benchmark-epoch",
        event_type="benchmark.sample",
    )


class BenchmarkEventJournalTest(unittest.TestCase):
    def test_jsonl_recovery_rejects_duplicate_event_ids(self):
        events = [
            benchmark_event(1, "duplicate-id", 10),
            benchmark_event(2, "duplicate-id", 11),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-event-ids.jsonl"
            with patch.object(
                benchmark_event_journal,
                "_create_benchmark_event",
                side_effect=events,
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid event"):
                    benchmark_event_journal._benchmark_jsonl_reference(
                        path,
                        event_count=2,
                        fsync_batch_size=1,
                        payload={},
                        monotonic_epoch_id="benchmark-epoch",
                    )

    def test_jsonl_recovery_rejects_decreasing_monotonic_time_within_epoch(self):
        events = [
            benchmark_event(1, "event-one", 10),
            benchmark_event(2, "event-two", 9),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decreasing-monotonic-time.jsonl"
            with patch.object(
                benchmark_event_journal,
                "_create_benchmark_event",
                side_effect=events,
            ):
                with self.assertRaisesRegex(RuntimeError, "decreasing monotonic time"):
                    benchmark_event_journal._benchmark_jsonl_reference(
                        path,
                        event_count=2,
                        fsync_batch_size=1,
                        payload={},
                        monotonic_epoch_id="benchmark-epoch",
                    )
