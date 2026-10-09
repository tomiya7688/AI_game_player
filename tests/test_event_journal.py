import json
import re
import sqlite3
import sys
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
    EventJournalError,
    _is_sqlite_access_error,
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
            monotonic_epoch_id="epoch-1",
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
            "monotonic_epoch_id": "epoch-1",
            "event_type": "session.started",
        }
        invalid_fields = (
            {"schema_version": EVENT_ENVELOPE_SCHEMA_VERSION + 1},
            {"schema_version": EVENT_ENVELOPE_SCHEMA_VERSION + 0.5},
            {"sequence": True},
            {"sequence": 0},
            {"sequence": 9_223_372_036_854_775_808},
            {"monotonic_ns": -1},
            {"monotonic_ns": 9_223_372_036_854_775_808},
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
            monotonic_epoch_id="epoch-1",
            event_type="session.started",
            payload=payload,
        )

        payload["nested"]["values"].append(2)

        self.assertEqual({"nested": {"values": (1,)}}, event.payload)

    def test_payload_is_immutable_and_to_dict_returns_a_deep_independent_copy(self):
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30Z",
            monotonic_ns=0,
            monotonic_epoch_id="epoch-1",
            event_type="session.started",
            payload={"nested": {"values": [1]}},
        )

        with self.assertRaises(TypeError):
            event.payload["nested"]["new"] = True
        with self.assertRaises(AttributeError):
            event.payload["nested"]["values"].append(2)
        exported_payload = event.to_dict()["payload"]
        exported_payload["nested"]["values"].append(2)

        self.assertEqual((1,), event.payload["nested"]["values"])

    def test_normalizes_integer_valued_json_numbers_to_runtime_integers(self):
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30Z",
            monotonic_ns=0,
            monotonic_epoch_id="epoch-1",
            event_type="session.started",
        )
        value = event.to_dict()
        value["schema_version"] = 1.0
        value["sequence"] = 1.0
        value["monotonic_ns"] = 0.0

        restored_event = EventEnvelope.from_dict(value)

        self.assertIs(type(restored_event.schema_version), int)
        self.assertIs(type(restored_event.sequence), int)
        self.assertIs(type(restored_event.monotonic_ns), int)

    def test_rejects_missing_and_unknown_persisted_fields(self):
        event = EventEnvelope(
            schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
            event_id="event-1",
            session_id="session-1",
            sequence=1,
            timestamp_utc="2026-10-05T10:20:30Z",
            monotonic_ns=0,
            monotonic_epoch_id="epoch-1",
            event_type="session.started",
        )
        value = event.to_dict()

        with self.assertRaisesRegex(ValueError, "missing fields"):
            EventEnvelope.from_dict({key: field for key, field in value.items() if key != "status"})
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            EventEnvelope.from_dict(value | {"unexpected": True})


