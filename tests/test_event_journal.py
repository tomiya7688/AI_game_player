import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ai_game_player.event_journal import (
    EVENT_ENVELOPE_SCHEMA_VERSION,
    EventEnvelope,
    EventJournal,
    EventJournalClosedError,
    EventJournalCorruptionError,
)
from ai_game_player.experience import ArtifactReference


EVENTS_PER_SECOND = 1
THIRTY_MINUTE_EVENT_COUNT = 30 * 60 * EVENTS_PER_SECOND


def sample_artifact_reference() -> ArtifactReference:
    return ArtifactReference("frame.initial", "a" * 64, "image/png", True)


def load_stored_events(database_path: Path) -> list[EventEnvelope]:
    with closing(sqlite3.connect(database_path)) as connection:
        stored_rows = connection.execute(
            "SELECT envelope_json FROM events ORDER BY sequence"
        ).fetchall()
    return [EventEnvelope.from_dict(json.loads(row[0])) for row in stored_rows]


class EventEnvelopeTest(unittest.TestCase):
    def test_round_trips_version_time_correlation_payload_and_artifact_reference(self):
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30.123456Z",
            monotonic_ns=123456789,
            event_type="screen.observed",
            status="completed",
            frame_id="frame-1",
            turn_id="turn-1",
            snapshot_id="snapshot-1",
            correlation_id="correlation-1",
            payload={"dimensions": {"width": 640, "height": 480}},
            artifact_refs=(sample_artifact_reference(),),
        )

        self.assertEqual(event, EventEnvelope.from_dict(event.to_dict()))

    def test_rejects_unsupported_versions_invalid_timestamps_sequences_and_payloads(self):
        base_fields = {
            "schema_version": EVENT_ENVELOPE_SCHEMA_VERSION,
            "event_id": "event-1",
            "session_id": "session-1",
            "sequence": 1,
            "timestamp_utc": "2026-10-05T10:20:30Z",
            "monotonic_ns": 0,
            "event_type": "session.started",
        }
        invalid_fields = (
            {"schema_version": EVENT_ENVELOPE_SCHEMA_VERSION + 1},
            {"schema_version": float(EVENT_ENVELOPE_SCHEMA_VERSION)},
            {"sequence": True},
            {"sequence": 0},
            {"monotonic_ns": -1},
            {"timestamp_utc": "2026-10-05T10:20:30"},
            {"timestamp_utc": "2026-10-05T12:20:30+02:00"},
            {"timestamp_utc": "2026-10-05T10:20:30+00:00"},
            {"timestamp_utc": "2026-10-05 10:20:30Z"},
            {"timestamp_utc": "2026-W41-1T10:20:30Z"},
            {"payload": {"not-json": float("nan")}},
            {"payload": {1: "non-string key"}},
        )
        for invalid_field in invalid_fields:
            with self.subTest(invalid_field=invalid_field), self.assertRaises(ValueError):
                EventEnvelope(**(base_fields | invalid_field))

    def test_copies_nested_payload_values_at_envelope_creation(self):
        payload = {"nested": {"values": [1]}}
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30Z",
            monotonic_ns=0,
            event_type="session.started",
            payload=payload,
        )

        payload["nested"]["values"].append(2)

        self.assertEqual({"nested": {"values": [1]}}, event.payload)

    def test_rejects_missing_and_unknown_persisted_fields(self):
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30Z",
            monotonic_ns=0,
            event_type="session.started",
        )
        value = event.to_dict()

        with self.assertRaisesRegex(ValueError, "missing fields"):
            EventEnvelope.from_dict({key: field for key, field in value.items() if key != "status"})
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            EventEnvelope.from_dict(value | {"unexpected": True})


