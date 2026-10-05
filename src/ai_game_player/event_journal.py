from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_game_player.experience import ArtifactReference


EVENT_ENVELOPE_SCHEMA_VERSION = 1
SQLITE_JOURNAL_SCHEMA_VERSION = 1
SQLITE_JOURNAL_METADATA_ID = 1
FIRST_EVENT_SEQUENCE = 1
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_BUSY_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_MS / 1_000
_ENVELOPE_FIELDS = frozenset({
    "schema_version", "event_id", "session_id", "sequence", "timestamp_utc",
    "monotonic_ns", "event_type", "status", "frame_id", "turn_id",
    "snapshot_id", "correlation_id", "payload", "artifact_refs",
})


class EventJournalError(RuntimeError):
    """Base error for session event journal operations."""


class EventJournalCorruptionError(EventJournalError):
    """The journal contains a complete record that violates the v1 contract."""


class EventJournalClosedError(EventJournalError):
    """An operation was attempted after the journal was closed."""


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    timestamp_utc: str
    monotonic_ns: int
    event_type: str
    status: str | None = None
    frame_id: str | None = None
    turn_id: str | None = None
    snapshot_id: str | None = None
    correlation_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        schema_version = _require_integer(
            self.schema_version,
            "schema_version",
            EVENT_ENVELOPE_SCHEMA_VERSION,
        )
        if schema_version != EVENT_ENVELOPE_SCHEMA_VERSION:
            raise ValueError(f"unsupported event envelope schema version: {self.schema_version}")
        for field_name in ("event_id", "session_id", "event_type"):
            _require_nonempty_text(getattr(self, field_name), field_name)
        _require_integer(self.sequence, "sequence", FIRST_EVENT_SEQUENCE)
        _require_integer(self.monotonic_ns, "monotonic_ns", 0)
        _validate_utc_timestamp(self.timestamp_utc)
        for field_name in ("status", "frame_id", "turn_id", "snapshot_id", "correlation_id"):
            _require_optional_text(getattr(self, field_name), field_name)
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be an object")
        normalized_payload = _json_compatible_copy(self.payload, "payload", set())
        if not isinstance(self.artifact_refs, (tuple, list)):
            raise ValueError("artifact_refs must be an array")
        if any(not isinstance(reference, ArtifactReference) for reference in self.artifact_refs):
            raise ValueError("artifact_refs must contain ArtifactReference values")
        object.__setattr__(self, "payload", normalized_payload)
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp_utc": self.timestamp_utc,
            "monotonic_ns": self.monotonic_ns,
            "event_type": self.event_type,
            "status": self.status,
            "frame_id": self.frame_id,
            "turn_id": self.turn_id,
            "snapshot_id": self.snapshot_id,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
            "artifact_refs": [reference.to_dict() for reference in self.artifact_refs],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventEnvelope:
        if not isinstance(value, Mapping):
            raise ValueError("event envelope must be an object")
        if any(not isinstance(field_name, str) for field_name in value):
            raise ValueError("event envelope field names must be strings")
        missing_fields = _ENVELOPE_FIELDS - set(value)
        unknown_fields = set(value) - _ENVELOPE_FIELDS
        if missing_fields:
            raise ValueError(f"event envelope is missing fields: {sorted(missing_fields)}")
        if unknown_fields:
            raise ValueError(f"event envelope contains unknown fields: {sorted(unknown_fields)}")
        artifact_values = value["artifact_refs"]
        if not isinstance(artifact_values, list):
            raise ValueError("artifact_refs must be an array")
        return cls(
            schema_version=_require_integer(value["schema_version"], "schema_version", EVENT_ENVELOPE_SCHEMA_VERSION),
            event_id=_require_nonempty_text(value["event_id"], "event_id"),
            session_id=_require_nonempty_text(value["session_id"], "session_id"),
            sequence=_require_integer(value["sequence"], "sequence", FIRST_EVENT_SEQUENCE),
            timestamp_utc=_require_nonempty_text(value["timestamp_utc"], "timestamp_utc"),
            monotonic_ns=_require_integer(value["monotonic_ns"], "monotonic_ns", 0),
            event_type=_require_nonempty_text(value["event_type"], "event_type"),
            status=_require_optional_text(value["status"], "status"),
            frame_id=_require_optional_text(value["frame_id"], "frame_id"),
            turn_id=_require_optional_text(value["turn_id"], "turn_id"),
            snapshot_id=_require_optional_text(value["snapshot_id"], "snapshot_id"),
            correlation_id=_require_optional_text(value["correlation_id"], "correlation_id"),
            payload=_require_mapping(value["payload"], "payload"),
            artifact_refs=tuple(
                ArtifactReference.from_dict(_require_mapping(item, "artifact reference"))
                for item in artifact_values
            ),
        )