class EventJournalTest(unittest.TestCase):
    def test_sqlite_full_is_classified_as_a_recoverable_access_failure(self):
        disk_full_error = sqlite3.OperationalError("SQLite operation failed")
        disk_full_error.sqlite_errorcode = getattr(sqlite3, "SQLITE_FULL", 13)
        malformed_database_error = sqlite3.OperationalError("database is malformed")
        malformed_database_error.sqlite_errorcode = getattr(sqlite3, "SQLITE_CORRUPT", 11)

        self.assertTrue(_is_sqlite_access_error(disk_full_error))
        self.assertTrue(_is_sqlite_access_error(
            sqlite3.OperationalError("database or disk is full")
        ))
        self.assertTrue(_is_sqlite_access_error(
            sqlite3.OperationalError("database schema is locked: main")
        ))
        self.assertTrue(_is_sqlite_access_error(
            sqlite3.OperationalError("database table is locked")
        ))
        self.assertFalse(_is_sqlite_access_error(malformed_database_error))

    def test_invalid_path_cleanup_preserves_journal_error_for_embedded_nul(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid\0journal.sqlite3"

            with self.assertRaises(EventJournalError):
                EventJournal(path)

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

    def test_rejects_a_preexisting_empty_database_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.sqlite3"
            path.write_bytes(b"")

            with self.assertRaisesRegex(EventJournalCorruptionError, "unrecognized SQLite database"):
                EventJournal(path)

    def test_invalid_requested_session_id_uses_journal_error_without_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-session-id.sqlite3"
            preexisting_sidecars = {
                Path(f"{path}-journal"): b"existing rollback journal",
                Path(f"{path}-wal"): b"existing write-ahead log",
                Path(f"{path}-shm"): b"existing shared memory file",
            }
            for sidecar_path, content in preexisting_sidecars.items():
                sidecar_path.write_bytes(content)

            with self.assertRaisesRegex(EventJournalError, "requested session_id"):
                EventJournal(path, session_id=" ")

            self.assertFalse(path.exists())
            for sidecar_path, content in preexisting_sidecars.items():
                with self.subTest(sidecar=sidecar_path.suffix):
                    self.assertEqual(content, sidecar_path.read_bytes())

    def test_recovery_rejects_decreasing_monotonic_time_within_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decreasing-monotonic-time.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                first_event = journal.append("session.started")
                second_event = journal.append("session.continued")

            first_record = first_event.to_dict()
            second_record = second_event.to_dict()
            first_record["monotonic_ns"] = 10
            second_record["monotonic_ns"] = 9
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (json.dumps(first_record, separators=(",", ":")),),
                )
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=2",
                    (json.dumps(second_record, separators=(",", ":")),),
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "monotonic_ns decreases"):
                EventJournal(path)

    def test_recovery_preserves_large_integer_from_decimal_json_token(self):
        large_monotonic_ns = 9_007_199_254_740_993
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-decimal-monotonic-time.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                first_event = journal.append("session.started")
                second_event = journal.append("session.continued")

            first_record = first_event.to_dict()
            first_record["monotonic_ns"] = large_monotonic_ns
            second_record = second_event.to_dict()
            second_record["monotonic_ns"] = large_monotonic_ns
            encoded_first_record = json.dumps(first_record, separators=(",", ":"))
            encoded_second_record = json.dumps(second_record, separators=(",", ":")).replace(
                f'"monotonic_ns":{large_monotonic_ns}',
                f'"monotonic_ns":{large_monotonic_ns}.0',
                1,
            )

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (encoded_first_record,),
                )
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=2",
                    (encoded_second_record,),
                )
                connection.commit()

            with EventJournal(path) as recovered_journal:
                self.assertEqual(2, recovered_journal.last_sequence)
                next_event = recovered_journal.append("session.resumed")

            self.assertEqual(3, next_event.sequence)

    def test_recovery_rejects_decimal_exponent_above_sqlite_integer_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized-decimal-monotonic-time.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                journal.append("session.started")

            with closing(sqlite3.connect(path)) as connection:
                envelope_json = connection.execute(
                    "SELECT envelope_json FROM events WHERE sequence=1"
                ).fetchone()[0]
                oversized_envelope_json = re.sub(
                    r'("monotonic_ns":)[^,}]+',
                    r'\g<1>1e1000000000',
                    envelope_json,
                    count=1,
                )
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (oversized_envelope_json,),
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "invalid complete event record"):
                EventJournal(path)

    def test_recovery_wraps_decimal_parser_exponent_errors_as_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-decimal-exponent.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                journal.append("session.started")

            with closing(sqlite3.connect(path)) as connection:
                envelope_json = connection.execute(
                    "SELECT envelope_json FROM events WHERE sequence=1"
                ).fetchone()[0]
                invalid_exponent_json = re.sub(
                    r'("monotonic_ns":)[^,}]+',
                    r'\g<1>1e999999999999999999999999999999999999',
                    envelope_json,
                    count=1,
                )
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (invalid_exponent_json,),
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "invalid complete event record"):
                EventJournal(path)

    def test_initializes_database_through_preexisting_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "journal-link.sqlite3"
            database_target = Path(directory) / "journal-target.sqlite3"
            try:
                database_path.symlink_to(database_target.name)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"the current platform cannot create a file symlink: {error}")

            with EventJournal(database_path, session_id="session-1") as journal:
                event = journal.append("session.started")

            self.assertTrue(database_path.is_symlink())
            self.assertTrue(database_target.is_file())
            self.assertEqual("session-1", load_stored_events(database_path)[0].session_id)
            self.assertEqual(1, event.sequence)

    def test_failed_first_open_removes_partial_database_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retryable.sqlite3"
            with patch.object(
                EventJournal,
                "_create_database_schema",
                side_effect=EventJournalError("disk full"),
            ):
                with self.assertRaisesRegex(EventJournalError, "disk full"):
                    EventJournal(path, session_id="session-1")

            self.assertFalse(path.exists())
            with EventJournal(path, session_id="session-1") as journal:
                event = journal.append("session.started")
            self.assertEqual(1, event.sequence)

            interrupted_path = Path(directory) / "interrupted.sqlite3"
            with patch.object(
                EventJournal,
                "_create_database_schema",
                side_effect=KeyboardInterrupt("initialization interrupted"),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "initialization interrupted"):
                    EventJournal(interrupted_path, session_id="session-1")
            self.assertFalse(interrupted_path.exists())

    def test_failed_first_open_preserves_preexisting_dangling_symlink(self):
        path = Path("dangling-link.sqlite3")
        with (
            patch(
                "ai_game_player.event_journal.os.path.lexists",
                side_effect=lambda candidate: Path(candidate) == path,
            ),
            patch.object(
                sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ),
            patch.object(
                Path,
                "is_symlink",
                autospec=True,
                side_effect=lambda candidate: Path(candidate) == path,
            ),
            patch.object(EventJournal, "_remove_incomplete_new_database") as remove_database,
        ):
            with self.assertRaises(EventJournalError):
                EventJournal(path, session_id="session-1")

        remove_database.assert_called_once_with(
            frozenset({path}),
            cleanup_dangling_symlink_target=True,
        )

    def test_failed_first_open_removes_target_without_removing_symlink_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal-link.sqlite3"
            target = Path(directory) / "journal-target.sqlite3"
            path.write_text("pre-existing symlink entry placeholder", encoding="utf-8")
            target.write_bytes(b"new incomplete database")
            journal = EventJournal.__new__(EventJournal)
            journal.path = path

            with patch.object(
                Path,
                "resolve",
                autospec=True,
                side_effect=lambda candidate, strict=False: (
                    target if Path(candidate) == path else Path(candidate).absolute()
                ),
            ):
                journal._remove_incomplete_new_database(
                    frozenset({path}),
                    cleanup_dangling_symlink_target=True,
                )

            self.assertTrue(path.is_file())
            self.assertFalse(target.exists())

    def test_path_probe_permission_error_uses_journal_error_contract(self):
        path = Path("inaccessible-directory") / "journal.sqlite3"
        with patch.object(Path, "exists", autospec=True, side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(EventJournalError, "cannot open event journal database"):
                EventJournal(path, session_id="session-1")

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

    def test_rejects_non_integer_existing_metadata_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-schema-version.sqlite3"
            with EventJournal(path, session_id="session-1"):
                pass
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE journal_metadata SET journal_schema_version=1.5 WHERE singleton_id=1"
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "metadata is invalid"):
                EventJournal(path)

    def test_rejects_additional_metadata_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-metadata.sqlite3"
            with EventJournal(path, session_id="session-1"):
                pass
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute(
                    "INSERT INTO journal_metadata(singleton_id, journal_schema_version, session_id) "
                    "VALUES (2, 1, 'conflicting-session')"
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "metadata is invalid"):
                EventJournal(path)

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

    def test_invalid_append_data_uses_journal_error_without_poisoning_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-append.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                with self.assertRaises(EventJournalError):
                    journal.append(" ")
                with self.assertRaises(EventJournalError):
                    journal.append("event.validated", payload={"value": float("nan")})
                deeply_nested_payload: dict[str, object] = {}
                for _ in range(sys.getrecursionlimit() + 20):
                    deeply_nested_payload = {"nested": deeply_nested_payload}
                with self.assertRaises(EventJournalError):
                    journal.append("event.validated", payload=deeply_nested_payload)
                with patch.object(EventEnvelope, "to_dict", side_effect=RecursionError("serialization depth")):
                    with self.assertRaises(EventJournalError):
                        journal.append("event.validated")

                valid_event = journal.append("event.validated")

            self.assertEqual(1, valid_event.sequence)

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
            self.assertNotEqual(first_event.monotonic_epoch_id, second_event.monotonic_epoch_id)
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
                monotonic_epoch_id="epoch-1",
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

    def test_rejects_duplicate_json_members_during_record_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-json-member.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                event = journal.append("session.started")
            encoded_event = json.dumps(event.to_dict(), separators=(",", ":"))
            duplicate_session_id = encoded_event[:-1] + ',"session_id":"session-1"}'

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (duplicate_session_id,),
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "invalid complete event record"):
                EventJournal(path)

    def test_rejects_blob_envelope_storage_during_record_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob-envelope.sqlite3"
            with EventJournal(path, session_id="session-1") as journal:
                journal.append("session.started")

            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET envelope_json=? WHERE sequence=1",
                    (b'{"schema_version":1}',),
                )
                connection.commit()

            with self.assertRaisesRegex(EventJournalCorruptionError, "not stored as SQLite TEXT"):
                EventJournal(path)

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
            with EventJournal(path, session_id="session-1") as initial_journal:
                initial_journal.append("session.started")
            reader = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(ValueError, "original body failure"):
                    with EventJournal(path, session_id="session-1") as journal:
                        reader.execute("BEGIN")
                        reader.execute("SELECT * FROM events").fetchall()
                        journal._require_connection().execute("PRAGMA busy_timeout=1")
                        journal.append("session.continued")
                        raise ValueError("original body failure")
                self.assertTrue(journal._closed)
            finally:
                reader.close()