class EventJournalTest(unittest.TestCase):
    def test_enables_wal_after_rejecting_an_unrecognized_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE unrelated_records (record_id INTEGER PRIMARY KEY)")
                connection.commit()
                original_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

            with self.assertRaises(EventJournalCorruptionError):
                EventJournal(path)

            with closing(sqlite3.connect(path)) as connection:
                current_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(original_journal_mode, current_journal_mode)

    def test_rejects_unexpected_schema_objects_and_altered_table_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            triggered_path = Path(directory) / "unexpected-trigger.sqlite3"
            with EventJournal(triggered_path, session_id="session-1"):
                pass
            with closing(sqlite3.connect(triggered_path)) as connection:
                connection.execute(
                    "CREATE TRIGGER delete_inserted_event AFTER INSERT ON events "
                    "BEGIN DELETE FROM events WHERE sequence=NEW.sequence; END"
                )
                connection.commit()
            with self.assertRaisesRegex(EventJournalCorruptionError, "schema objects"):
                EventJournal(triggered_path)

            altered_path = Path(directory) / "altered-table.sqlite3"
            with EventJournal(altered_path, session_id="session-1"):
                pass
            with closing(sqlite3.connect(altered_path)) as connection:
                connection.execute("ALTER TABLE events ADD COLUMN unexpected TEXT")
                connection.commit()
            with self.assertRaisesRegex(EventJournalCorruptionError, "table definitions"):
                EventJournal(altered_path)

    def test_appends_compact_records_with_generated_utc_monotonic_and_correlation_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "session.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                event = journal.append(
                    "screen.observed",
                    status="completed",
                    frame_id="frame-1",
                    turn_id="turn-1",
                    snapshot_id="snapshot-1",
                    correlation_id="correlation-1",
                    payload={"width": 640},
                    artifact_refs=(sample_artifact_reference(),),
                )
                self.assertEqual("session-1", event.session_id)
                self.assertEqual(1, event.sequence)
                self.assertGreaterEqual(event.monotonic_ns, 0)
                self.assertTrue(event.timestamp_utc.endswith("Z"))
                self.assertEqual(1, journal.last_sequence)

            with closing(sqlite3.connect(path)) as connection:
                encoded_event = connection.execute(
                    "SELECT envelope_json FROM events WHERE sequence=1"
                ).fetchone()[0]
            self.assertNotIn(": ", encoded_event)
            restored_event = EventEnvelope.from_dict(json.loads(encoded_event))
            self.assertEqual("correlation-1", restored_event.correlation_id)
            self.assertTrue(restored_event.artifact_refs[0].sensitive)

    def test_reopens_existing_session_and_continues_sequence_without_rewriting_prior_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.sqlite3"
            with EventJournal(path, session_id="session-1") as first_journal:
                first_event = first_journal.append("session.started")
            original_record = load_stored_events(path)[0]

            with EventJournal(path) as resumed_journal:
                self.assertEqual("session-1", resumed_journal.session_id)
                second_event = resumed_journal.append("action.completed")

            stored_events = load_stored_events(path)
            self.assertEqual(1, first_event.sequence)
            self.assertEqual(2, second_event.sequence)
            self.assertEqual(original_record, stored_events[0])
            self.assertEqual([1, 2], [event.sequence for event in stored_events])

    def test_recovers_committed_events_and_discards_an_uncommitted_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                journal.append("session.started")

            uncommitted_event = EventEnvelope(
                schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
                event_id="uncommitted-event",
                session_id="session-1",
                sequence=2,
                timestamp_utc="2026-10-05T10:20:30Z",
                monotonic_ns=0,
                event_type="session.uncommitted",
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO events(sequence, event_id, envelope_json) VALUES (?, ?, ?)",
                    (uncommitted_event.sequence, uncommitted_event.event_id,
                     json.dumps(uncommitted_event.to_dict(), separators=(",", ":"))),
                )
            finally:
                connection.close()

            with EventJournal(path) as recovered_journal:
                self.assertEqual(1, recovered_journal.last_sequence)
                next_event = recovered_journal.append("session.resumed")

            self.assertEqual(2, next_event.sequence)
            self.assertEqual(["session.started", "session.resumed"], [
                event.event_type for event in load_stored_events(path)
            ])

    def test_fails_closed_for_corrupt_records_sequence_gaps_and_wrong_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                first_event = journal.append("session.started")
                journal.append("session.continued")

            with closing(sqlite3.connect(path)) as connection:
                connection.execute("UPDATE events SET envelope_json='{}' WHERE sequence=1")
                connection.commit()
            with self.assertRaises(EventJournalCorruptionError):
                EventJournal(path)

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (json.dumps(first_event.to_dict(), separators=(",", ":")),),
                )
                connection.execute("DELETE FROM events WHERE sequence=1")
                connection.commit()
            with self.assertRaisesRegex(EventJournalCorruptionError, "sequence"):
                EventJournal(path)

            with self.assertRaisesRegex(EventJournalCorruptionError, "does not match"):
                EventJournal(path, session_id="another-session")

    def test_serializes_concurrent_appends_from_one_writer_in_sequence_order(self):
        event_count = 100
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                with ThreadPoolExecutor(max_workers=8) as workers:
                    events = list(workers.map(
                        lambda event_index: journal.append(
                            "action.completed", payload={"index": event_index}
                        ),
                        range(event_count),
                    ))

            self.assertEqual(list(range(1, event_count + 1)), sorted(
                event.sequence for event in events
            ))
            self.assertEqual(event_count, len(load_stored_events(path)))

    def test_30_minute_equivalent_event_volume_remains_append_only_and_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long-session.sqlite3"
            with EventJournal(path, session_id="session-long") as journal:
                for event_index in range(THIRTY_MINUTE_EVENT_COUNT):
                    journal.append("session.tick", payload={"tick": event_index})

            self.assertEqual(THIRTY_MINUTE_EVENT_COUNT, len(load_stored_events(path)))
            with EventJournal(path) as recovered_journal:
                self.assertEqual(THIRTY_MINUTE_EVENT_COUNT, recovered_journal.last_sequence)

    def test_flush_persists_and_closed_writer_rejects_new_events(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EventJournal(Path(directory) / "session.sqlite3", session_id="session-1")
            journal.append("session.started")
            journal.flush()
            journal.close()
            journal.close()

            with self.assertRaises(EventJournalClosedError):
                journal.append("session.after_close")

    def test_interrupted_append_rolls_back_and_marks_writer_unhealthy(self):
        class InterruptOnCommit:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, *args, **kwargs):
                return self.connection.execute(*args, **kwargs)

            def commit(self):
                raise KeyboardInterrupt("append interrupted before commit")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.sqlite3"
            journal = EventJournal(path, session_id="session-1")
            interrupting_connection = InterruptOnCommit(journal._require_connection())
            with patch.object(
                journal,
                "_require_connection",
                return_value=interrupting_connection,
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "append interrupted"):
                    journal.append("session.interrupted")

            self.assertEqual(0, journal.last_sequence)
            journal.close()
            self.assertEqual([], load_stored_events(path))

    def test_context_exit_preserves_body_exception_when_checkpoint_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reader-blocked.sqlite3"
            reader = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(ValueError, "original body failure") as raised:
                    with EventJournal(path, session_id="session-1") as journal:
                        journal.append("session.started")
                        reader.execute("BEGIN")
                        reader.execute("SELECT * FROM events").fetchall()
                        journal._require_connection().execute("PRAGMA busy_timeout=1")
                        journal.append("session.continued")
                        raise ValueError("original body failure")
                self.assertTrue(any(
                    "cleanup also failed" in note for note in raised.exception.__notes__
                ))
            finally:
                reader.close()

class EventEnvelopeSchemaTest(unittest.TestCase):
    def test_schema_matches_supported_version_and_required_session_fields(self):
        schema_path = Path(__file__).parents[1] / "config" / "session_event.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual("object", schema["type"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(EVENT_ENVELOPE_SCHEMA_VERSION, schema["properties"]["schema_version"]["const"])
        self.assertEqual("Z$", schema["properties"]["timestamp_utc"]["pattern"])
        self.assertIn("sequence", schema["required"])
        self.assertIn("monotonic_ns", schema["required"])
        self.assertIn("correlation_id", schema["required"])