class EventJournal:
    """Single-session append-only SQLite WAL journal with atomic recovery.

    One EventJournal instance owns a session path. A single instance serializes
    concurrent callers; multiple writer instances must not share the same path.
    """

    def __init__(self, path: Path, *, session_id: str | None = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        self._write_failed = False
        self._event_ids: set[str] = set()
        self._session_id = _require_optional_text(session_id, "session_id")
        self._next_sequence = FIRST_EVENT_SEQUENCE
        self._connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path,
                timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                check_same_thread=False,
            )
            self._connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize_database()
            self._recover_existing_records()
            journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise EventJournalError("SQLite could not enable WAL mode for the event journal")
        except EventJournalError:
            self._discard_connection()
            raise
        except sqlite3.DatabaseError as error:
            self._discard_connection()
            if _is_sqlite_access_error(error):
                raise EventJournalError(f"cannot access event journal database: {self.path}") from error
            raise EventJournalCorruptionError(f"event journal database is invalid: {self.path}") from error
        except (IndexError, KeyError, RecursionError, TypeError, ValueError) as error:
            self._discard_connection()
            raise EventJournalCorruptionError(f"event journal metadata is invalid: {self.path}") from error
        except (OSError, sqlite3.Error) as error:
            self._discard_connection()
            raise EventJournalError(f"cannot open event journal database: {self.path}") from error

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise EventJournalError("event journal session metadata was not initialized")
        return self._session_id

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    def append(
        self,
        event_type: str,
        *,
        status: str | None = None,
        frame_id: str | None = None,
        turn_id: str | None = None,
        snapshot_id: str | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        artifact_refs: tuple[ArtifactReference, ...] = (),
    ) -> EventEnvelope:
        with self._lock:
            self._ensure_writable()
            event = EventEnvelope(
                schema_version=EVENT_ENVELOPE_SCHEMA_VERSION,
                event_id=uuid4().hex,
                session_id=self.session_id,
                sequence=self._next_sequence,
                timestamp_utc=_current_utc_timestamp(),
                monotonic_ns=time.monotonic_ns(),
                event_type=event_type,
                status=status,
                frame_id=frame_id,
                turn_id=turn_id,
                snapshot_id=snapshot_id,
                correlation_id=correlation_id,
                payload={} if payload is None else payload,
                artifact_refs=artifact_refs,
            )
            if event.event_id in self._event_ids:
                raise EventJournalError("duplicate event_id generated for journal append")
            encoded_record = _encode_json_record(event.to_dict())
            try:
                connection = self._require_connection()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO events(sequence, event_id, envelope_json) VALUES (?, ?, ?)",
                    (event.sequence, event.event_id, encoded_record),
                )
                connection.commit()
            except sqlite3.Error as error:
                self._write_failed = True
                self._rollback_open_transaction()
                raise EventJournalError("event journal transaction failed; reopen to recover") from error
            self._event_ids.add(event.event_id)
            self._next_sequence += 1
            return event

    def flush(self) -> None:
        """Flush buffered bytes and request durable storage for the session log."""
        with self._lock:
            self._ensure_writable()
            try:
                connection = self._require_connection()
                connection.commit()
                checkpoint = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            except sqlite3.Error as error:
                self._write_failed = True
                raise EventJournalError("event journal flush failed; reopen to recover") from error
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise EventJournalError("event journal WAL checkpoint is busy; committed events remain available")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            failure: sqlite3.Error | None = None
            connection = self._connection
            if connection is not None:
                try:
                    connection.commit()
                    checkpoint = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                    if checkpoint is not None and int(checkpoint[0]) != 0:
                        failure = sqlite3.OperationalError("WAL checkpoint is busy")
                except sqlite3.Error as error:
                    failure = error
                    self._write_failed = True
                try:
                    connection.close()
                except sqlite3.Error as error:
                    if failure is None:
                        failure = error
            self._connection = None
            self._closed = True
            if failure is not None:
                raise EventJournalError("event journal close could not checkpoint all data") from failure

    def __enter__(self) -> EventJournal:
        return self

    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> bool:
        self.close()
        return False

    def _ensure_writable(self) -> None:
        if self._closed:
            raise EventJournalClosedError("event journal is closed")
        if self._write_failed:
            raise EventJournalError("event journal is unhealthy; close and reopen for recovery")
        self._require_connection()

    def _initialize_database(self) -> None:
        connection = self._require_connection()
        database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if database_version not in (0, SQLITE_JOURNAL_SCHEMA_VERSION):
            raise EventJournalCorruptionError(
                f"unsupported SQLite journal schema version: {database_version}"
            )
        database_objects = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('table', 'view', 'trigger', 'index') AND name NOT LIKE 'sqlite_%'"
            )
        )
        existing_tables = {
            object_name for object_type, object_name in database_objects if object_type == "table"
        }
        required_tables = {"journal_metadata", "events"}
        if database_version == 0:
            if database_objects:
                raise EventJournalCorruptionError("unrecognized SQLite database at the event journal path")
            self._create_database_schema()
            return
        if not required_tables.issubset(existing_tables):
            raise EventJournalCorruptionError("SQLite event journal is missing required tables")
        metadata = connection.execute(
            "SELECT journal_schema_version, session_id FROM journal_metadata WHERE singleton_id=?",
            (SQLITE_JOURNAL_METADATA_ID,),
        ).fetchone()
        if metadata is None or int(metadata[0]) != SQLITE_JOURNAL_SCHEMA_VERSION:
            raise EventJournalCorruptionError("SQLite event journal metadata is invalid")
        stored_session_id = _require_nonempty_text(metadata[1], "session_id")
        if self._session_id is not None and self._session_id != stored_session_id:
            raise EventJournalCorruptionError("journal session_id does not match the requested session")
        self._session_id = stored_session_id

    def _create_database_schema(self) -> None:
        connection = self._require_connection()
        created_session_id = self._session_id or uuid4().hex
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE journal_metadata ("
                f"singleton_id INTEGER PRIMARY KEY CHECK(singleton_id={SQLITE_JOURNAL_METADATA_ID}), "
                "journal_schema_version INTEGER NOT NULL, "
                "session_id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE events ("
                "sequence INTEGER PRIMARY KEY, "
                "event_id TEXT NOT NULL UNIQUE, "
                "envelope_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO journal_metadata(singleton_id, journal_schema_version, session_id) "
                "VALUES (?, ?, ?)",
                (SQLITE_JOURNAL_METADATA_ID, SQLITE_JOURNAL_SCHEMA_VERSION, created_session_id),
            )
            connection.execute(f"PRAGMA user_version={SQLITE_JOURNAL_SCHEMA_VERSION}")
            connection.commit()
        except sqlite3.Error as error:
            self._rollback_open_transaction()
            raise EventJournalError("cannot create SQLite event journal schema") from error
        self._session_id = created_session_id

    def _recover_existing_records(self) -> None:
        connection = self._require_connection()
        expected_sequence = FIRST_EVENT_SEQUENCE
        journal_session_id = self.session_id
        for stored_sequence, stored_event_id, encoded_event in connection.execute(
            "SELECT sequence, event_id, envelope_json FROM events ORDER BY sequence"
        ):
            try:
                event = EventEnvelope.from_dict(_require_mapping(
                    json.loads(encoded_event),
                    "event envelope",
                ))
            except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
                raise EventJournalCorruptionError(
                    f"invalid complete event record at sequence {expected_sequence}"
                ) from error
            if event.sequence != expected_sequence or int(stored_sequence) != expected_sequence:
                raise EventJournalCorruptionError(
                    f"event sequence must be contiguous at {expected_sequence}"
                )
            if event.session_id != journal_session_id:
                raise EventJournalCorruptionError("event session_id does not match journal metadata")
            if event.event_id != stored_event_id or event.event_id in self._event_ids:
                raise EventJournalCorruptionError(
                    f"event_id does not match its index or is duplicated at sequence {expected_sequence}"
                )
            self._event_ids.add(event.event_id)
            expected_sequence += 1
        self._next_sequence = expected_sequence

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise EventJournalClosedError("event journal database is not open")
        return self._connection

    def _rollback_open_transaction(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.rollback()
        except sqlite3.Error:
            return

    def _discard_connection(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
        except sqlite3.Error:
            pass
        self._connection = None


def _encode_json_record(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _is_sqlite_access_error(error: sqlite3.DatabaseError) -> bool:
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "unable to open database file",
            "database is locked",
            "database is busy",
            "readonly database",
            "disk i/o error",
            "permission denied",
        )
    )


def _current_utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_utc_timestamp(value: str) -> None:
    _require_nonempty_text(value, "timestamp_utc")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp_utc must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("timestamp_utc must include the UTC timezone")


def _require_integer(value: Any, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer of at least {minimum}")
    return value


def _require_nonempty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _require_optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_text(value, field_name)


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _json_compatible_copy(value: Any, field_name: str, active_container_ids: set[int]) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain only finite numbers")
        return value
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ValueError(f"{field_name} must not contain circular references")
        active_container_ids.add(container_id)
        try:
            copied_mapping: dict[str, Any] = {}
            for key, nested_value in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} object keys must be strings")
                copied_mapping[key] = _json_compatible_copy(
                    nested_value,
                    f"{field_name}.{key}",
                    active_container_ids,
                )
        finally:
            active_container_ids.remove(container_id)
        return copied_mapping
    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ValueError(f"{field_name} must not contain circular references")
        active_container_ids.add(container_id)
        try:
            return [
                _json_compatible_copy(nested_value, f"{field_name}[{index}]", active_container_ids)
                for index, nested_value in enumerate(value)
            ]
        finally:
            active_container_ids.remove(container_id)
    raise ValueError(f"{field_name} contains a value that is not JSON serializable")
