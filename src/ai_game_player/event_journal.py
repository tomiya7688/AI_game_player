from __future__ import annotations

import json
import math
import re
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
# {
#   責務: [_UTC_TIMESTAMP_PATTERN: v1 EventEnvelopeで受け付けるUTC timestampの文字列表現を制限する]
#   処理: [秒までの年月日時分秒と任意小数部の後に大文字Zがある文字列だけを一致させる]
# }
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)
# {
#   責務: [_JOURNAL_METADATA_TABLE_SQL: Journalの単一Session metadataを制約付きで保存するtable定義]
#   処理: [singleton_idとschema versionとsession IDのcolumn、およびsingleton IDのCHECK制約を定義する]
# }
_JOURNAL_METADATA_TABLE_SQL = (
    f"CREATE TABLE journal_metadata ("
    f"singleton_id INTEGER PRIMARY KEY CHECK(singleton_id={SQLITE_JOURNAL_METADATA_ID}), "
    "journal_schema_version INTEGER NOT NULL, "
    "session_id TEXT NOT NULL)"
)
# {
#   責務: [_EVENTS_TABLE_SQL: Journal eventを連番順に保持するtable定義]
#   処理: [sequenceをprimary key、event_idをunique、JSON envelopeを必須columnとして定義する]
# }
_EVENTS_TABLE_SQL = (
    "CREATE TABLE events ("
    "sequence INTEGER PRIMARY KEY, "
    "event_id TEXT NOT NULL UNIQUE, "
    "envelope_json TEXT NOT NULL)"
)


# {
#   責務: [EventJournalError: SQLite Event Journalの操作が完了しなかったことを呼び出し元へ伝える]
# }
class EventJournalError(RuntimeError):
    """Base error for session event journal operations."""


# {
#   責務: [EventJournalCorruptionError: 保存済みJournalが定義済みschemaまたはevent contractに違反したことを示す]
# }
class EventJournalCorruptionError(EventJournalError):
    """The journal contains a complete record that violates the v1 contract."""


# {
#   責務: [EventJournalClosedError: close後または接続消失後のJournal操作を拒否したことを示す]
# }
class EventJournalClosedError(EventJournalError):
    """An operation was attempted after the journal was closed."""


@dataclass(frozen=True)
# {
#   責務: [EventEnvelope: 1 Session内の事実をschema version・連番・時刻・関連ID・payload・artifact参照と一緒に表す]
#   フィールド: [session_id/sequence: Session識別子と1から始まる連続番号, timestamp_utc/monotonic_ns: UTC表示時刻と経過時間比較用時刻, payload/artifact_refs: 小さなJSON値と別保存artifactへの参照]
# }
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

    # {
    #   責務: [__post_init__: Envelopeを永続化する前にschema・識別子・時刻・JSON payload・artifact参照を検証する]
    #   処理: [integer-valued numberをintへ正規化し、UTC・文字列・payload・artifact refsを検証して保持する]
    #   引数: [self: 作成直後のSession event envelope]
    #   戻り値: [なし: 検証成功時は正規化したfieldを保持する]
    #   エラー: [ValueError: version・必須値・UTC時刻・JSON payload・artifact参照がcontractに合わない場合]
    # }
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
        sequence = _require_integer(self.sequence, "sequence", FIRST_EVENT_SEQUENCE)
        monotonic_ns = _require_integer(self.monotonic_ns, "monotonic_ns", 0)
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
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "monotonic_ns", monotonic_ns)

    # {
    #   責務: [to_dict: EventEnvelopeをJSONへ保存できるfield名と値の辞書に変換する]
    #   処理: [payloadを新しい辞書にし、各ArtifactReferenceをJSON objectへ変換してevent fieldとまとめる]
    #   引数: [self: JSONへ保存する検証済みevent]
    #   戻り値: [dict[str, Any]: schema fieldをすべて含みartifact_refsを辞書配列にした保存用record]
    # }
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
    # {
    #   責務: [from_dict: JSON由来の辞書からcontract検証済みEventEnvelopeを復元する]
    #   処理: [必須field・未知field・artifact配列を確認し、各値とArtifactReferenceを検証してEnvelopeを構築する]
    #   引数: [cls: 生成するEnvelope型, value: JSON parser等が返したevent object]
    #   戻り値: [EventEnvelope: schema version 1に適合する復元済みevent]
    #   エラー: [ValueError: objectのshapeまたはfield値がevent contractに違反する場合]
    # }
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