class EventEnvelopeSchemaTest(unittest.TestCase):
    def test_schema_matches_supported_version_and_required_session_fields(self):
        schema_path = Path(__file__).parents[1] / "config" / "session_event.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema",
            schema["$schema"],
        )
        self.assertEqual("object", schema["type"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(EVENT_ENVELOPE_SCHEMA_VERSION, schema["properties"]["schema_version"]["const"])
        self.assertEqual(9_223_372_036_854_775_807, schema["properties"]["sequence"]["maximum"])
        self.assertEqual(9_223_372_036_854_775_807, schema["properties"]["monotonic_ns"]["maximum"])
        timestamp_pattern = schema["properties"]["timestamp_utc"]["pattern"]
        self.assertEqual("date-time", schema["properties"]["timestamp_utc"]["format"])
        self.assertIsNotNone(re.fullmatch(timestamp_pattern, "2026-10-05T10:20:30.123456Z"))
        self.assertIsNotNone(re.fullmatch(timestamp_pattern, "2024-02-29T23:59:59Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "not-a-dateZ"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "2026-10-05T25:20:30Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "2026-02-29T10:20:30Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "0000-02-29T10:20:30Z"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "2026-10-05T10:20:30Z\n"))
        self.assertIsNone(re.fullmatch(timestamp_pattern, "2026-10-05T10:20:30Z\r\n"))
        artifact_properties = schema["properties"]["artifact_refs"]["items"]["properties"]
        valid_artifact_fields = {
            "artifact_id": "frame.initial",
            "sha256": "0" * 64,
            "media_type": "image/png",
        }
        for field_name, valid_value in valid_artifact_fields.items():
            with self.subTest(artifact_field=field_name):
                field_pattern = artifact_properties[field_name]["pattern"]
                self.assertIsNotNone(re.fullmatch(field_pattern, valid_value))
                self.assertIsNone(re.fullmatch(field_pattern, valid_value + "\n"))
        python_whitespace_characters = "".join(
            chr(codepoint)
            for codepoint in (
                *range(0x0009, 0x000E),
                *range(0x001C, 0x0021),
                0x0085,
                0x00A0,
                0x1680,
                *range(0x2000, 0x200B),
                0x2028,
                0x2029,
                0x202F,
                0x205F,
                0x3000,
            )
        )
        self.assertTrue(all(character.isspace() for character in python_whitespace_characters))
        for field_name in (
            "event_id", "session_id", "event_type", "status", "frame_id",
            "turn_id", "snapshot_id", "correlation_id", "monotonic_epoch_id",
        ):
            with self.subTest(field_name=field_name):
                pattern = schema["properties"][field_name]["pattern"]
                for whitespace_character in python_whitespace_characters:
                    with self.subTest(field_name=field_name, whitespace=ord(whitespace_character)):
                        self.assertIsNone(re.search(pattern, whitespace_character))
                self.assertIsNotNone(re.search(pattern, " x "))
                self.assertIsNotNone(re.search(pattern, "\ufeff"))
        media_type_pattern = artifact_properties["media_type"]["pattern"]
        self.assertIsNotNone(re.fullmatch(media_type_pattern, "image/png"))
        for whitespace_character in python_whitespace_characters:
            with self.subTest(media_type_whitespace=ord(whitespace_character)):
                self.assertIsNone(re.fullmatch(media_type_pattern, f"image/{whitespace_character}png"))
        self.assertIn("sequence", schema["required"])
        self.assertIn("monotonic_ns", schema["required"])
        self.assertIn("monotonic_epoch_id", schema["required"])
        self.assertIn("correlation_id", schema["required"])
