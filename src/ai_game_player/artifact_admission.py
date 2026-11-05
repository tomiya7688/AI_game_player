"""Artifact Storeへの新規保存とRetention登録を、root単位のtransactionとして整合させる。"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from ai_game_player.admission_pending_store import (
    SOURCE_REF_PATTERN,
    PendingAdmission,
    PendingAdmissionStore,
)
from ai_game_player.artifact_store import ArtifactIntegrityError
from ai_game_player.experience import ArtifactReference
from ai_game_player.retention_collector import (
    RetentionCapacityError,
    RetentionCollector,
    RetentionSnapshot,
    current_time_ms,
)
from ai_game_player.retention_index import RetentionEntry, RetentionIndexError
from ai_game_player.retention_root_lock import (
    RetentionLockTimeoutError,
    require_plain_directory,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER_SHA256: Final = "0" * 64
_MILLISECONDS_PER_SECOND: Final = 1000.0

OUTCOME_COMMITTED: Final = "committed"
OUTCOME_REJECTED_CAPACITY: Final = "rejected_capacity"
OUTCOME_REJECTED_INVALID: Final = "rejected_invalid"
OUTCOME_LOCK_TIMEOUT: Final = "lock_timeout"
OUTCOME_FAILED_ROLLED_BACK: Final = "failed_rolled_back"
OUTCOME_FAILED_PENDING_RETAINED: Final = "failed_pending_retained"

STATE_REGISTERED: Final = "registered"
STATE_INDEXED_BY_OTHER_ENTRY: Final = "indexed_by_other_entry"
STATE_NO_OBJECT: Final = "no_object"
STATE_PREEXISTING: Final = "preexisting"
STATE_ORPHAN_CREATED: Final = "orphan_created"


# {
#   責務: [ArtifactAdmissionError: put以降の失敗を、補償結果付きで呼出側へ通知する（silent successにしない）]
#   フィールド: [admission_id: この試行のID, rolled_back: 新規作成objectとrecordを取り消せたか,
#     pending_retained: 補償できずpending recordを残したか（#790/recover_pendingが回収する）]
# }
class ArtifactAdmissionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        admission_id: str,
        rolled_back: bool,
        pending_retained: bool,
    ) -> None:
        super().__init__(message)
        self.admission_id = admission_id
        self.rolled_back = rolled_back
        self.pending_retained = pending_retained


# {
#   責務: [ArtifactAdmissionRequest: 1回の保存要求を、予約量と登録内容を含む検証済みの不変値として表す]
#   フィールド: [payload_size: writerが書くpayloadのbyte数（0以上）, expected_sha256: 既知のdigest。
#     指定時は既存digestを追加容量0で判定しwriterのpayloadと照合する, artifact_type: Artifactの意味型,
#     media_type: MIME型, entry_id: 索引のentry_id, protected: 登録時のProtected,
#     source_ref: 診断用の識別子（pathや秘密を入れない）, sensitive: ArtifactReferenceへ保存する機微フラグ]
# }
@dataclass(frozen=True)
class ArtifactAdmissionRequest:
    payload_size: int
    expected_sha256: str | None
    artifact_type: str
    media_type: str
    entry_id: str
    protected: bool
    source_ref: str
    sensitive: bool = False

    # {
    #   責務: [__post_init__: 契約外の保存要求を、Storeに触れる前の生成時点で拒否する]
    #   処理: [サイズ・digest・source_refの形式を検査し、型・MIME・entry_idはArtifactReferenceとRetentionEntryの検証を再利用する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [ValueError: いずれかのフィールドが契約外（RetentionIndexErrorを含む）]
    # }
    def __post_init__(self) -> None:
        if type(self.payload_size) is not int or self.payload_size < 0:
            raise ValueError("payload_size must be a non-negative integer")
        if self.expected_sha256 is not None and (
            not isinstance(self.expected_sha256, str)
            or _SHA256.fullmatch(self.expected_sha256) is None
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 or None")
        if not isinstance(self.source_ref, str) or (
            SOURCE_REF_PATTERN.fullmatch(self.source_ref) is None
        ):
            raise ValueError("source_ref must be a short identifier")
        ArtifactReference(
            "admission.request", _PLACEHOLDER_SHA256, self.media_type, self.sensitive
        )
        RetentionEntry(
            self.entry_id,
            _PLACEHOLDER_SHA256,
            0,
            self.artifact_type,
            0,
            None,
            self.protected,
        )


# {
#   責務: [AdmissionTraceEvent: 1回のadmitの結果と、lock待ち・transaction時間・rollback有無を観測用に表す]
#   フィールド: [outcome: OUTCOME_*のいずれか, admission_id: 試行ID, source_ref/entry_id: requestの識別子,
#     artifact_sha256: putしたpayloadのdigest（put前の終了ではNone）, lock_wait_ms: root lock取得の待ちms,
#     transaction_ms: admit全体のms, rolled_back: 補償を実行して完了したか,
#     pending_cleanup_failed: 成功後にpending recordを消せず残ったか]
# }
@dataclass(frozen=True)
class AdmissionTraceEvent:
    outcome: str
    admission_id: str
    source_ref: str
    entry_id: str
    artifact_sha256: str | None
    lock_wait_ms: float
    transaction_ms: float
    rolled_back: bool
    pending_cleanup_failed: bool


# {
#   責務: [AdmissionStats: coordinatorの累積観測値（件数・rollback数・lock待ち・transaction時間）の読取専用snapshot]
#   フィールド: [outcome_counts: OUTCOME_*ごとの件数, rollbacks: 補償が完了した件数, pending_cleanup_failures: 成功後のrecord削除失敗数,
#     trace_errors: traceコールバックの例外数, lock_wait_ms_total/max: lock待ちの合計と最大, transaction_ms_total/max: transaction時間の合計と最大]
# }
@dataclass(frozen=True)
class AdmissionStats:
    outcome_counts: dict[str, int]
    rollbacks: int
    pending_cleanup_failures: int
    trace_errors: int
    lock_wait_ms_total: float
    lock_wait_ms_max: float
    transaction_ms_total: float
    transaction_ms_max: float


# {
#   責務: [PendingAdmissionStatus: pending recordと実際のStore/索引を照合した分類結果を#790などへ渡す]
#   フィールド: [record: 照合したpending record, state: STATE_*の分類]
# }
@dataclass(frozen=True)
class PendingAdmissionStatus:
    record: PendingAdmission
    state: str


# {
#   責務: [AdmissionRecoveryReport: recover_pendingが回収・保留したrecordとobjectを報告する]
#   フィールド: [removed_records: 削除したadmission_id, deleted_objects: 削除した新規作成orphanのdigest,
#     retained_records: 他recordが同じdigestを参照するため残したadmission_id]
# }
@dataclass(frozen=True)
class AdmissionRecoveryReport:
    removed_records: tuple[str, ...]
    deleted_objects: tuple[str, ...]
    retained_records: tuple[str, ...]


# {
#   責務: [_AdmissionSession: 1回のadmitの進行状態（record・結果・観測値）を1つの所有者にまとめる]
#   フィールド: [request: 検証済みの要求, admission_id: 試行ID, record: put直前に作ったpending record。
#     put未到達ならNone, put_calls: portのput呼出回数, outcome: 確定した結果区分,
#     lock_wait_ms: root lock待ち, rolled_back: 補償が完了したか, cleanup_failed: 成功後のrecord削除失敗]
# }
@dataclass
class _AdmissionSession:
    request: ArtifactAdmissionRequest
    admission_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    record: PendingAdmission | None = None
    put_calls: int = 0
    outcome: str = OUTCOME_REJECTED_INVALID
    lock_wait_ms: float = 0.0
    rolled_back: bool = False
    cleanup_failed: bool = False


# {
#   責務: [AdmissionPutPort: writerへ渡す唯一の書込み口。Storeやpathを渡さず、bytesのputだけを許す]
#   フィールド: [_put: coordinatorが検査付きでStoreへ書く関数]
#   補足: [Pythonでは内部属性に到達できるため、悪意あるwriterへの境界ではなく、誤ってroot外へ書く経路を作らないための契約]
# }
class AdmissionPutPort:
    __slots__ = ("_put",)

    def __init__(self, put: Callable[[bytes], ArtifactReference]) -> None:
        self._put = put

    # {
    #   責務: [put: requestに固定されたmedia_type/artifact_typeでpayloadを1回だけ保存する]
    #   処理: [サイズ・digest・shard directoryを検査し、pending recordを書いてからStoreへatomic putする]
    #   引数: [payload: request.payload_sizeと同じ長さのbytes]
    #   戻り値: [ArtifactReference: 保存したArtifactの参照]
    #   エラー: [TypeError: bytesでない, ValueError: 2回目の呼出し, ArtifactIntegrityError: サイズ/digest不一致,
    #     RetentionRootUnsafeError: shardがlink, OSError: 保存失敗]
    # }
    def put(self, payload: bytes) -> ArtifactReference:
        return self._put(payload)


AdmissionWriter = Callable[[AdmissionPutPort], ArtifactReference]


# {
#   責務: [ArtifactAdmissionCoordinator: 容量判定・put・Retention登録・補償を同一root lock内の1 transactionにする]
#   フィールド: [collector: root lockと容量計算・索引更新・安全削除を提供するRetentionCollector,
#     trace: admitごとに結果を受ける任意コールバック, _records: pending recordの保存先,
#     _counters: 累積観測値を守るlockと値]
#   補足: [writerはlock保持中に動く。lock待ちは各呼出しのlock_timeout_secondsで有界。
#     FileSystemArtifactStore.putを直接呼ぶ書込みはこのlockの対象外]
# }
class ArtifactAdmissionCoordinator:
    # {
    #   責務: [__init__: 対象Collectorとpending record保存先、観測コールバックを固定する]
    #   処理: [recordsを省略した場合はStore root直下の専用directoryを使う]
    #   引数: [collector: 管理対象のRetentionCollector, trace: AdmissionTraceEventを受ける任意関数,
    #     records: pending recordの保存先（省略可）]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        collector: RetentionCollector,
        *,
        trace: Callable[[AdmissionTraceEvent], None] | None = None,
        records: PendingAdmissionStore | None = None,
    ) -> None:
        self.collector = collector
        self.trace = trace
        self._records = (
            records
            if records is not None
            else PendingAdmissionStore(collector.store.root)
        )
        self._guard = threading.Lock()
        self._outcome_counts: dict[str, int] = {}
        self._totals = {
            "rollbacks": 0,
            "pending_cleanup_failures": 0,
            "trace_errors": 0,
            "lock_wait_ms_total": 0.0,
            "lock_wait_ms_max": 0.0,
            "transaction_ms_total": 0.0,
            "transaction_ms_max": 0.0,
        }

    # {
    #   責務: [stats: これまでのadmitの累積観測値を読取専用snapshotで返す]
    #   処理: [counterのlockを取り、値を複製する]
    #   引数: [なし]
    #   戻り値: [AdmissionStats: 件数・rollback数・lock待ち・transaction時間]
    # }
    @property
    def stats(self) -> AdmissionStats:
        with self._guard:
            return AdmissionStats(
                outcome_counts=dict(self._outcome_counts),
                rollbacks=int(self._totals["rollbacks"]),
                pending_cleanup_failures=int(self._totals["pending_cleanup_failures"]),
                trace_errors=int(self._totals["trace_errors"]),
                lock_wait_ms_total=self._totals["lock_wait_ms_total"],
                lock_wait_ms_max=self._totals["lock_wait_ms_max"],
                transaction_ms_total=self._totals["transaction_ms_total"],
                transaction_ms_max=self._totals["transaction_ms_max"],
            )

    # {
    #   責務: [admit: 新規Artifactの容量判定・保存・Retention登録を1 transactionとして行う]
    #   処理: [1: root lockを期限付きで取得  2: 索引とStore使用量を再計測し、entry_id重複・件数・hard limitをput前に判定
    #     3: writerを実行（portのput直前にpending record）  4: 保存結果を検証し、Retention indexへ登録
    #     5: 成功でrecordを削除。失敗は新規作成objectだけ補償して、補償不能ならrecordを残す  6: 結果を1回だけtrace]
    #   引数: [request: 検証済みの保存要求, writer: AdmissionPutPortのputを1回呼びArtifactReferenceを返す関数]
    #   戻り値: [ArtifactReference: 保存・登録まで完了したArtifactの参照]
    #   エラー: [RetentionLockTimeoutError: lock期限超過, RetentionCapacityError: hard limit/件数上限超過（put前）,
    #     RetentionIndexError: 索引・metadata不正またはentry_id重複（put前）, RetentionRootUnsafeError: root等がlink,
    #     ArtifactAdmissionError: writer/put/検証/register失敗（補償結果付き）, BaseException: 補償後に元のまま再送出]
    # }
    def admit(
        self, request: ArtifactAdmissionRequest, writer: AdmissionWriter
    ) -> ArtifactReference:
        if not isinstance(request, ArtifactAdmissionRequest):
            raise TypeError("request must be an ArtifactAdmissionRequest")
        if not callable(writer):
            raise TypeError("writer must be callable")
        session = _AdmissionSession(request)
        started = time.monotonic()
        try:
            with self.collector.coordination_lock() as waited_ms:
                session.lock_wait_ms = waited_ms
                return self._run_transaction(session, writer)
        except RetentionLockTimeoutError:
            session.outcome = OUTCOME_LOCK_TIMEOUT
            raise
        except RetentionCapacityError:
            session.outcome = OUTCOME_REJECTED_CAPACITY
            raise
        finally:
            self._emit(session, (time.monotonic() - started) * _MILLISECONDS_PER_SECOND)

    # {
    #   責務: [inspect_pending: 残っているpending recordをStoreと索引の実状態で分類する（変更しない）]
    #   処理: [root lock内で全recordと索引を厳格に読み、recordごとに分類する]
    #   引数: [なし]
    #   戻り値: [tuple: admission_id順のPendingAdmissionStatus]
    #   エラー: [AdmissionRecordError: record破損, RetentionIndexError: 索引不正, RetentionLockTimeoutError: lock期限超過]
    # }
    def inspect_pending(self) -> tuple[PendingAdmissionStatus, ...]:
        with self.collector.coordination_lock():
            entries = self.collector.load_entries()
            return tuple(
                PendingAdmissionStatus(record, self._classify(record, entries))
                for record in self._records.list_records()
            )

    # {
    #   責務: [recover_pending: 中断したadmissionのrecordを、安全に確認できる範囲で回収する]
    #   処理: [lockを取れた時点で既存recordは全て中断済み。1: 完了済み・object無し・既存digest・他entry参照のrecordを削除
    #     2: 新規作成された未索引orphanは、他recordが同じdigestを参照しなければobjectとrecordを削除、参照があれば保留]
    #   引数: [なし]
    #   戻り値: [AdmissionRecoveryReport: 削除したrecord・object、保留したrecord]
    #   エラー: [AdmissionRecordError: record破損, RetentionIndexError: 索引不正, RetentionSafetyError: 削除先がlink等,
    #     OSError: 削除失敗, RetentionLockTimeoutError: lock期限超過]
    # }
    def recover_pending(self) -> AdmissionRecoveryReport:
        removed: list[str] = []
        deleted: list[str] = []
        retained: list[str] = []
        with self.collector.coordination_lock():
            entries = self.collector.load_entries()
            statuses = [
                PendingAdmissionStatus(record, self._classify(record, entries))
                for record in self._records.list_records()
            ]
            remaining = {
                status.record.admission_id: status.record for status in statuses
            }
            for status in statuses:
                if status.state == STATE_ORPHAN_CREATED:
                    continue
                self._records.remove(status.record.admission_id)
                del remaining[status.record.admission_id]
                removed.append(status.record.admission_id)
            for status in statuses:
                record = status.record
                if status.state != STATE_ORPHAN_CREATED:
                    continue
                if _referenced_by_other(record, remaining.values()):
                    retained.append(record.admission_id)
                    continue
                if self.collector.discard_unindexed_object(record.artifact_sha256):
                    deleted.append(record.artifact_sha256)
                self._records.remove(record.admission_id)
                del remaining[record.admission_id]
                removed.append(record.admission_id)
        return AdmissionRecoveryReport(tuple(removed), tuple(deleted), tuple(retained))

    # {
    #   責務: [_run_transaction: root lock保持中に、判定→保存→登録を行い、失敗を補償する]
    #   処理: [put前の拒否は元の例外のまま通し、writer以降の失敗は補償して結果を確定する]
    #   引数: [session: この試行の進行状態, writer: 呼出側のwriter]
    #   戻り値: [ArtifactReference: 登録まで完了した参照]
    #   エラー: [admitと同じ]
    # }
    def _run_transaction(
        self, session: _AdmissionSession, writer: AdmissionWriter
    ) -> ArtifactReference:
        self._reject_unadmittable(session.request, self.collector.measure())
        try:
            reference = writer(
                AdmissionPutPort(lambda payload: self._put(session, payload))
            )
            self._verify_put(session, reference)
            self._register(session, reference)
        except BaseException as error:
            # Exceptionは補償結果付きの例外で包み、KeyboardInterrupt等は補償後に元のまま再送出する。
            failure = self._compensate(
                session, error if isinstance(error, Exception) else None
            )
            if isinstance(error, Exception):
                raise failure from error
            raise
        self._commit(session)
        return reference

    # {
    #   責務: [_reject_unadmittable: put前にentry_id重複・件数上限・hard limit超過を判定して拒否する]
    #   処理: [既存digest（索引または未索引）は追加容量0、それ以外とdigest不明はpayload_sizeを追加して合計をhard limitと比較する]
    #   引数: [request: 保存要求, snapshot: lock内で再計測した索引と使用量]
    #   戻り値: [なし]
    #   エラー: [RetentionIndexError: entry_id重複, RetentionCapacityError: 件数上限またはhard limit超過]
    # }
    def _reject_unadmittable(
        self, request: ArtifactAdmissionRequest, snapshot: RetentionSnapshot
    ) -> None:
        entries = snapshot.entries
        if any(entry.entry_id == request.entry_id for entry in entries):
            raise RetentionIndexError(
                f"duplicate retention entry_id: {request.entry_id}"
            )
        policy = self.collector.policy
        if len(entries) >= policy.max_entries:
            raise RetentionCapacityError("retention index entry limit reached")
        known = {entry.artifact_sha256 for entry in entries} | (
            snapshot.usage.unindexed_digests
        )
        additional = 0 if request.expected_sha256 in known else request.payload_size
        if snapshot.usage.total_bytes + additional > policy.hard_limit_bytes:
            raise RetentionCapacityError("admission would exceed the hard limit")

    # {
    #   責務: [_put: portから呼ばれ、検査とpending record作成の後でStoreへatomic putする]
    #   処理: [1回限り・bytes・長さ・expected digestを検査し、shardとobjectがlinkでないことを確認する。
    #     putの直前に既存有無を判定してrecordを原子的に書き、Store.putを呼ぶ]
    #   引数: [session: この試行の進行状態, payload: 保存するbytes]
    #   戻り値: [ArtifactReference: 保存した参照]
    #   エラー: [AdmissionPutPortと同じ]
    # }
    def _put(self, session: _AdmissionSession, payload: bytes) -> ArtifactReference:
        request = session.request
        session.put_calls += 1
        if session.put_calls > 1:
            raise ValueError("admission put may be called only once")
        if not isinstance(payload, bytes):
            raise TypeError("artifact payload must be bytes")
        if len(payload) != request.payload_size:
            raise ArtifactIntegrityError(
                "payload size does not match the admission request"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if request.expected_sha256 is not None and digest != request.expected_sha256:
            raise ArtifactIntegrityError(
                "payload digest does not match the admission request"
            )
        store = self.collector.store
        object_path = store.object_path(digest)
        require_plain_directory(object_path.parent, "artifact shard directory")
        require_plain_directory(object_path, "artifact object directory")
        record = PendingAdmission(
            admission_id=session.admission_id,
            artifact_sha256=digest,
            created_object=not os.path.lexists(object_path),
            entry_id=request.entry_id,
            source_ref=request.source_ref,
            artifact_type=request.artifact_type,
            media_type=request.media_type,
            payload_size=request.payload_size,
            protected=request.protected,
            started_at_ms=current_time_ms(),
            owner_pid=os.getpid(),
        )
        self._records.write(record)
        session.record = record
        return store.put(
            payload,
            media_type=request.media_type,
            artifact_type=request.artifact_type,
            sensitive=request.sensitive,
        )

    # {
    #   責務: [_verify_put: writerが返した参照が、実際にputしたpayloadとrequestに一致するか確認する]
    #   処理: [portを経由したputの有無、digest・media_type・sensitive、保存metadataの型とサイズを照合する]
    #   引数: [session: この試行の進行状態, reference: writerの戻り値]
    #   戻り値: [なし]
    #   エラー: [ArtifactIntegrityError: portのputが無い・参照または保存内容が不一致]
    # }
    def _verify_put(
        self, session: _AdmissionSession, reference: ArtifactReference
    ) -> None:
        request, record = session.request, session.record
        if record is None:
            raise ArtifactIntegrityError(
                "writer did not store through the admission port"
            )
        if (
            not isinstance(reference, ArtifactReference)
            or reference.sha256 != record.artifact_sha256
            or reference.media_type != request.media_type
            or reference.sensitive != request.sensitive
        ):
            raise ArtifactIntegrityError(
                "writer returned a reference that was not stored"
            )
        metadata = self.collector.store.read_metadata(record.artifact_sha256)
        if (
            metadata.artifact_type != request.artifact_type
            or metadata.size_bytes != request.payload_size
        ):
            raise ArtifactIntegrityError("stored artifact does not match the request")

    # {
    #   責務: [_register: 保存済みArtifactをRetention indexへ登録し、公開後に例外が出た場合は実状態で判定する]
    #   処理: [Collectorのregisterを呼び、失敗時は索引を読み直して同じentry_idとdigestの記録が有れば成功とみなす]
    #   引数: [session: この試行の進行状態, reference: 保存した参照]
    #   戻り値: [なし]
    #   エラー: [registerの例外: 索引に今回の記録が無い場合そのまま再送出]
    # }
    def _register(
        self, session: _AdmissionSession, reference: ArtifactReference
    ) -> None:
        request = session.request
        try:
            self.collector.register(
                reference, entry_id=request.entry_id, protected=request.protected
            )
        except Exception:
            if not self._is_registered(request.entry_id, reference.sha256):
                raise

    # {
    #   責務: [_is_registered: 指定のentry_idがそのdigestで索引に登録済みか、実状態から判定する]
    #   処理: [索引を読み直し、読めない場合は登録済みと扱わない]
    #   引数: [entry_id: 確認する記録ID, digest: 期待するArtifact SHA-256]
    #   戻り値: [bool: entry_idとdigestが一致する記録が有ればTrue]
    # }
    def _is_registered(self, entry_id: str, digest: str) -> bool:
        try:
            entries = self.collector.load_entries()
        except RetentionIndexError:
            return False
        return any(
            entry.entry_id == entry_id and entry.artifact_sha256 == digest
            for entry in entries
        )

    # {
    #   責務: [_commit: 登録まで成功したadmissionのpending recordを消して成功を確定する]
    #   処理: [record削除に失敗してもadmissionは成功。残ったrecordは後でrecover_pendingが完了済みとして回収する]
    #   引数: [session: この試行の進行状態]
    #   戻り値: [なし]
    # }
    def _commit(self, session: _AdmissionSession) -> None:
        try:
            self._records.remove(session.admission_id)
        except Exception:
            session.cleanup_failed = True
        session.outcome = OUTCOME_COMMITTED

    # {
    #   責務: [_compensate: 失敗したadmissionが作ったobjectとrecordを、安全に取り消せる範囲で取り消す]
    #   処理: [record未作成なら取り消す対象なし。有れば_rollbackを実行し、補償不能ならrecordを残す。結果をsessionへ確定する]
    #   引数: [session: この試行の進行状態, cause: 失敗の原因。BaseExceptionの再送出ではNone]
    #   戻り値: [ArtifactAdmissionError: 補償結果付きの例外（Exceptionの場合に送出する）]
    # }
    def _compensate(
        self, session: _AdmissionSession, cause: Exception | None
    ) -> ArtifactAdmissionError:
        retained = False
        if session.record is not None:
            try:
                retained = not self._rollback(session.record)
            except Exception:
                retained = True
        session.rolled_back = not retained
        session.outcome = (
            OUTCOME_FAILED_PENDING_RETAINED if retained else OUTCOME_FAILED_ROLLED_BACK
        )
        reason = (
            f"{type(cause).__name__}: {cause}" if cause is not None else "interrupted"
        )
        return ArtifactAdmissionError(
            f"artifact admission failed ({reason}); "
            + ("pending record retained for recovery" if retained else "rolled back"),
            admission_id=session.admission_id,
            rolled_back=session.rolled_back,
            pending_retained=retained,
        )

    # {
    #   責務: [_rollback: このadmissionが新規作成したobjectだけを消し、recordを取り除く]
    #   処理: [created_objectがTrueで、他のpending recordが同じdigestを参照せず、索引にも無い場合だけobjectを削除する。
    #     既存digest・索引登録済みのobjectには触れない。他recordが参照するなら自分のrecordを残す]
    #   引数: [record: put直前に書いたpending record]
    #   戻り値: [bool: Trueなら取り消し完了。Falseはrecordを残すべき状態]
    #   エラー: [AdmissionRecordError/RetentionSafetyError/OSError: 他recordの読取不正や削除失敗（呼出側がrecord保持に倒す）]
    # }
    def _rollback(self, record: PendingAdmission) -> bool:
        if record.created_object:
            others = [
                other
                for other in self._records.list_records()
                if other.admission_id != record.admission_id
            ]
            if _referenced_by_other(record, others):
                return False
            self.collector.discard_unindexed_object(record.artifact_sha256)
        self._records.remove(record.admission_id)
        with self._guard:
            self._totals["rollbacks"] += 1
        return True

    # {
    #   責務: [_classify: pending recordをStoreと索引の実状態で1つのSTATE_*へ分類する]
    #   処理: [索引に同じentry_idとdigestが有ればregistered、digestだけ有ればindexed_by_other_entry、
    #     objectが無ければno_object、既存digestならpreexisting、それ以外は新規作成のorphan]
    #   引数: [record: 分類するpending record, entries: lock内で読んだ索引記録]
    #   戻り値: [str: STATE_*]
    # }
    def _classify(
        self, record: PendingAdmission, entries: tuple[RetentionEntry, ...]
    ) -> str:
        digest_entries = [
            e for e in entries if e.artifact_sha256 == record.artifact_sha256
        ]
        if any(e.entry_id == record.entry_id for e in digest_entries):
            return STATE_REGISTERED
        if digest_entries:
            return STATE_INDEXED_BY_OTHER_ENTRY
        if not os.path.lexists(
            self.collector.store.object_path(record.artifact_sha256)
        ):
            return STATE_NO_OBJECT
        if not record.created_object:
            return STATE_PREEXISTING
        return STATE_ORPHAN_CREATED

    # {
    #   責務: [_emit: admitの結果を累積観測値とtraceコールバックへちょうど1回反映する]
    #   処理: [counterを更新し、traceの例外はtransactionへ影響させずtrace_errorsに数える]
    #   引数: [session: 結果が確定した試行, transaction_ms: admit全体のms]
    #   戻り値: [なし]
    # }
    def _emit(self, session: _AdmissionSession, transaction_ms: float) -> None:
        event = AdmissionTraceEvent(
            outcome=session.outcome,
            admission_id=session.admission_id,
            source_ref=session.request.source_ref,
            entry_id=session.request.entry_id,
            artifact_sha256=(
                session.record.artifact_sha256 if session.record is not None else None
            ),
            lock_wait_ms=session.lock_wait_ms,
            transaction_ms=transaction_ms,
            rolled_back=session.rolled_back,
            pending_cleanup_failed=session.cleanup_failed,
        )
        with self._guard:
            self._outcome_counts[event.outcome] = (
                self._outcome_counts.get(event.outcome, 0) + 1
            )
            if event.pending_cleanup_failed:
                self._totals["pending_cleanup_failures"] += 1
            self._totals["lock_wait_ms_total"] += event.lock_wait_ms
            self._totals["lock_wait_ms_max"] = max(
                self._totals["lock_wait_ms_max"], event.lock_wait_ms
            )
            self._totals["transaction_ms_total"] += event.transaction_ms
            self._totals["transaction_ms_max"] = max(
                self._totals["transaction_ms_max"], event.transaction_ms
            )
        if self.trace is None:
            return
        try:
            self.trace(event)
        except Exception:
            with self._guard:
                self._totals["trace_errors"] += 1


# {
#   責務: [_referenced_by_other: 他のpending recordが同じdigestのobjectを参照しているか判定する]
#   処理: [admission_idが異なり、artifact_sha256が一致するrecordが有るか確認する]
#   引数: [record: 基準のpending record, others: 比較対象のpending record]
#   戻り値: [bool: 他recordが参照していればTrue]
# }
def _referenced_by_other(
    record: PendingAdmission, others: Iterable[PendingAdmission]
) -> bool:
    return any(
        other.admission_id != record.admission_id
        and other.artifact_sha256 == record.artifact_sha256
        for other in others
    )