# {
#   責務: [EventJournal: 1 SessionのeventをSQLite WALへ連続番号付きで追記し、再open時に既存recordを検証する]
#   フィールド: [_connection: Session databaseとのSQLite接続, _session_id/_next_sequence: 保存対象Sessionと次に割り当てる番号, _event_ids: 重複を拒否する既存event ID集合, _lock/_closed/_write_failed: 同一instanceの排他と利用可能状態]
# }
class EventJournal:
    """Single-session append-only SQLite WAL journal with atomic recovery.

    One EventJournal instance owns a session path. A single instance serializes
    concurrent callers; multiple writer instances must not share the same path.
    """

    # {
    #   責務: [__init__: 1 Session専用のSQLite Journalを開き、schema・既存event・WAL modeを検証する]
    #   処理: [SQLite接続をFULL synchronousで構成し、database metadataからSession IDを確定してevent連番を復旧する]
    #   引数: [path: Session event databaseの保存先, session_id: 新規databaseに指定するSession ID、既存databaseを開く場合の照合値]
    #   戻り値: [なし: 検証済みSQLite接続と次のevent sequenceをinstanceに保持する]
    #   エラー: [EventJournalError: databaseへ接続できないかWALを開始できない場合, EventJournalCorruptionError: schema・metadata・保存eventが不正な場合]
    # }
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
    # {
    #   責務: [session_id: Journalが記録するSessionの識別子を返す]
    #   処理: [database metadataから初期化したSession IDを読み出す]
    #   引数: [self: Session Journal]
    #   戻り値: [str: 全event envelopeに使うSession ID]
    #   エラー: [EventJournalError: Session IDが初期化されていない場合]
    # }
    def session_id(self) -> str:
        if self._session_id is None:
            raise EventJournalError("event journal session metadata was not initialized")
        return self._session_id

    @property
    # {
    #   責務: [last_sequence: 最後にcommit済みのevent sequenceを返す]
    #   処理: [次のsequenceから1を引き、event未記録なら0を返す]
    #   引数: [self: Session Journal]
    #   戻り値: [int: commit済みeventの最後の連番。未記録時は0]
    # }
    def last_sequence(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    # {
    #   責務: [append: Session eventにID・UTC/monotonic時刻・連番を割り当て、SQLite transactionで1 recordを永続化する]
    #   処理: [event fieldを検証し、BEGIN IMMEDIATEからINSERTとcommitを行う。失敗した接続は不健全として以後の書込みを拒否する]
    #   引数: [event_type: eventの種類, status/frame_id/turn_id/snapshot_id/correlation_id: 必要に応じて関連付ける状態とID, payload: 小さなJSON data object, artifact_refs: 大きなdataを別保存した場合の参照]
    #   戻り値: [EventEnvelope: SQLiteにcommitしたsequence付きevent]
    #   エラー: [EventJournalClosedError: Journalがclose済みの場合, EventJournalError: 入力contract違反またはtransaction失敗の場合]
    # }
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
            except BaseException as error:
                self._write_failed = True
                self._rollback_open_transaction()
                if isinstance(error, sqlite3.Error):
                    raise EventJournalError(
                        "event journal transaction failed; reopen to recover"
                    ) from error
                raise
            self._event_ids.add(event.event_id)
            self._next_sequence += 1
            return event

    # {
    #   責務: [flush: commit済みeventを含むWALをfull checkpointしてdatabase本体へ反映する]
    #   処理: [未完transactionをcommitし、busy timeout内にcheckpointが完了したか検証する]
    #   引数: [self: flush対象のSession Journal]
    #   戻り値: [なし: checkpoint完了時に戻る]
    #   エラー: [EventJournalError: Journalが利用不可、SQLite flush失敗、またはcheckpointがbusyの場合]
    # }
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

    # {
    #   責務: [close: Session Journalの接続を閉じ、終了前にWAL checkpointを試みる]
    #   処理: [commit・checkpointを試し、checkpoint失敗も記録して接続を必ず閉じる。再度のcloseは何もしない]
    #   引数: [self: 解放するSession Journal]
    #   戻り値: [なし: 接続を切り離してclosed状態にする]
    #   エラー: [EventJournalError: 接続を閉じてもcheckpointまたはcloseの失敗が残る場合]
    # }
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

    # {
    #   責務: [__enter__: with blockで利用するopen済みSession Journalを返す]
    #   処理: [Journal接続やSession状態を変えずにselfを返す]
    #   引数: [self: with blockへ渡すJournal]
    #   戻り値: [EventJournal: context managerとして利用する同じJournal]
    # }
    def __enter__(self) -> EventJournal:
        return self

    # {
    #   責務: [__exit__: with block終了時にJournalをcloseし、block内の例外を外へ伝える]
    #   処理: [成功・例外のどちらでもcloseを実行し、block内例外があればclose失敗で元の例外を置き換えない]
    #   引数: [self: 解放するSession Journal, exception_type/exception/traceback: block内で発生した例外情報]
    #   戻り値: [bool: 常にFalseを返しblock内例外の伝播を許可する]
    # }
    def __exit__(self, exception_type: Any, exception: Any, traceback: Any) -> bool:
        try:
            self.close()
        except Exception:
            if exception is None:
                raise
        return False

    # {
    #   責務: [_ensure_writable: append/flush前にJournalが書込み可能か確認する]
    #   処理: [closed・write failure・connection lossを順に検査し、不健全なconnectionを使わせない]
    #   引数: [self: 書込み要求を受けたSession Journal]
    #   戻り値: [なし: 書込み可能なら正常終了する]
    #   エラー: [EventJournalClosedError: Journalやconnectionがclose済みの場合, EventJournalError: 前回のwrite failureが残る場合]
    # }
    def _ensure_writable(self) -> None:
        if self._closed:
            raise EventJournalClosedError("event journal is closed")
        if self._write_failed:
            raise EventJournalError("event journal is unhealthy; close and reopen for recovery")
        self._require_connection()

    # {
    #   責務: [_initialize_database: SQLite databaseのschema versionとSession metadataを読み、新規作成または再利用を決める]
    #   処理: [未知のobjectを持つversion 0 databaseを拒否し、version 1では正確なtable定義・唯一のmetadata row・整数schema version・requested session_idを照合する]
    #   引数: [self: 初期化中のSession Journal]
    #   戻り値: [なし: 新規schemaを作成するか、既存Session IDをinstanceへ設定する]
    #   エラー: [EventJournalCorruptionError: schema version・table・metadata・requested Session IDが合わない場合]
    # }
    def _initialize_database(self) -> None:
        connection = self._require_connection()
        database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if database_version not in (0, SQLITE_JOURNAL_SCHEMA_VERSION):
            raise EventJournalCorruptionError(
                f"unsupported SQLite journal schema version: {database_version}"
            )
        database_objects = tuple(
            (str(row[0]), str(row[1]), None if row[2] is None else str(row[2]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'view', 'trigger', 'index') AND name NOT LIKE 'sqlite_%'"
            )
        )
        if database_version == 0:
            if database_objects:
                raise EventJournalCorruptionError("unrecognized SQLite database at the event journal path")
            self._create_database_schema()
            return
        expected_schema_objects = {
            ("table", "journal_metadata"): _JOURNAL_METADATA_TABLE_SQL,
            ("table", "events"): _EVENTS_TABLE_SQL,
        }
        actual_schema_objects = {
            (object_type, object_name): sql
            for object_type, object_name, sql in database_objects
        }
        if actual_schema_objects != expected_schema_objects:
            raise EventJournalCorruptionError(
                "SQLite event journal schema objects or table definitions are unexpected"
            )
        metadata_rows = connection.execute(
            "SELECT singleton_id, journal_schema_version, session_id FROM journal_metadata"
        ).fetchall()
        if (
            len(metadata_rows) != 1
            or metadata_rows[0][0] != SQLITE_JOURNAL_METADATA_ID
            or not isinstance(metadata_rows[0][1], int)
            or metadata_rows[0][1] != SQLITE_JOURNAL_SCHEMA_VERSION
        ):
            raise EventJournalCorruptionError("SQLite event journal metadata is invalid")
        stored_session_id = _require_nonempty_text(metadata_rows[0][2], "session_id")
        if self._session_id is not None and self._session_id != stored_session_id:
            raise EventJournalCorruptionError("journal session_id does not match the requested session")
        self._session_id = stored_session_id

    # {
    #   責務: [_create_database_schema: 空のSQLite databaseへversion 1 metadataとevent tableをtransactionで作成する]
    #   処理: [一意なSession IDと連続sequenceを保存するtableを作り、全schema作成を1 transactionでcommitする]
    #   引数: [self: schemaを初期化するJournal]
    #   戻り値: [なし: 作成したSession IDをinstanceへ保存する]
    #   エラー: [EventJournalError: schema作成transactionが失敗した場合]
    # }
    def _create_database_schema(self) -> None:
        connection = self._require_connection()
        created_session_id = self._session_id or uuid4().hex
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_JOURNAL_METADATA_TABLE_SQL)
            connection.execute(_EVENTS_TABLE_SQL)
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

    # {
    #   責務: [_recover_existing_records: 保存済みeventを全件検証して連番・Session・一意IDの復旧状態を作る]
    #   処理: [sequence順にJSON recordをdecodeしEnvelope contractとindexを照合して、次に使うsequenceを決める]
    #   引数: [self: 保存済みrecordsを検査するJournal]
    #   戻り値: [なし: event ID集合とnext sequenceをinstanceへ保存する]
    #   エラー: [EventJournalCorruptionError: JSON・Envelope・sequence・session ID・event IDのいずれかが不正な場合]
    # }
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

    # {
    #   責務: [_require_connection: SQLiteへ操作を渡す前にopen connectionを取得する]
    #   処理: [instanceが保持するconnectionの有無を調べ、未接続ならclosed errorを返す]
    #   引数: [self: database connectionを要求するJournal]
    #   戻り値: [sqlite3.Connection: SQLを実行できるopen connection]
    #   エラー: [EventJournalClosedError: connectionが未作成またはclose済みの場合]
    # }
    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise EventJournalClosedError("event journal database is not open")
        return self._connection

    # {
    #   責務: [_rollback_open_transaction: 失敗したSQLite書込みtransactionの未commit変更を破棄する]
    #   処理: [接続が残っていればrollbackし、rollback自体のSQLite errorは元の失敗処理を妨げないよう無視する]
    #   引数: [self: transactionを巻き戻すSession Journal]
    #   戻り値: [なし]
    # }
    def _rollback_open_transaction(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.rollback()
        except sqlite3.Error:
            return

    # {
    #   責務: [_discard_connection: 初期化に失敗したJournalからSQLite connectionを切り離す]
    #   処理: [接続があればcloseを試し、close errorの有無にかかわらずinstance参照をNoneにする]
    #   引数: [self: 初期化を中止するSession Journal]
    #   戻り値: [なし]
    # }
    def _discard_connection(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
        except sqlite3.Error:
            pass
        self._connection = None


# {
#   責務: [_encode_json_record: EventEnvelope recordを有限値・空白なしのJSON textへ符号化する]
#   処理: [Unicodeをescapeし、recordのサイズを抑え、NaN/Infinityを拒否する]
#   引数: [record: schema検証済みeventを表すmapping]
#   戻り値: [str: SQLite envelope_json columnへ保存するJSON text]
#   エラー: [ValueError: JSONにできない値または非有限数が含まれる場合]
# }
def _encode_json_record(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# {
#   責務: [_is_sqlite_access_error: database errorをアクセス失敗と保存record破損に分類する]
#   処理: [SQLite OperationalErrorのうちopen/lock/busy/read-only/I/O/permissionの失敗文言だけを照合する]
#   引数: [error: 初期化または接続で発生したSQLite database error]
#   戻り値: [bool: database access由来ならTrue、それ以外ならFalse]
# }
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


# {
#   責務: [_current_utc_timestamp: 新しいeventへ保存するUTC時刻を固定形式で作る]
#   処理: [現在時刻をUTCへ変換し、microsecond精度のISO-8601文字列末尾をZにする]
#   引数: []
#   戻り値: [str: timezoneをZで表したISO-8601 UTC timestamp]
# }
def _current_utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# {
#   責務: [_validate_utc_timestamp: Envelope timestampがtimezone付きUTC時刻か検証する]
    #   処理: [v1で許可する年月日T時分秒[小数]Z形式だけを受け付け、日付としてparseする]
#   引数: [value: event envelopeのtimestamp_utc field]
#   戻り値: [なし: UTC timestampとして受理できる場合に戻る]
#   エラー: [ValueError: 空文字・不正なISO-8601値・timezoneなし・UTC以外のoffsetの場合]
# }
def _validate_utc_timestamp(value: str) -> None:
    _require_nonempty_text(value, "timestamp_utc")
    if _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp_utc must use the v1 UTC date-time format ending in Z")
    try:
        timestamp = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("timestamp_utc must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("timestamp_utc must include the UTC timezone")


# {
#   責務: [_require_integer: event fieldがboolではない整数値で下限を満たすか検証する]
#   処理: [JSON Schemaの整数定義に合わせ、有限な整数値floatを受け入れてintへ変換する]
#   引数: [value: 検証対象field値, field_name: error messageに表示するfield名, minimum: 許可する最小値]
#   戻り値: [int: 検証済み整数]
#   エラー: [ValueError: bool・非整数・minimum未満の場合]
# }
def _require_integer(value: Any, field_name: str, minimum: int) -> int:
    is_integer_value = isinstance(value, int) or (
        isinstance(value, float) and math.isfinite(value) and value.is_integer()
    )
    if isinstance(value, bool) or not is_integer_value or value < minimum:
        raise ValueError(f"{field_name} must be an integer of at least {minimum}")
    return int(value)


# {
#   責務: [_require_nonempty_text: 必須event fieldが空白だけではない文字列か検証する]
#   処理: [str型とstrip後の非空状態を確認し、元の文字列を保持する]
#   引数: [value: 検証対象field値, field_name: error messageに表示するfield名]
#   戻り値: [str: 空白だけではない検証済み文字列]
#   エラー: [ValueError: 文字列以外または空白のみの場合]
# }
def _require_nonempty_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


# {
#   責務: [_require_optional_text: 任意event fieldをNoneまたは非空文字列に制限する]
#   処理: [Noneは未指定値としてそのまま許可し、それ以外は必須文字列検証へ渡す]
#   引数: [value: 検証対象field値, field_name: error messageに表示するfield名]
#   戻り値: [str | None: 未指定のNoneまたは検証済み文字列]
#   エラー: [ValueError: Noneでも文字列でもない値、または空白だけの文字列の場合]
# }
def _require_optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_nonempty_text(value, field_name)


# {
#   責務: [_require_mapping: event objectがmapping型か検証する]
#   処理: [Mapping contractを満たすことを確認し、値を変換せず返す]
#   引数: [value: JSON decode等で得たobject, field_name: error messageに表示するfield名]
#   戻り値: [Mapping[str, Any]: object fieldの参照用mapping]
#   エラー: [ValueError: objectがmapping型ではない場合]
# }
def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


# {
#   責務: [_json_compatible_copy: payloadをJSON保存可能な独立containerへ複製して不正値を拒否する]
#   処理: [有限数・文字列key・list/tuple・mappingだけを再帰copyし、現在の探索経路で再登場するcontainerを循環参照として拒否する]
#   引数: [value: payload内の検証対象値, field_name: error位置を示すJSON field path, active_container_ids: 再帰経路上のcontainer ID集合]
#   戻り値: [Any: JSON互換プリミティブ、dict、listから成る独立コピー]
#   エラー: [ValueError: 非有限数・非文字列key・循環参照・未対応型を含む場合]
# }
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
