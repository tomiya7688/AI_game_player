"""crash由来の未索引Artifact（orphan）を状態分類し、安全なものだけをtrash経由で回収する。"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

from ai_game_player.admission_pending_store import AdmissionRecordError
from ai_game_player.artifact_admission import (
    AdmissionRecoveryReport,
    ArtifactAdmissionCoordinator,
)
from ai_game_player.artifact_store import ArtifactMetadata
from ai_game_player.artifact_trash import (
    CURSOR_FILENAME,
    KIND_OBJECT,
    KIND_STAGING,
    REASON_SAFE_ORPHAN,
    REASON_STALE_STAGING,
    ArtifactTrash,
    TrashIntent,
    TrashIntentError,
)
from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.data_inventory import load_data_inventory
from ai_game_player.experience import ArtifactReference
from ai_game_player.retention_collector import (
    RetentionCollector,
    RetentionReport,
    RetentionSafetyError,
)
from ai_game_player.retention_index import RetentionIndexError
from ai_game_player.retention_planner import plan_retention
from ai_game_player.retention_root_lock import is_junction

STATE_ACTIVE_PENDING: Final = "active_pending"
STATE_LIVE_REFERENCED: Final = "live_referenced"
STATE_RECENT_UNINDEXED: Final = "recent_unindexed"
STATE_SAFE_ORPHAN: Final = "safe_orphan"
STATE_CORRUPT_OR_UNKNOWN: Final = "corrupt_or_unknown"

TRIGGER_STARTUP: Final = "startup"
TRIGGER_SESSION_CLOSE: Final = "session_close"
TRIGGER_BUDGET: Final = "budget"
TRIGGER_EXPLICIT: Final = "explicit"
TRIGGER_PERIODIC: Final = "periodic"
TRIGGERS: Final = frozenset(
    {
        TRIGGER_STARTUP,
        TRIGGER_SESSION_CLOSE,
        TRIGGER_BUDGET,
        TRIGGER_EXPLICIT,
        TRIGGER_PERIODIC,
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHARD = re.compile(r"^[0-9a-f]{2}$")
_CURSOR_KEY = re.compile(r"^[0-9a-f]{2}(?:/[^/\\]{1,128})?$")
_STAGING_PREFIX = ".pending-"
_ALLOWED_FILES = frozenset({"payload", "metadata.json"})
_MAX_METADATA_BYTES = 16 * 1024
_HASH_CHUNK_BYTES = 64 * 1024
_MIN_GRACE_MS = 60_000
_MAX_GRACE_MS = 30 * 86_400_000
_MAX_CANDIDATES_LIMIT = 1_000_000


# {
#   責務: [LiveReferenceSnapshot: 1つの保持者が1時点で返す、生きているArtifact参照の集合]
#   フィールド: [source: 保持者名（診断用）, references: 参照中のArtifact, complete: 取りこぼしなく列挙できたか。
#     Falseなら回収全体を止める]
# }
@dataclass(frozen=True)
class LiveReferenceSnapshot:
    source: str
    references: tuple[ArtifactReference, ...]
    complete: bool


# {
#   責務: [LiveReferenceSource: Journal/Experience/Replayなど参照を持つ側が実装する、参照集合の提供契約]
#   フィールド: [なし: 契約だけ]
# }
class LiveReferenceSource(Protocol):
    # {
    #   責務: [snapshot: 現在生きている参照を1時点の集合として返す]
    #   処理: [実装側が自分の参照を取りこぼしなく列挙する。列挙できなければcomplete=Falseにする]
    #   引数: [なし]
    #   戻り値: [LiveReferenceSnapshot: 参照集合と完全性]
    # }
    def snapshot(self) -> LiveReferenceSnapshot: ...


# {
#   責務: [OrphanReconcilerConfig: 回収の猶予・上限・間隔を1つの不変値に固定する]
#   フィールド: [grace_ms: 最終活動からの猶予, future_skew_ms: 未来時刻を時計ずれとみなす許容,
#     max_candidates: 1回の走査で候補にする最大件数, max_verify_bytes_per_run: 1回のpayload検証byte上限（1件目は超えても検証）,
#     max_delete_count: 1回の削除件数上限（object+staging）, max_lock_hold_ms: apply中のroot lock保持上限,
#     live_snapshot_timeout_ms: live snapshot取得の合計上限, periodic_min_interval_ms: periodic契機の最小間隔]
# }
@dataclass(frozen=True)
class OrphanReconcilerConfig:
    grace_ms: int = 86_400_000
    future_skew_ms: int = 300_000
    max_candidates: int = 10_000
    max_verify_bytes_per_run: int = 1 << 30
    max_delete_count: int = 1_000
    max_lock_hold_ms: int = 1_000
    live_snapshot_timeout_ms: int = 2_000
    periodic_min_interval_ms: int = 600_000

    # {
    #   責務: [__post_init__: 範囲外の設定を生成時点で拒否する]
    #   処理: [全項目が整数であること、graceが60秒〜30日、他が正（skewは0以上）かを確認する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [ValueError: いずれかの値が範囲外]
    # }
    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int:
                raise ValueError(f"{name} must be an integer")
        if not _MIN_GRACE_MS <= self.grace_ms <= _MAX_GRACE_MS:
            raise ValueError("grace_ms must be between 60 seconds and 30 days")
        if self.future_skew_ms < 0:
            raise ValueError("future_skew_ms must not be negative")
        if not 1 <= self.max_candidates <= _MAX_CANDIDATES_LIMIT:
            raise ValueError("max_candidates is out of range")
        for name in (
            "max_verify_bytes_per_run",
            "max_delete_count",
            "max_lock_hold_ms",
            "live_snapshot_timeout_ms",
            "periodic_min_interval_ms",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


# {
#   責務: [OrphanCandidate: 走査で見つけた索引外の1件と、その判定結果・変更検出用fingerprintを表す]
#   フィールド: [location: shard/名前（root相対）, kind: object/staging, sha256: objectのdigest,
#     state: STATE_*, reason: 判定理由, size_bytes: payload等の合計byte, artifact_type/sensitive: metadataの分類,
#     last_activity_ms: 最終活動時刻（不明はNone）, fingerprint: (ino,size,mtime_ns,ctime_ns)の組]
# }
@dataclass(frozen=True)
class OrphanCandidate:
    location: str
    kind: str
    sha256: str | None
    state: str
    reason: str
    size_bytes: int
    artifact_type: str | None
    sensitive: bool | None
    last_activity_ms: int | None
    fingerprint: tuple[Any, ...]


# {
#   責務: [OrphanReconcilePlan: dry-runで求めた判定結果と削除予定（planは何も変更しない）]
#   フィールド: [state_version: 判定入力のhash, evaluated_at_ms: 評価時刻, trigger: 契機, candidates: 全候補,
#     targets: 削除予定（safe_orphanと削除可能なstale staging）, stale_staging: targetsのうちstagingの位置,
#     trash_residual_bytes: trashに残るbyte, scan_start/scan_next: 走査cursor,
#     blocked_reason: 全体を止めた理由, truncated: 1周しきれず打切ったか]
# }
@dataclass(frozen=True)
class OrphanReconcilePlan:
    state_version: str
    evaluated_at_ms: int
    trigger: str
    candidates: tuple[OrphanCandidate, ...]
    targets: tuple[OrphanCandidate, ...]
    stale_staging: tuple[str, ...]
    trash_residual_bytes: int
    scan_start: str | None
    scan_next: str | None
    blocked_reason: str | None
    truncated: bool


# {
#   責務: [OrphanDeleteAudit: 回収した1件の監査項目。pathとpayloadを含まず#225がそのまま保存できる]
#   フィールド: [audit_id: trash intent ID, sha256: objectのdigest, kind: object/staging, artifact_type/data_class/sensitive: 分類,
#     size_bytes: 回収byte, reason: 理由, trigger: 契機, deleted_at_ms: 論理削除時刻,
#     recovered: crash前の削除をintentから再生成した分か]
# }
@dataclass(frozen=True)
class OrphanDeleteAudit:
    audit_id: str
    sha256: str | None
    kind: str
    artifact_type: str | None
    data_class: str | None
    sensitive: bool | None
    size_bytes: int
    reason: str
    trigger: str | None
    deleted_at_ms: int | None
    recovered: bool

    # {
    #   責務: [to_dict: 監査項目をJSON化可能なdictへ変換する]
    #   処理: [全フィールドを出力する（pathとpayloadは元々持たない）]
    #   引数: [なし]
    #   戻り値: [dict: JSON化可能な監査項目]
    # }
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# {
#   責務: [OrphanReconcileReport: apply結果（削除・保留・異常・audit・trash状況）を返す]
#   フィールド: [plan_version/applied_version: 計画時と適用時のstate_version,
#     recovered_pending: recover_pendingの結果, deleted: 削除した位置, deleted_bytes: 削除byte合計,
#     skipped: (位置, 理由), corrupt: 診断として残した(位置, 理由), audits: 未ackのaudit,
#     trash_swept: 掃除を完了したtrash entry数, trash_residual_bytes: 適用後のtrash残存byte,
#     blocked_reason: 全体を止めた理由, stopped_by_lock_budget: lock保持上限で打切ったか]
# }
@dataclass(frozen=True)
class OrphanReconcileReport:
    plan_version: str
    applied_version: str | None
    recovered_pending: AdmissionRecoveryReport | None
    deleted: tuple[str, ...]
    deleted_bytes: int
    skipped: tuple[tuple[str, str], ...]
    corrupt: tuple[tuple[str, str], ...]
    audits: tuple[OrphanDeleteAudit, ...]
    trash_swept: int
    trash_residual_bytes: int
    blocked_reason: str | None
    stopped_by_lock_budget: bool


# {
#   責務: [LiveReferences: live sourceとextra_liveを1回だけ集めた結果]
#   フィールド: [references: 参照中のArtifact, digests: そのdigest集合, sources: 取得した保持者名,
#     problem: 不完全・失敗・期限超過の理由（Noneなら完全）]
# }
@dataclass(frozen=True)
class LiveReferences:
    references: tuple[ArtifactReference, ...]
    digests: frozenset[str]
    sources: tuple[str, ...]
    problem: str | None


# {
#   責務: [BudgetReclaimReport: 予算超過時の回収順序制御の結果を返す]
#   フィールド: [reconcile: orphan回収の結果（省略時None）, retention: collectの結果（止めた場合None）,
#     blocked_reason: collectを止めた理由（orphan_pressureなど）, remaining_unindexed_bytes: 回収後に残る未索引byte]
# }
@dataclass(frozen=True)
class BudgetReclaimReport:
    reconcile: OrphanReconcileReport | None
    retention: RetentionReport | None
    blocked_reason: str | None
    remaining_unindexed_bytes: int


# {
#   責務: [_Inspection: 1件のdirectoryを軽量検査した結果]
#   フィールド: [problem: 異常の理由（Noneなら正常）, size_bytes: 合計byte, artifact_type/sensitive: metadata由来,
#     last_activity_ms: 最終活動時刻, fingerprint: 変更検出用の組]
# }
@dataclass(frozen=True)
class _Inspection:
    problem: str | None
    size_bytes: int = 0
    artifact_type: str | None = None
    sensitive: bool | None = None
    last_activity_ms: int | None = None
    fingerprint: tuple[Any, ...] = ()


# {
#   責務: [_Scan: lock内で行った読取り走査の結果]
#   フィールド: [entries: 索引記録, pending_pairs: (admission_id, digest), pending_digests: pending record参照digest,
#     candidates: 暫定判定済みの候補, scan_start/scan_next: cursor, truncated: 1周しきれなかったか, residual: trash残存byte]
# }
@dataclass(frozen=True)
class _Scan:
    entries: tuple[Any, ...]
    pending_pairs: tuple[tuple[str, str], ...]
    candidates: tuple[OrphanCandidate, ...]
    scan_start: str | None
    scan_next: str | None
    truncated: bool
    residual: int


# {
#   責務: [OrphanReconciler: 未索引Artifactを状態分類し、安全なものだけをtrash経由で回収する（planは変更せず、applyだけが変更する）]
#   フィールド: [collector: 対象のRetentionCollector, admission: pending recordを扱うcoordinator,
#     live_sources: 参照を返す保持者, config: 猶予・上限, clock: ms時計, data_class_resolver: artifact_type→data class]
#   補足: [live sourceの取得と大きいpayloadのhashはroot lock外で行い、lock内は読取りと削除だけにして
#     admissionの5秒期限を妨げない。runtime配線は後続Issue（#225）]
# }
class OrphanReconciler:
    # {
    #   責務: [__init__: 依存と設定を固定する]
    #   処理: [live_sourcesが空でdeclared_no_live_sourcesもFalseなら拒否する。timer/rmtreeはテストで差し替える]
    #   引数: [collector: 対象, admission: pending回収用, live_sources: 参照保持者, config: 設定,
    #     clock: ms時計（省略時はcollectorと同じ）, data_class_resolver: 型→class, declared_no_live_sources: 参照者なしの明示宣言,
    #     timer: 単調時計, rmtree: trash掃除に使う削除関数]
    #   戻り値: [なし]
    #   エラー: [ValueError: live_sourcesが空で宣言もない]
    # }
    def __init__(
        self,
        collector: RetentionCollector,
        admission: ArtifactAdmissionCoordinator,
        live_sources: Iterable[LiveReferenceSource],
        *,
        config: OrphanReconcilerConfig | None = None,
        clock: Callable[[], int] | None = None,
        data_class_resolver: Callable[[str], str | None] | None = None,
        declared_no_live_sources: bool = False,
        timer: Callable[[], float] = time.monotonic,
        rmtree: Callable[[Path], None] | None = None,
    ) -> None:
        sources = tuple(live_sources)
        if not sources and not declared_no_live_sources:
            raise ValueError(
                "live_sources is empty; pass declared_no_live_sources=True to confirm nothing references the store"
            )
        self.collector = collector
        self.admission = admission
        self.live_sources = sources
        self.config = config if config is not None else OrphanReconcilerConfig()
        self.clock = clock if clock is not None else collector.clock
        self._resolver = data_class_resolver
        self._timer = timer
        self._rmtree = rmtree
        self._last_periodic_ms: int | None = None
        self.root = Path(collector.store.root)

    # {
    #   責務: [collect_live: live sourceとextra_liveを1回だけ集めて1つの集合にする]
    #   処理: [extra_liveを先にtupleへ実体化し、各sourceのsnapshotを順に取る。例外・不完全・型違反・合計期限超過は
    #     problemに理由を入れる（削除0にする根拠）。root lockは持たない]
    #   引数: [extra_live: 呼出側が追加で渡す参照（generator可）]
    #   戻り値: [LiveReferences: 参照・digest・保持者名・problem]
    # }
    def collect_live(
        self, extra_live: Iterable[ArtifactReference] = ()
    ) -> LiveReferences:
        references: list[ArtifactReference] = list(extra_live)
        names: list[str] = []
        problem: str | None = None
        started = self._timer()
        for source in self.live_sources:
            try:
                snapshot = source.snapshot()
            except Exception:  # noqa: BLE001 - 失敗した保持者の理由は問わず安全側（削除0）に倒す
                problem = "live_source_failed"
                break
            # adapterのLiveReferenceProvider（RetentionReferenceSnapshot）も、
            # complete/referencesを持つ値としてそのまま受け付ける（Issue #788との整合）。
            if getattr(snapshot, "complete", None) is not True or not isinstance(
                getattr(snapshot, "references", None), tuple
            ):
                problem = "live_snapshot_incomplete"
                break
            names.append(
                str(getattr(snapshot, "source", None) or type(source).__name__)
            )
            references.extend(snapshot.references)
            if (self._timer() - started) * 1000 > self.config.live_snapshot_timeout_ms:
                problem = "live_snapshot_timeout"
                break
        if problem is None and not all(
            isinstance(item, ArtifactReference) for item in references
        ):
            problem = "live_reference_invalid"
        digests = frozenset(
            item.sha256 for item in references if isinstance(item, ArtifactReference)
        )
        return LiveReferences(tuple(references), digests, tuple(names), problem)

    # {
    #   責務: [plan: dry-run。判定と削除予定を求め、objects・索引・pending・trash・cursorを変更しない]
    #   処理: [1: lock外でlive snapshot 2: lock内で索引・pending・trash残存・走査を読む（短時間）
    #     3: lock外で暫定safe_orphanのpayloadを逐次hash 4: graceと上限でtargetsを決める]
    #   引数: [trigger: TRIGGER_*, extra_live: 追加の参照（1回だけ実体化）]
    #   戻り値: [OrphanReconcilePlan: 判定結果。異常時はblocked_reason付きでtargets空]
    #   エラー: [ValueError: triggerが不正, RetentionLockTimeoutError: lock期限超過, RetentionRootUnsafeError: root/trashがlink等]
    # }
    def plan(
        self, trigger: str, *, extra_live: Iterable[ArtifactReference] = ()
    ) -> OrphanReconcilePlan:
        if trigger not in TRIGGERS:
            raise ValueError(f"unknown reconcile trigger: {trigger!r}")
        extra = tuple(extra_live)
        now = self._now()
        if not os.path.lexists(self.root):
            return self._empty_plan(trigger, now, None)
        if self._rate_limited(trigger, now):
            return self._empty_plan(trigger, now, "rate_limited")
        live = self.collect_live(extra)
        if live.problem is not None:
            return self._empty_plan(trigger, now, live.problem)
        scan_or_reason = self._read_scan(live, now)
        if isinstance(scan_or_reason, str):
            return self._empty_plan(trigger, now, scan_or_reason)
        scan = scan_or_reason
        candidates = self._verify_payloads(scan.candidates)
        targets = self._select_targets(candidates)
        return OrphanReconcilePlan(
            state_version=self._version(
                scan.entries, scan.pending_pairs, live, targets
            ),
            evaluated_at_ms=now,
            trigger=trigger,
            candidates=candidates,
            targets=targets,
            stale_staging=tuple(
                item.location for item in targets if item.kind == KIND_STAGING
            ),
            trash_residual_bytes=scan.residual,
            scan_start=scan.scan_start,
            scan_next=scan.scan_next,
            blocked_reason=None,
            truncated=scan.truncated,
        )

    # {
    #   責務: [apply: 計画の削除予定を、再検査したうえでtrash経由で回収する（変更はここだけ）]
    #   処理: [1: lock外でlive snapshotを取り直す 2: lock内でtrash sweep→recover_pending→索引/pending再読込→
    #     対象ごとに再検査して削除→cursor保存（保持はmax_lock_hold_ms以内） 3: 今回分とcrash前の未ack auditを返す。
    #     計画外の対象は決して追加しない]
    #   引数: [plan: planの結果, recover_pending: #789のcrash leftover回収を先に行うか, extra_live: 追加の参照]
    #   戻り値: [OrphanReconcileReport: 削除・保留・異常・audit]
    #   エラー: [RetentionLockTimeoutError: lock期限超過, RetentionRootUnsafeError: root/trashがlink等, OSError: trash/cursor書込み失敗]
    # }
    def apply(
        self,
        plan: OrphanReconcilePlan,
        *,
        recover_pending: bool = True,
        extra_live: Iterable[ArtifactReference] = (),
    ) -> OrphanReconcileReport:
        extra = tuple(extra_live)
        if plan.blocked_reason is not None or not os.path.lexists(self.root):
            return self._report(plan, blocked=plan.blocked_reason)
        live = self.collect_live(extra)
        with self.collector.coordination_lock():
            started = self._timer()
            budget_end = started + self.config.max_lock_hold_ms / 1000
            trash = self._trash()
            trash.ensure()
            swept = len(trash.sweep(deadline=budget_end, timer=self._timer).swept)
            recovered: AdmissionRecoveryReport | None = None
            blocked: str | None = None
            if recover_pending:
                try:
                    recovered = self.admission.recover_pending()
                except (
                    AdmissionRecordError,
                    RetentionIndexError,
                    RetentionSafetyError,
                    OSError,
                ):
                    blocked = "recover_pending_failed"
            now = self._now()
            if blocked is None and live.problem is not None:
                blocked = live.problem
            if (
                blocked is None
                and now < plan.evaluated_at_ms - self.config.future_skew_ms
            ):
                blocked = "clock_rollback"
            applied = _Applied()
            if blocked is None:
                blocked = self._apply_targets(
                    plan, live, now, budget_end, trash, applied
                )
            audits = self._audits(trash, applied.created)
            residual = trash.residual_bytes()
        return OrphanReconcileReport(
            plan_version=plan.state_version,
            applied_version=applied.version,
            recovered_pending=recovered,
            deleted=tuple(applied.deleted),
            deleted_bytes=applied.deleted_bytes,
            skipped=tuple(applied.skipped),
            corrupt=tuple(
                (item.location, item.reason)
                for item in plan.candidates
                if item.state == STATE_CORRUPT_OR_UNKNOWN
            ),
            audits=audits,
            trash_swept=swept,
            trash_residual_bytes=residual,
            blocked_reason=blocked,
            stopped_by_lock_budget=applied.stopped,
        )

    # {
    #   責務: [reconcile: planを求め、dry_runでなければapplyまで行う]
    #   処理: [extra_liveを1回だけ実体化し、同じ集合をplanとapplyに渡す。periodicは成功した実行時刻を記録する]
    #   引数: [trigger: TRIGGER_*, dry_run: Trueなら計画だけ, extra_live: 追加の参照]
    #   戻り値: [OrphanReconcileReport: dry_runでは何も削除しない報告（targetsはplanから得る）]
    #   エラー: [plan/applyと同じ]
    # }
    def reconcile(
        self,
        trigger: str,
        *,
        dry_run: bool = False,
        extra_live: Iterable[ArtifactReference] = (),
    ) -> OrphanReconcileReport:
        extra = tuple(extra_live)
        plan = self.plan(trigger, extra_live=extra)
        if dry_run:
            return self._report(plan, blocked=plan.blocked_reason)
        report = self.apply(plan, extra_live=extra)
        if trigger == TRIGGER_PERIODIC and plan.blocked_reason is None:
            self._last_periodic_ms = plan.evaluated_at_ms
        return report

    # {
    #   責務: [pending_audits: 呼出元が未永続化の削除auditを返す（crash後の分はrecovered=True）]
    #   処理: [root lock内でtrash intentを読み、未ackのauditを再生成する]
    #   引数: [なし]
    #   戻り値: [tuple: 未ackのOrphanDeleteAudit]
    #   エラー: [TrashIntentError: intent不正, RetentionLockTimeoutError: lock期限超過]
    # }
    def pending_audits(self) -> tuple[OrphanDeleteAudit, ...]:
        if not os.path.lexists(self.root):
            return ()
        with self.collector.coordination_lock():
            return self._audits(self._trash(), set())

    # {
    #   責務: [acknowledge: 永続化済みのauditを確認済みにする（再送は呼出側がaudit_idで除く）]
    #   処理: [root lock内でtrashのintentを確認済みにする]
    #   引数: [audit_ids: 永続化済みのaudit_id]
    #   戻り値: [なし]
    #   エラー: [TrashIntentError: ID形式不正・intent不正, RetentionLockTimeoutError: lock期限超過]
    # }
    def acknowledge(self, audit_ids: Iterable[str]) -> None:
        ids = tuple(audit_ids)
        if not os.path.lexists(self.root):
            return
        with self.collector.coordination_lock():
            self._trash().acknowledge(ids)

    # {
    #   責務: [_trash: 同じrootのArtifactTrashを作る]
    #   処理: [clockとrmtreeの差替えを引き継ぐ]
    #   引数: [なし]
    #   戻り値: [ArtifactTrash: このrootのtrash]
    # }
    def _trash(self) -> ArtifactTrash:
        if self._rmtree is None:
            return ArtifactTrash(self.root, clock=self.clock)
        return ArtifactTrash(self.root, clock=self.clock, rmtree=self._rmtree)

    # {
    #   責務: [_now: 差替可能な時計から判定時刻を取得し、契約外の値を拒否する]
    #   処理: [clockの戻り値がboolでない非負整数か確認する]
    #   引数: [なし]
    #   戻り値: [int: UNIXエポックms]
    #   エラー: [ValueError: clockの値が契約外]
    # }
    def _now(self) -> int:
        value = self.clock()
        if type(value) is not int or value < 0:
            raise ValueError("reconciler clock must return non-negative integer ms")
        return value

    # {
    #   責務: [_rate_limited: periodic契機が最小間隔以内か判定する]
    #   処理: [前回の成功時刻との差を最小間隔と比較する]
    #   引数: [trigger: 契機, now: 現在ms]
    #   戻り値: [bool: Trueなら実行しない]
    # }
    def _rate_limited(self, trigger: str, now: int) -> bool:
        return (
            trigger == TRIGGER_PERIODIC
            and self._last_periodic_ms is not None
            and now - self._last_periodic_ms < self.config.periodic_min_interval_ms
        )

    # {
    #   責務: [_empty_plan: 何も走査しない計画（rootなし・異常で停止）を作る]
    #   処理: [候補・削除予定を空にし、理由があればblocked_reasonに入れる]
    #   引数: [trigger: 契機, now: 評価時刻, reason: 停止理由（Noneなら正常な空）]
    #   戻り値: [OrphanReconcilePlan: 空の計画]
    # }
    def _empty_plan(
        self, trigger: str, now: int, reason: str | None
    ) -> OrphanReconcilePlan:
        return OrphanReconcilePlan(
            state_version="",
            evaluated_at_ms=now,
            trigger=trigger,
            candidates=(),
            targets=(),
            stale_staging=(),
            trash_residual_bytes=0,
            scan_start=None,
            scan_next=None,
            blocked_reason=reason,
            truncated=False,
        )

    # {
    #   責務: [_report: applyせずに返す報告（dry-runや停止済み計画）を作る]
    #   処理: [削除0・audit空で計画の診断だけを載せる]
    #   引数: [plan: 対象の計画, blocked: 停止理由]
    #   戻り値: [OrphanReconcileReport: 変更なしの報告]
    # }
    def _report(
        self, plan: OrphanReconcilePlan, *, blocked: str | None
    ) -> OrphanReconcileReport:
        return OrphanReconcileReport(
            plan_version=plan.state_version,
            applied_version=None,
            recovered_pending=None,
            deleted=(),
            deleted_bytes=0,
            skipped=(),
            corrupt=tuple(
                (item.location, item.reason)
                for item in plan.candidates
                if item.state == STATE_CORRUPT_OR_UNKNOWN
            ),
            audits=(),
            trash_swept=0,
            trash_residual_bytes=plan.trash_residual_bytes,
            blocked_reason=blocked,
            stopped_by_lock_budget=False,
        )

    # {
    #   責務: [_read_scan: lock内で索引・pending・trash残存・走査を短時間で読む（変更しない）]
    #   処理: [trash残存→索引→pending→cursor→shard走査→候補の軽量検査と暫定状態決定の順に行う]
    #   引数: [live: live集合, now: 評価時刻]
    #   戻り値: [_Scan | str: 走査結果。索引・pending・trashが不正ならblocked_reason]
    #   エラー: [RetentionLockTimeoutError: lock期限超過, RetentionRootUnsafeError: root/trashがlink等]
    # }
    def _read_scan(self, live: LiveReferences, now: int) -> _Scan | str:
        with self.collector.coordination_lock():
            trash = self._trash()
            try:
                residual = trash.residual_bytes()
            except TrashIntentError:
                return "trash_unreadable"
            try:
                entries = self.collector.load_entries()
            except RetentionIndexError:
                return "index_unreadable"
            try:
                statuses = self.admission.inspect_pending()
            except (AdmissionRecordError, RetentionIndexError):
                return "pending_unreadable"
            pending_pairs = tuple(
                sorted(
                    (item.record.admission_id, item.record.artifact_sha256)
                    for item in statuses
                )
            )
            pending_digests = frozenset(digest for _, digest in pending_pairs)
            indexed = frozenset(entry.artifact_sha256 for entry in entries)
            cursor = self._read_cursor(trash)
            keys = self._list_keys()
            start = bisect.bisect_right(keys, cursor) if cursor is not None else 0
            ordered = keys[start:] + keys[:start]
            candidates: list[OrphanCandidate] = []
            last: str | None = None
            processed = 0
            for key in ordered:
                if len(candidates) >= self.config.max_candidates:
                    break
                processed += 1
                last = key
                candidate = self._classify(key, indexed, pending_digests, live, now)
                if candidate is not None:
                    candidates.append(candidate)
            return _Scan(
                entries=tuple(entries),
                pending_pairs=pending_pairs,
                candidates=tuple(candidates),
                scan_start=cursor,
                scan_next=last,
                truncated=processed < len(ordered),
                residual=residual,
            )

    # {
    #   責務: [_list_keys: root配下のshardと中身の名前を「shard/名前」でソートして列挙する]
    #   処理: [2桁hexのshardだけを対象にし、link/junctionのshardは中身を辿らずshard自体を1件として列挙する]
    #   引数: [なし]
    #   戻り値: [list: ソート済みのkey]
    # }
    def _list_keys(self) -> list[str]:
        keys: list[str] = []
        for shard in sorted(os.listdir(self.root)):
            if _SHARD.fullmatch(shard) is None:
                continue
            path = self.root / shard
            if path.is_symlink() or is_junction(path) or not path.is_dir():
                keys.append(shard)
                continue
            keys.extend(f"{shard}/{name}" for name in sorted(os.listdir(path)))
        return sorted(keys)

    # {
    #   責務: [_classify: 1つのkeyを候補にするか判定し、暫定状態を決める]
    #   処理: [索引済みdigestは候補にしない。pending→live→整合性→graceの順で状態を決める]
    #   引数: [key: shard/名前, indexed: 索引済みdigest, pending_digests: pending record参照digest,
    #     live: live集合, now: 評価時刻]
    #   戻り値: [OrphanCandidate | None: 候補。索引済みならNone]
    # }
    def _classify(
        self,
        key: str,
        indexed: frozenset[str],
        pending_digests: frozenset[str],
        live: LiveReferences,
        now: int,
    ) -> OrphanCandidate | None:
        shard, _, name = key.partition("/")
        if not name:
            return self._corrupt(key, KIND_OBJECT, None, "shard_not_plain_directory")
        path = self.root / shard / name
        if name.startswith(_STAGING_PREFIX):
            inspection = self._inspect_staging(path)
            return self._decide(
                key, KIND_STAGING, None, inspection, now, REASON_STALE_STAGING
            )
        if _SHA256.fullmatch(name) is None or not name.startswith(shard):
            return self._corrupt(key, KIND_OBJECT, None, "unexpected_entry")
        if name in indexed:
            return None
        inspection = self._inspect_object(path, name)
        if name in pending_digests:
            return self._candidate(
                key,
                KIND_OBJECT,
                name,
                STATE_ACTIVE_PENDING,
                "pending_admission",
                inspection,
            )
        if name in live.digests:
            return self._candidate(
                key,
                KIND_OBJECT,
                name,
                STATE_LIVE_REFERENCED,
                "live_reference",
                inspection,
            )
        return self._decide(key, KIND_OBJECT, name, inspection, now, REASON_SAFE_ORPHAN)

    # {
    #   責務: [_decide: 整合性とgraceから、corrupt/recent/safe_orphanのいずれかを決める]
    #   処理: [異常ならcorrupt。最終活動が未来またはgrace未満ならrecent。それ以外は暫定safe_orphan]
    #   引数: [key/kind/digest: 対象, inspection: 軽量検査結果, now: 評価時刻, safe_reason: safe時の理由]
    #   戻り値: [OrphanCandidate: 判定済み候補]
    # }
    def _decide(
        self,
        key: str,
        kind: str,
        digest: str | None,
        inspection: _Inspection,
        now: int,
        safe_reason: str,
    ) -> OrphanCandidate:
        if inspection.problem is not None:
            return self._candidate(
                key,
                kind,
                digest,
                STATE_CORRUPT_OR_UNKNOWN,
                inspection.problem,
                inspection,
            )
        if not self._grace_elapsed(inspection.last_activity_ms, now):
            return self._candidate(
                key, kind, digest, STATE_RECENT_UNINDEXED, "within_grace", inspection
            )
        return self._candidate(
            key, kind, digest, STATE_SAFE_ORPHAN, safe_reason, inspection
        )

    # {
    #   責務: [_grace_elapsed: 最終活動からgraceが過ぎ、かつ未来時刻でないか判定する]
    #   処理: [最終活動が現在+許容skewより未来、またはgrace未満なら偽にする（最も若い年齢で保守的に判定）]
    #   引数: [last_activity_ms: 最終活動時刻, now: 現在ms]
    #   戻り値: [bool: Trueなら回収してよい経過時間]
    # }
    def _grace_elapsed(self, last_activity_ms: int | None, now: int) -> bool:
        if last_activity_ms is None:
            return False
        if last_activity_ms > now + self.config.future_skew_ms:
            return False
        return now - last_activity_ms >= self.config.grace_ms

    # {
    #   責務: [_candidate: 検査結果から候補を組み立てる]
    #   処理: [metadata由来の分類・byte・fingerprintを引き継ぐ]
    #   引数: [key/kind/digest/state/reason: 判定, inspection: 検査結果]
    #   戻り値: [OrphanCandidate: 候補]
    # }
    @staticmethod
    def _candidate(
        key: str,
        kind: str,
        digest: str | None,
        state: str,
        reason: str,
        inspection: _Inspection,
    ) -> OrphanCandidate:
        return OrphanCandidate(
            location=key,
            kind=kind,
            sha256=digest,
            state=state,
            reason=reason,
            size_bytes=inspection.size_bytes,
            artifact_type=inspection.artifact_type,
            sensitive=inspection.sensitive,
            last_activity_ms=inspection.last_activity_ms,
            fingerprint=inspection.fingerprint,
        )

    # {
    #   責務: [_corrupt: 検査できない対象をcorrupt_or_unknownの候補にする]
    #   処理: [byte・時刻は不明のまま理由だけを持つ候補を作る]
    #   引数: [key/kind/digest: 対象, reason: 異常理由]
    #   戻り値: [OrphanCandidate: corrupt候補]
    # }
    def _corrupt(
        self, key: str, kind: str, digest: str | None, reason: str
    ) -> OrphanCandidate:
        return self._candidate(
            key, kind, digest, STATE_CORRUPT_OR_UNKNOWN, reason, _Inspection(reason)
        )

    # {
    #   責務: [_inspect_object: object directoryを軽量検査する（payload全体のhashは含まない）]
    #   処理: [実directory・中身がpayload/metadata.jsonの通常fileだけ・metadata解析とdigest一致・payload sizeを確認し、
    #     最終活動時刻とfingerprintを採る]
    #   引数: [path: object directory, digest: 名前のdigest]
    #   戻り値: [_Inspection: 検査結果。異常はproblemに理由]
    # }
    def _inspect_object(self, path: Path, digest: str) -> _Inspection:
        shape = _plain_contents(path)
        if isinstance(shape, str):
            return _Inspection(shape)
        names, signatures, mtimes = shape
        if names != _ALLOWED_FILES:
            return _Inspection("unexpected_contents")
        try:
            raw = json.loads(
                read_regular_file(path / "metadata.json", max_bytes=_MAX_METADATA_BYTES)
            )
            metadata = ArtifactMetadata.from_dict(raw)
            created_ms = int(
                datetime.fromisoformat(metadata.created_at).timestamp() * 1000
            )
        except (OSError, ValueError):
            return _Inspection("metadata_invalid")
        if metadata.sha256 != digest:
            return _Inspection("digest_mismatch")
        payload_size = signatures["payload"][1]
        if payload_size != metadata.size_bytes:
            return _Inspection("size_mismatch")
        return _Inspection(
            None,
            size_bytes=payload_size + signatures["metadata.json"][1],
            artifact_type=metadata.artifact_type,
            sensitive=metadata.sensitive,
            last_activity_ms=max(created_ms, *mtimes),
            fingerprint=(
                signatures["."],
                signatures["metadata.json"],
                signatures["payload"],
            ),
        )

    # {
    #   責務: [_inspect_staging: .pending-* directoryを検査する]
    #   処理: [実directoryで中身がpayload/metadata.jsonの通常file部分集合（空可）だけを正常とし、最新mtimeを最終活動にする]
    #   引数: [path: staging directory]
    #   戻り値: [_Inspection: 検査結果]
    # }
    def _inspect_staging(self, path: Path) -> _Inspection:
        shape = _plain_contents(path)
        if isinstance(shape, str):
            return _Inspection(shape)
        names, signatures, mtimes = shape
        if not names <= _ALLOWED_FILES:
            return _Inspection("unexpected_contents")
        return _Inspection(
            None,
            size_bytes=sum(signatures[name][1] for name in names),
            last_activity_ms=max(mtimes),
            fingerprint=tuple(signatures[name] for name in sorted(signatures)),
        )

    # {
    #   責務: [_verify_payloads: 暫定safe_orphanのpayloadをlock外で逐次SHA-256検証する]
    #   処理: [走査順に64KiB単位でhashし、検証量が上限を超えたら以降はverification_deferredにする（1件目は必ず検証）。
    #     digest不一致・読込失敗・検証中のfingerprint変化はcorrupt_or_unknownにする]
    #   引数: [candidates: 暫定判定済みの候補]
    #   戻り値: [tuple: 検証結果を反映した候補]
    # }
    def _verify_payloads(
        self, candidates: tuple[OrphanCandidate, ...]
    ) -> tuple[OrphanCandidate, ...]:
        verified = 0
        result: list[OrphanCandidate] = []
        for item in candidates:
            if item.state != STATE_SAFE_ORPHAN or item.kind != KIND_OBJECT:
                result.append(item)
                continue
            if (
                verified > 0
                and verified + item.size_bytes > self.config.max_verify_bytes_per_run
            ):
                result.append(
                    replace(
                        item,
                        state=STATE_CORRUPT_OR_UNKNOWN,
                        reason="verification_deferred",
                    )
                )
                continue
            verified += item.size_bytes
            result.append(self._verify_one(item))
        return tuple(result)

    # {
    #   責務: [_verify_one: 1件のpayloadを検証し、結果を候補に反映する]
    #   処理: [payloadを読んでdigestを比較し、読込後にfingerprintが変わっていないことを確認する]
    #   引数: [item: 暫定safe_orphanのobject候補]
    #   戻り値: [OrphanCandidate: 検証後の候補]
    # }
    def _verify_one(self, item: OrphanCandidate) -> OrphanCandidate:
        assert item.sha256 is not None
        path = self.root / item.location
        hasher = hashlib.sha256()
        try:
            with open(path / "payload", "rb") as stream:
                while chunk := stream.read(_HASH_CHUNK_BYTES):
                    hasher.update(chunk)
            after = self._inspect_object(path, item.sha256)
        except OSError:
            return replace(
                item, state=STATE_CORRUPT_OR_UNKNOWN, reason="payload_unreadable"
            )
        if hasher.hexdigest() != item.sha256:
            return replace(
                item, state=STATE_CORRUPT_OR_UNKNOWN, reason="payload_digest_mismatch"
            )
        if after.fingerprint != item.fingerprint:
            return replace(
                item,
                state=STATE_CORRUPT_OR_UNKNOWN,
                reason="changed_during_verification",
            )
        return item

    # {
    #   責務: [_select_targets: 削除予定を、上限件数までobject→stagingの順に決める]
    #   処理: [safe_orphanだけを走査順に取り、max_delete_countで打切る]
    #   引数: [candidates: 検証済みの候補]
    #   戻り値: [tuple: 削除予定]
    # }
    def _select_targets(
        self, candidates: tuple[OrphanCandidate, ...]
    ) -> tuple[OrphanCandidate, ...]:
        safe = [item for item in candidates if item.state == STATE_SAFE_ORPHAN]
        ordered = [i for i in safe if i.kind == KIND_OBJECT] + [
            i for i in safe if i.kind == KIND_STAGING
        ]
        return tuple(ordered[: self.config.max_delete_count])

    # {
    #   責務: [_version: 判定入力のhash（評価時刻は含めない）を求める]
    #   処理: [索引・pending・live・設定・削除予定(位置+fingerprint)を正規化JSONにしてSHA-256を取る]
    #   引数: [entries: 索引記録, pending_pairs: pendingの(id,digest), live: live集合, targets: 削除予定]
    #   戻り値: [str: 64桁hex]
    # }
    def _version(
        self,
        entries: Iterable[Any],
        pending_pairs: Iterable[tuple[str, str]],
        live: LiveReferences,
        targets: Iterable[Any],
    ) -> str:
        target_keys = sorted(
            (item.location, item.fingerprint)
            if isinstance(item, OrphanCandidate)
            else item
            for item in targets
        )
        document = {
            "entries": sorted(
                (json.dumps(asdict(entry), sort_keys=True) for entry in entries)
            ),
            "pending": sorted(pending_pairs),
            "live": sorted(live.digests),
            "sources": sorted(live.sources),
            "config": asdict(self.config),
            "targets": target_keys,
        }
        encoded = json.dumps(document, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # {
    #   責務: [_read_cursor: 前回の走査位置を読む（無い・不正なら先頭から）]
    #   処理: [trashのcursor fileを小さい上限で読み、形式が合うkeyだけを採用する]
    #   引数: [trash: このrootのtrash]
    #   戻り値: [str | None: 最後に見たkey]
    # }
    def _read_cursor(self, trash: ArtifactTrash) -> str | None:
        path = trash.directory / CURSOR_FILENAME
        if not trash.exists() or not os.path.lexists(path):
            return None
        try:
            value = json.loads(read_regular_file(path, max_bytes=1024))
        except (OSError, ValueError):
            return None
        last = value.get("last") if isinstance(value, dict) else None
        if isinstance(last, str) and _CURSOR_KEY.fullmatch(last):
            return last
        return None

    # {
    #   責務: [_write_cursor: 次回の走査位置を保存する]
    #   処理: [stage_json_writeで原子的に置換する]
    #   引数: [trash: このrootのtrash, last: 保存するkey]
    #   戻り値: [なし]
    #   エラー: [OSError: 書込み・置換失敗]
    # }
    def _write_cursor(self, trash: ArtifactTrash, last: str) -> None:
        staged = stage_json_write(
            trash.directory / CURSOR_FILENAME, {"schema_version": 1, "last": last}
        )
        try:
            staged.publish()
        finally:
            staged.discard()

    # {
    #   責務: [_apply_targets: lock内で対象を再検査して削除し、cursorを保存する]
    #   処理: [索引とpendingを読み直し、計画の各対象をindexed/pending/live/fingerprint/graceで再検査する。
    #     通った対象だけをlock保持上限の範囲で削除する（計画外は追加しない）]
    #   引数: [plan: 計画, live: live集合, now: 現在ms, budget_end: lock保持の期限, trash: trash, applied: 結果の蓄積先]
    #   戻り値: [str | None: 全体を止めた理由]
    # }
    def _apply_targets(
        self,
        plan: OrphanReconcilePlan,
        live: LiveReferences,
        now: int,
        budget_end: float,
        trash: ArtifactTrash,
        applied: _Applied,
    ) -> str | None:
        try:
            entries = self.collector.load_entries()
            statuses = self.admission.inspect_pending()
        except RetentionIndexError:
            return "index_unreadable"
        except AdmissionRecordError:
            return "pending_unreadable"
        indexed = frozenset(entry.artifact_sha256 for entry in entries)
        pending_pairs = [
            (item.record.admission_id, item.record.artifact_sha256) for item in statuses
        ]
        pending_digests = frozenset(digest for _, digest in pending_pairs)
        eligible: list[OrphanCandidate] = []
        fresh: list[tuple[str, Any]] = []
        for target in plan.targets:
            reason, fingerprint = self._recheck(
                target, indexed, pending_digests, live.digests, now
            )
            fresh.append((target.location, fingerprint))
            if reason is None:
                eligible.append(target)
            else:
                applied.skipped.append((target.location, reason))
        applied.version = self._version(entries, pending_pairs, live, fresh)
        for target in eligible:
            if len(applied.deleted) >= self.config.max_delete_count:
                break
            if self._timer() >= budget_end:
                applied.stopped = True
                break
            self._discard(target, plan.trigger, trash, applied)
        if plan.scan_next is not None:
            self._write_cursor(trash, plan.scan_next)
        return None

    # {
    #   責務: [_recheck: 削除直前に1件を再検査する]
    #   処理: [索引・pending・liveに無いこと、配置が正規であること、fingerprint一致、現在時刻でgrace経過を確認する]
    #   引数: [target: 計画の対象, indexed/pending_digests/live_digests: 現在の集合, now: 現在ms]
    #   戻り値: [tuple: (除外理由 or None, 現在のfingerprint or None)]
    # }
    def _recheck(
        self,
        target: OrphanCandidate,
        indexed: frozenset[str],
        pending_digests: frozenset[str],
        live_digests: frozenset[str],
        now: int,
    ) -> tuple[str | None, Any]:
        if target.kind == KIND_OBJECT:
            assert target.sha256 is not None
            if target.sha256 in indexed:
                return "indexed", None
            if target.sha256 in pending_digests:
                return "pending", None
            if target.sha256 in live_digests:
                return "live", None
            try:
                path = self.collector.safe_object_path(target.sha256)
            except RetentionSafetyError:
                return "unsafe_path", None
            inspection = self._inspect_object(path, target.sha256)
        else:
            path = self.root / target.location
            inspection = self._inspect_staging(path)
        if (
            inspection.problem is not None
            or inspection.fingerprint != target.fingerprint
        ):
            return "changed_since_plan", inspection.fingerprint or None
        if not self._grace_elapsed(inspection.last_activity_ms, now):
            return "within_grace", inspection.fingerprint
        return None, inspection.fingerprint

    # {
    #   責務: [_discard: 再検査済みの1件をtrash経由で削除し、結果を蓄積する]
    #   処理: [分類とdata classをintentへ入れて論理削除する。rename失敗などはskippedにして他の対象を続ける]
    #   引数: [target: 対象, trigger: 契機, trash: trash, applied: 結果の蓄積先]
    #   戻り値: [なし]
    # }
    def _discard(
        self,
        target: OrphanCandidate,
        trigger: str,
        trash: ArtifactTrash,
        applied: _Applied,
    ) -> None:
        try:
            if target.kind == KIND_OBJECT:
                assert target.sha256 is not None
                source = self.collector.safe_object_path(target.sha256)
                intent = trash.discard(
                    source,
                    kind=KIND_OBJECT,
                    sha256=target.sha256,
                    reason=REASON_SAFE_ORPHAN,
                    trigger=trigger,
                    artifact_type=target.artifact_type,
                    sensitive=target.sensitive,
                    data_class=self._data_class(target.artifact_type),
                )
            else:
                intent = trash.discard(
                    self.root / target.location,
                    kind=KIND_STAGING,
                    sha256=None,
                    reason=REASON_STALE_STAGING,
                    trigger=trigger,
                )
        except (OSError, RetentionSafetyError, ValueError):
            applied.skipped.append((target.location, "discard_failed"))
            return
        applied.created.add(intent.id)
        applied.deleted.append(target.location)
        applied.deleted_bytes += target.size_bytes

    # {
    #   責務: [_data_class: artifact_typeに対応するdata classを引く]
    #   処理: [注入されたresolverか、既定でdata inventoryを使う。未登録や失敗はNone]
    #   引数: [artifact_type: 型名（Noneなら不明）]
    #   戻り値: [str | None: data class]
    # }
    def _data_class(self, artifact_type: str | None) -> str | None:
        if artifact_type is None:
            return None
        if self._resolver is not None:
            return self._resolver(artifact_type)
        return load_data_inventory().artifact_types.get(artifact_type)

    # {
    #   責務: [_audits: trashのintentから未ackのauditを再生成する]
    #   処理: [今回作ったintent以外はrecovered=Trueにする]
    #   引数: [trash: trash, created: 今回のapplyで作ったintent ID]
    #   戻り値: [tuple: audit]
    #   エラー: [TrashIntentError: intent不正]
    # }
    @staticmethod
    def _audits(
        trash: ArtifactTrash, created: set[str]
    ) -> tuple[OrphanDeleteAudit, ...]:
        return tuple(
            _audit_from(intent, recovered=intent.id not in created)
            for intent in trash.audit_intents()
        )


# {
#   責務: [_Applied: applyの途中結果を1つの所有者にまとめる可変値]
#   フィールド: [deleted: 削除した位置, deleted_bytes: 合計byte, skipped: 保留(位置,理由), created: 今回作ったintent ID,
#     version: 適用時のstate_version, stopped: lock保持上限で打切ったか]
# }
@dataclass
class _Applied:
    deleted: list[str] = field(default_factory=list)
    deleted_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    created: set[str] = field(default_factory=set)
    version: str | None = None
    stopped: bool = False


# {
#   責務: [_audit_from: trash intentから監査項目を作る]
#   処理: [path・payloadを含めずに分類とbyte・理由だけを写す]
#   引数: [intent: 対象intent, recovered: crash前の分か]
#   戻り値: [OrphanDeleteAudit: 監査項目]
# }
def _audit_from(intent: TrashIntent, *, recovered: bool) -> OrphanDeleteAudit:
    return OrphanDeleteAudit(
        audit_id=intent.id,
        sha256=intent.sha256,
        kind=intent.kind,
        artifact_type=intent.artifact_type,
        data_class=intent.data_class,
        sensitive=intent.sensitive,
        size_bytes=intent.size_bytes,
        reason=intent.reason,
        trigger=intent.trigger,
        deleted_at_ms=intent.deleted_at_ms,
        recovered=recovered,
    )


# {
#   責務: [_plain_contents: directoryがlinkでない実directoryで、中身が通常fileだけかをlstatで確認し署名を採る]
#   処理: [directory自体と各fileのlstatからfingerprint用の署名とmtime(ms)を集める。異常は理由文字列を返す]
#   引数: [path: 検査するdirectory]
#   戻り値: [tuple | str: (名前集合, 名前→署名（"."はdirectory）, mtime(ms)の列) または異常理由]
# }
def _plain_contents(
    path: Path,
) -> tuple[frozenset[str], dict[str, tuple[Any, ...]], list[int]] | str:
    try:
        info = os.lstat(path)
        if path.is_symlink() or is_junction(path) or not stat.S_ISDIR(info.st_mode):
            return "not_plain_directory"
        signatures: dict[str, tuple[Any, ...]] = {".": _signature(info)}
        mtimes = [info.st_mtime_ns // 1_000_000]
        names = frozenset(os.listdir(path))
        for name in sorted(names):
            child = os.lstat(path / name)
            if (
                not stat.S_ISREG(child.st_mode)
                or (path / name).is_symlink()
                or is_junction(path / name)
            ):
                return "not_regular_file"
            signatures[name] = _signature(child)
            mtimes.append(child.st_mtime_ns // 1_000_000)
    except OSError:
        return "unreadable"
    return names, signatures, mtimes


# {
#   責務: [_signature: lstat結果をfingerprint用の(ino,size,mtime_ns,ctime_ns)にする]
#   処理: [4値を取り出す（Windowsのst_inoはfile index）]
#   引数: [info: os.lstatの結果]
#   戻り値: [tuple: 変更検出用の組]
# }
def _signature(info: os.stat_result) -> tuple[Any, ...]:
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# {
#   責務: [reclaim_budget: 予算超過時に、正常な索引dataを消す前にorphanを回収する順序を制御する]
#   処理: [1: live集合を1回だけ作る 2: soft budget超過かつ未索引byteがあればlock外でreconcile(BUDGET)
#     3: lock内でmeasureを取り直し、orphan分の上乗せで正常entryが消えるならcollectせずorphan_pressureを報告、
#     そうでなければ同じlock内でcollectする。直接collectの挙動(#164)は変えない]
#   引数: [reconciler: 対象, extra_live: 追加の参照]
#   戻り値: [BudgetReclaimReport: 回収結果。live不完全はblocked_reasonで何もしない]
#   エラー: [RetentionIndexError: 索引・metadata不正, RetentionLockTimeoutError: lock期限超過]
# }
def reclaim_budget(
    reconciler: OrphanReconciler, *, extra_live: Iterable[ArtifactReference] = ()
) -> BudgetReclaimReport:
    collector = reconciler.collector
    live = reconciler.collect_live(tuple(extra_live))
    if live.problem is not None:
        return BudgetReclaimReport(None, None, live.problem, 0)
    before = collector.measure()
    reconcile_report: OrphanReconcileReport | None = None
    if (
        before.usage.total_bytes > collector.policy.soft_budget_bytes
        and before.usage.unindexed_bytes > 0
    ):
        reconcile_report = reconciler.reconcile(
            TRIGGER_BUDGET, extra_live=live.references
        )
    with collector.coordination_lock():
        snapshot = collector.measure()
        now = reconciler._now()
        actual = plan_retention(
            snapshot.entries,
            collector.policy,
            now_ms=now,
            live_artifact_sha256=live.digests,
            unindexed_bytes=snapshot.usage.unindexed_bytes,
        )
        baseline = plan_retention(
            snapshot.entries,
            collector.policy,
            now_ms=now,
            live_artifact_sha256=live.digests,
            unindexed_bytes=0,
        )
        extra_removed = {item.entry_id for item in actual.removed_entries} - {
            item.entry_id for item in baseline.removed_entries
        }
        if extra_removed:
            return BudgetReclaimReport(
                reconcile_report,
                None,
                "orphan_pressure",
                snapshot.usage.unindexed_bytes,
            )
        retention = collector.collect(live.references)
    return BudgetReclaimReport(
        reconcile_report, retention, None, snapshot.usage.unindexed_bytes
    )
