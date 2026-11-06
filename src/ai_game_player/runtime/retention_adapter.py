"""Session/Journal/Experienceの参照snapshotとbounded triggerで、RetentionCollectorを本番Sessionへ安全に接続する。"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_game_player.artifact_admission import (
    ArtifactAdmissionCoordinator,
    ArtifactAdmissionRequest,
    AdmissionWriter,
)
from ai_game_player.artifact_store import ArtifactIntegrityError
from ai_game_player.event_journal import MAX_REFERENCE_BATCH_LIMIT, EventJournal
from ai_game_player.experience import ArtifactReference, ExperienceEpisode
from ai_game_player.experience_reader import (
    ExperienceRecoveryError,
    iter_artifact_references,
    migrate_experience_dict,
)
from ai_game_player.retention_collector import (
    RetentionCollector,
    RetentionReport,
    RetentionSafetyError,
    current_time_ms,
)
from ai_game_player.retention_index import RetentionIndexError
from ai_game_player.retention_root_lock import (
    RetentionLockTimeoutError,
    RetentionRootUnsafeError,
    retention_root_lock,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPERIENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MILLISECONDS_PER_SECOND = 1000.0
DEFAULT_EXPERIENCE_MAX_BYTES = 64 * 1024 * 1024
TRACE_EVENT_TYPE = "retention.gc/v1"
# 削除判断に使った処理ではないため、Journalへ記録しないskip理由（件数はstatsだけに数える）
_UNTRACED_SKIPS = frozenset(
    {"not_due", "coalesced", "already_running", "closed", "store_absent"}
)

PressureLevel = Literal["none", "over_budget", "over_hard_limit"]


# {
#   責務: [RetentionReferenceError: 参照を書く条件（gate内・object存在）を満たさず、Journal/Experienceへ書かせなかったことを通知する]
#   フィールド: [なし: RuntimeErrorの理由文だけを持つ]
# }
class RetentionReferenceError(RuntimeError):
    """A reference was not written inside recording(), or its object is gone."""


# {
#   責務: [RetentionTrigger: GCを起動する理由を列挙する]
#   フィールド: [soft_budget_crossing/persist_count/periodic: 条件付きのtrigger,
#     session_close/maintenance: 最短間隔と条件を無条件に迂回するtrigger]
# }
class RetentionTrigger(str, Enum):
    SOFT_BUDGET_CROSSING = "soft_budget_crossing"
    PERSIST_COUNT = "persist_count"
    PERIODIC = "periodic"
    SESSION_CLOSE = "session_close"
    MAINTENANCE = "maintenance"


_FORCED_TRIGGERS = frozenset(
    {RetentionTrigger.SESSION_CLOSE, RetentionTrigger.MAINTENANCE}
)


# {
#   責務: [_require_int: 範囲付き整数（bool除外）を検査する]
#   処理: [型と下限を確認し、違反は値名付きのValueErrorにする]
#   引数: [value: 検査値, name: エラー文の項目名, minimum: 下限, maximum: 上限（省略可）]
#   戻り値: [なし]
#   エラー: [ValueError: 整数でない・範囲外]
# }
def _require_int(
    value: object, name: str, minimum: int, maximum: int | None = None
) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")


# {
#   責務: [_require_seconds: 有限の正の秒数を検査する]
#   処理: [bool・非数・非有限・0以下を拒否する]
#   引数: [value: 検査値, name: エラー文の項目名]
#   戻り値: [なし]
#   エラー: [ValueError: 正の有限数でない]
# }
def _require_seconds(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not value > 0
    ):
        raise ValueError(f"{name} must be a positive finite number of seconds")


# {
#   責務: [RetentionTriggerPolicy: GCの起動条件・最短間隔・1回の作業上限・待ち上限を不変値として固定する]
#   フィールド: [min_interval_ms: 前回GC開始からこの間はforced以外をcoalesce, persist_count_threshold: 前回GC以降の保存件数の起動閾値,
#     periodic_interval_ms: 背景workerの周期, max_deleted_artifacts_per_run: 1回で物理削除するobject数の上限,
#     trigger_lock_timeout_seconds/close_lock_timeout_seconds: trigger時・close時のlock待ち上限,
#     snapshot_max_age_ms: snapshot作成からcollect判定までの許容時間, journal_batch_limit: 1 snapshotで読むJournal event数]
# }
@dataclass(frozen=True)
class RetentionTriggerPolicy:
    min_interval_ms: int = 5_000
    persist_count_threshold: int = 64
    periodic_interval_ms: int = 300_000
    max_deleted_artifacts_per_run: int = 256
    trigger_lock_timeout_seconds: float = 0.5
    close_lock_timeout_seconds: float = 5.0
    snapshot_max_age_ms: int = 2_000
    journal_batch_limit: int = 10_000

    # {
    #   責務: [__post_init__: 矛盾する間隔・上限・待ち時間のpolicyを生成時点で拒否する]
    #   処理: [整数は下限、秒数は有限の正数、batch上限はJournalの上限以内であることを確認する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [ValueError: いずれかの値が契約外]
    # }
    def __post_init__(self) -> None:
        _require_int(self.min_interval_ms, "min_interval_ms", 0)
        _require_int(self.persist_count_threshold, "persist_count_threshold", 1)
        _require_int(self.periodic_interval_ms, "periodic_interval_ms", 1)
        _require_int(
            self.max_deleted_artifacts_per_run, "max_deleted_artifacts_per_run", 1
        )
        _require_int(self.snapshot_max_age_ms, "snapshot_max_age_ms", 0)
        _require_int(
            self.journal_batch_limit,
            "journal_batch_limit",
            1,
            MAX_REFERENCE_BATCH_LIMIT,
        )
        _require_seconds(
            self.trigger_lock_timeout_seconds, "trigger_lock_timeout_seconds"
        )
        _require_seconds(self.close_lock_timeout_seconds, "close_lock_timeout_seconds")


# {
#   責務: [RetentionReferenceSnapshot: GCへ渡す参照集合を、取得時点・世代・完全性と一緒に不変値として表す]
#   フィールド: [session_id: adapterのSessionと一致すべきID, generation: 参照を持つ変化を観測するたびに単調増加する値,
#     created_at_ms: snapshot作成時刻（UNIXエポックms）, artifact_sha256s: 参照中digest, references: collectへ渡す参照（digest昇順・重複なし）,
#     source_positions: source名→読取位置, complete: 全sourceを最後まで読めたか, incomplete_reasons: 不完全な理由]
# }
@dataclass(frozen=True)
class RetentionReferenceSnapshot:
    session_id: str
    generation: int
    created_at_ms: int
    artifact_sha256s: frozenset[str]
    references: tuple[ArtifactReference, ...]
    source_positions: Mapping[str, str]
    complete: bool
    incomplete_reasons: tuple[str, ...] = ()

    # {
    #   責務: [__post_init__: 型・digest集合の一致・並び順を検証し、位置情報を読取専用にする]
    #   処理: [referencesのdigestがartifact_sha256sと一致し、昇順で重複しないことを確認する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [ValueError: 型・値・digest集合が契約外]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        _require_int(self.generation, "generation", 0)
        _require_int(self.created_at_ms, "created_at_ms", 0)
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if any(not isinstance(item, ArtifactReference) for item in self.references):
            raise ValueError("references must contain ArtifactReference values")
        digests = [item.sha256 for item in self.references]
        if digests != sorted(set(digests)):
            raise ValueError("references must be unique and sorted by digest")
        if frozenset(digests) != frozenset(self.artifact_sha256s):
            raise ValueError("artifact_sha256s must equal the reference digests")
        if not self.complete and not self.incomplete_reasons:
            raise ValueError("an incomplete snapshot must name its reasons")
        object.__setattr__(
            self, "source_positions", dict(sorted(self.source_positions.items()))
        )
        object.__setattr__(self, "incomplete_reasons", tuple(self.incomplete_reasons))


# {
#   責務: [SourceReading: 1つの参照sourceが1回読んだ結果（参照・位置・完全性・変化回数）を表す]
#   フィールド: [name: source名, references: 読めた参照, position: 読取位置の文字列, complete: 最後まで読めたか,
#     reasons: 不完全な理由, changes: 参照を持つ変化を観測した累積回数]
# }
@dataclass(frozen=True)
class SourceReading:
    name: str
    references: tuple[ArtifactReference, ...]
    position: str
    complete: bool
    reasons: tuple[str, ...]
    changes: int


# {
#   責務: [ReferenceSource: Composite providerが合成する参照sourceの読取契約]
#   フィールド: [name: source名]
# }
class ReferenceSource(Protocol):
    name: str

    # {
    #   責務: [read: 現在の参照集合を前回位置からの増分または再読込で返す]
    #   処理: [実装ごとに有界で読み、不完全なら理由を付ける]
    #   引数: [なし]
    #   戻り値: [SourceReading: 読取結果]
    #   エラー: [Exception: 読取不能（snapshot失敗として扱われる）]
    # }
    def read(self) -> SourceReading: ...


# {
#   責務: [LiveReferenceProvider: adapterがGC直前に呼ぶ、Sessionの参照snapshot取得契約]
#   フィールド: [なし]
# }
class LiveReferenceProvider(Protocol):
    # {
    #   責務: [snapshot: 現在の参照集合を取得する]
    #   処理: [実装ごとに全sourceを読み、世代と完全性を付けて返す]
    #   引数: [なし]
    #   戻り値: [RetentionReferenceSnapshot: 取得結果]
    #   エラー: [Exception: 取得不能（adapterはsnapshot_failedとして削除しない）]
    # }
    def snapshot(self) -> RetentionReferenceSnapshot: ...


# {
#   責務: [JournalReferenceSource: Event Journalの参照を、前回位置からの増分だけで索引化して返す]
#   フィールド: [name: "journal", _journal: 読取元, _batch_limit: 1回の読取event数上限, _cursor: 読み終えた最後のsequence,
#     _refs: digest→最初の参照, _counts: digest→参照数, _changes: 参照付きbatchを取り込んだ回数]
#   補足: [Journalはappend-onlyなので参照は消えない。毎回の計算量は前回からの増分]
# }
class JournalReferenceSource:
    name = "journal"

    # {
    #   責務: [__init__: 読取元Journalと1回の読取上限を固定する]
    #   処理: [位置は0（先頭）から始める]
    #   引数: [journal: 読取元EventJournal, batch_limit: 1 snapshotで読むevent数の上限]
    #   戻り値: [なし]
    #   エラー: [ValueError: batch_limitが範囲外]
    # }
    def __init__(self, journal: EventJournal, *, batch_limit: int) -> None:
        _require_int(batch_limit, "batch_limit", 1, MAX_REFERENCE_BATCH_LIMIT)
        self._journal = journal
        self._batch_limit = batch_limit
        self._lock = threading.Lock()
        self._cursor = 0
        self._refs: dict[str, ArtifactReference] = {}
        self._counts: dict[str, int] = {}
        self._changes = 0

    # {
    #   責務: [reference_count: 取り込み済みのdigestの参照数を返す]
    #   処理: [索引を引く]
    #   引数: [digest: 対象digest]
    #   戻り値: [int: 参照数。未参照は0]
    # }
    def reference_count(self, digest: str) -> int:
        with self._lock:
            return self._counts.get(digest, 0)

    # {
    #   責務: [read: 前回位置から最大batch_limit件のeventを読み、参照索引へ取り込む]
    #   処理: [artifact_refs_afterで参照だけを増分取得し、位置を読んだ最後のsequenceへ進める。読み切れなければ不完全にする]
    #   引数: [なし]
    #   戻り値: [SourceReading: 累積の参照・位置seq=N・完全性]
    #   エラー: [EventJournalError系: 破損行・close済み]
    # }
    def read(self) -> SourceReading:
        with self._lock:
            batch = self._journal.artifact_refs_after(
                self._cursor, limit=self._batch_limit
            )
            if batch.refs:
                for reference in batch.refs:
                    self._refs.setdefault(reference.sha256, reference)
                    self._counts[reference.sha256] = (
                        self._counts.get(reference.sha256, 0) + 1
                    )
                self._changes += 1
            self._cursor = batch.last_sequence
            reasons = () if batch.exhausted else ("journal_catchup_pending",)
            return SourceReading(
                self.name,
                tuple(self._refs.values()),
                f"seq={self._cursor}",
                batch.exhausted,
                reasons,
                self._changes,
            )


# {
#   責務: [_StatIdentity: Experience fileの同一性確認に使うstat値（device・inode・size・mtime_ns）]
#   フィールド: [values: os.statから取った4つの値]
# }
def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    info = os.stat(path)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


# {
#   責務: [_parse_experience: Experience JSONLのbytesからArtifact参照を、JsonlExperienceReaderと同じ規則で取り出す]
#   処理: [各行をepisodeとして検証し、末尾の破損行だけを切り捨て（recovered_tail）、途中の破損と重複episodeは失敗にする]
#   引数: [raw: file全体のbytes]
#   戻り値: [tuple: digest→最初の参照, 末尾を切り捨てたか]
#   エラー: [ExperienceRecoveryError: 途中の破損行・不正episode・重複episode ID]
# }
def _parse_experience(raw: bytes) -> tuple[dict[str, ArtifactReference], bool]:
    references: dict[str, ArtifactReference] = {}
    episode_ids: set[str] = set()
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            if index == len(lines) - 1:
                return references, True
            raise ExperienceRecoveryError(
                f"invalid interior record at line {index + 1}"
            ) from error
        try:
            if not isinstance(value, Mapping):
                raise ValueError("episode record must be an object")
            episode = ExperienceEpisode.from_dict(migrate_experience_dict(value))
        except (TypeError, ValueError) as error:
            raise ExperienceRecoveryError(
                f"invalid episode at line {index + 1}"
            ) from error
        if episode.episode_id in episode_ids:
            raise ExperienceRecoveryError("duplicate episode ID")
        episode_ids.add(episode.episode_id)
        for reference in iter_artifact_references(episode):
            references.setdefault(reference.sha256, reference)
    return references, False


# {
#   責務: [ExperienceReferenceSource: Experience JSONLの参照を、writer契約のcontent_versionで変化検出して返す]
#   フィールド: [name: "experience:<name>", _path: 読取元, _max_bytes: 読取上限, _content_version: recordingが加算する版,
#     _cached*: 前回の完全な読取結果と、その時の版・stat, _changes: 参照集合が変わった回数]
#   補足: [writer契約: append-onlyまたはatomic replaceで、参照付き書込みはrecording(target="experience:<name>")内で行う。
#     statは補助で、変化の検出はcontent_versionが担う]
# }
class ExperienceReferenceSource:
    # {
    #   責務: [__init__: 読取元fileとsource名・読取上限を固定する]
    #   処理: [名前の形式を検査し、版と変化回数を0にする]
    #   引数: [name: recordingのtargetと対応する名前, path: Experience JSONL, max_bytes: 読取上限byte]
    #   戻り値: [なし]
    #   エラー: [ValueError: 名前・上限が契約外]
    # }
    def __init__(
        self, name: str, path: Path, *, max_bytes: int = DEFAULT_EXPERIENCE_MAX_BYTES
    ) -> None:
        if not isinstance(name, str) or _EXPERIENCE_NAME.fullmatch(name) is None:
            raise ValueError("experience source name is not a plain identifier")
        _require_int(max_bytes, "max_bytes", 1)
        self.name = f"experience:{name}"
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._content_version = 0
        self._cached: SourceReading | None = None
        self._cached_key: tuple[int, tuple[int, int, int, int]] | None = None
        self._digests: frozenset[str] = frozenset()
        self._changes = 0
        self._had_file = False

    # {
    #   責務: [mark_changed: 参照を書いたwriterの完了を記録し、次回は必ず読み直させる]
    #   処理: [content_versionを1増やす]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def mark_changed(self) -> None:
        with self._lock:
            self._content_version += 1

    # {
    #   責務: [read: Experience fileから参照を読む（版とstatが前回と同じなら再利用する）]
    #   処理: [1つのfile handleで上限まで読み、読取前後のstat差・末尾切捨て・不在を不完全として理由を付ける。
    #     内容の解析に失敗したら例外にする]
    #   引数: [なし]
    #   戻り値: [SourceReading: 参照・位置size/mtime_ns/sha256・完全性]
    #   エラー: [ExperienceRecoveryError: 途中破損, ValueError: 上限超過, OSError: 読取失敗]
    # }
    def read(self) -> SourceReading:
        with self._lock:
            version = self._content_version
            try:
                before = _stat_identity(self._path)
            except FileNotFoundError:
                self._cached = None
                self._cached_key = None
                if not self._had_file:
                    return SourceReading(
                        self.name, (), "absent", True, (), self._changes
                    )
                return SourceReading(
                    self.name,
                    (),
                    "absent",
                    False,
                    ("experience_missing",),
                    self._changes,
                )
            if self._cached is not None and self._cached_key == (version, before):
                return self._cached
            with open(self._path, "rb") as handle:
                raw = handle.read(self._max_bytes + 1)
            if len(raw) > self._max_bytes:
                raise ValueError("experience file exceeds the read limit")
            after = _stat_identity(self._path)
            references, recovered_tail = _parse_experience(raw)
            self._had_file = True
            digests = frozenset(references)
            if digests != self._digests:
                self._digests = digests
                self._changes += 1
            reasons: list[str] = []
            if recovered_tail:
                reasons.append("experience_recovered_tail")
            if after != before:
                reasons.append("experience_changed_during_read")
            position = (
                f"size={len(raw)};mtime_ns={after[3]};"
                f"sha256={hashlib.sha256(raw).hexdigest()}"
            )
            reading = SourceReading(
                self.name,
                tuple(references.values()),
                position,
                not reasons,
                tuple(reasons),
                self._changes,
            )
            self._cached = reading if not reasons else None
            self._cached_key = (version, before) if not reasons else None
            return reading


# {
#   責務: [PinToken: pinした1件を識別する不変値（release用）]
#   フィールド: [token_id: 単調増加のID, sha256: pinしたdigest, reason: 診断用の短い理由]
# }
@dataclass(frozen=True)
class PinToken:
    token_id: int
    sha256: str
    reason: str


# {
#   責務: [PinnedReferenceSource: 保存途中・Replay再生中など、まだJournalに書かれていない参照中digestをtoken単位で保持する]
#   フィールド: [name: "pins", _tokens: token_id→PinToken, _version: pin/release/clearのたびに増える値, _lock: 葉のlock]
#   補足: [_lockは保持中に他のlockを取らない]
# }
class PinnedReferenceSource:
    name = "pins"

    # {
    #   責務: [__init__: 空のpin集合を作る]
    #   処理: [token採番とversionを0から始める]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[int, PinToken] = {}
        self._counter = itertools.count(1)
        self._version = 0

    # {
    #   責務: [version: pin集合の変化回数を返す]
    #   処理: [lock内で読む]
    #   引数: [なし]
    #   戻り値: [int: 現在のversion]
    # }
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # {
    #   責務: [pin: digestを参照中として登録し、releaseに使うtokenを返す]
    #   処理: [digest形式を検査して採番・登録する。同じdigestを複数回pinできる]
    #   引数: [sha256: 小文字SHA-256, reason: 診断用の短い理由]
    #   戻り値: [PinToken: 登録したpin]
    #   エラー: [ValueError: digest・理由が契約外]
    # }
    def pin(self, sha256: str, reason: str) -> PinToken:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("reason must be a short non-empty string")
        with self._lock:
            token = PinToken(next(self._counter), sha256, reason)
            self._tokens[token.token_id] = token
            self._version += 1
            return token

    # {
    #   責務: [release: tokenのpinを外す]
    #   処理: [登録済みなら削除してversionを進める。未登録は何もしない]
    #   引数: [token: pinで得たtoken]
    #   戻り値: [bool: 外したらTrue]
    # }
    def release(self, token: PinToken) -> bool:
        with self._lock:
            removed = self._tokens.pop(token.token_id, None) is not None
            if removed:
                self._version += 1
            return removed

    # {
    #   責務: [release_digest: 同じdigestの最も古いpin 1件を外す]
    #   処理: [token_id昇順で最初に一致するpinを削除する]
    #   引数: [sha256: 対象digest]
    #   戻り値: [bool: 外したらTrue]
    # }
    def release_digest(self, sha256: str) -> bool:
        with self._lock:
            for token_id in sorted(self._tokens):
                if self._tokens[token_id].sha256 == sha256:
                    del self._tokens[token_id]
                    self._version += 1
                    return True
            return False

    # {
    #   責務: [digests: 現在pin中のdigest集合を返す]
    #   処理: [lock内で複製する]
    #   引数: [なし]
    #   戻り値: [frozenset[str]: pin中のdigest]
    # }
    def digests(self) -> frozenset[str]:
        with self._lock:
            return frozenset(token.sha256 for token in self._tokens.values())

    # {
    #   責務: [clear: 全pinを破棄する（Session終了後）]
    #   処理: [全tokenを削除する]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def clear(self) -> None:
        with self._lock:
            if self._tokens:
                self._tokens.clear()
                self._version += 1


# {
#   責務: [CompositeReferenceProvider: 複数sourceを合成し、完全性・世代・位置を持つsnapshotを作る]
#   フィールド: [session_id: snapshotに付けるSession ID, _sources: 合成するsource, _clock: ms時計, _pins: 位置表示だけに使うpin集合]
#   補足: [source例外は握りつぶさず呼出側（adapter）へ伝え、adapterがsnapshot_failedとして削除しない。
#     pinの参照はsnapshotでなくadapterがgate内で合成する（generationに含めない）]
# }
class CompositeReferenceProvider:
    # {
    #   責務: [__init__: 合成するsourceと時計を固定する]
    #   処理: [source名の重複を拒否する]
    #   引数: [session_id: Session ID, sources: 参照source, clock: ms時計, pins: 位置表示用のpin集合（省略可）]
    #   戻り値: [なし]
    #   エラー: [ValueError: Session ID空・source名重複]
    # }
    def __init__(
        self,
        session_id: str,
        sources: Sequence[ReferenceSource],
        *,
        clock: Callable[[], int] = current_time_ms,
        pins: PinnedReferenceSource | None = None,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        names = [source.name for source in sources]
        if len(set(names)) != len(names):
            raise ValueError("reference source names must be unique")
        self.session_id = session_id
        self._sources = tuple(sources)
        self._clock = clock
        self._pins = pins

    # {
    #   責務: [note_recorded: recording完了を該当sourceへ伝え、次回の読み直しを保証する]
    #   処理: [targetがexperience:<name>なら同名sourceのmark_changedを呼ぶ。journalは位置で読むため何もしない]
    #   引数: [target: journal または experience:<name>]
    #   戻り値: [なし]
    # }
    def note_recorded(self, target: str) -> None:
        for source in self._sources:
            if source.name == target and isinstance(source, ExperienceReferenceSource):
                source.mark_changed()

    # {
    #   責務: [snapshot: 全sourceを読み、digest昇順・重複なしの参照と世代を持つsnapshotを返す]
    #   処理: [開始時刻を記録し、各sourceのreadを順に呼ぶ。世代は各sourceの変化回数の合計]
    #   引数: [なし]
    #   戻り値: [RetentionReferenceSnapshot: 合成結果。1つでも不完全ならcomplete=False]
    #   エラー: [Exception: source読取の失敗（そのまま伝える）]
    # }
    def snapshot(self) -> RetentionReferenceSnapshot:
        created_at_ms = self._clock()
        readings = [source.read() for source in self._sources]
        merged: dict[str, ArtifactReference] = {}
        positions: dict[str, str] = {}
        reasons: list[str] = []
        generation = 0
        for reading in readings:
            for reference in reading.references:
                merged.setdefault(reference.sha256, reference)
            positions[reading.name] = reading.position
            reasons.extend(reading.reasons)
            generation += reading.changes
        if self._pins is not None:
            positions["pins"] = f"version={self._pins.version}"
        ordered = tuple(merged[digest] for digest in sorted(merged))
        complete = all(reading.complete for reading in readings)
        return RetentionReferenceSnapshot(
            session_id=self.session_id,
            generation=generation,
            created_at_ms=created_at_ms,
            artifact_sha256s=frozenset(merged),
            references=ordered,
            source_positions=positions,
            complete=complete,
            incomplete_reasons=tuple(reasons),
        )


# {
#   責務: [SkippedRetention: GCを実行しなかった（削除0件）理由を表す]
#   フィールド: [trigger: 起動理由, reason: not_due/coalesced/already_running/closed/store_absent/snapshot_*/session_mismatch/
#     generation_mismatch/lock_timeout/index_error/safety_error, detail: 診断用の短い文字列（digestやpathを含めない）]
# }
@dataclass(frozen=True)
class SkippedRetention:
    trigger: RetentionTrigger
    reason: str
    detail: str = ""


# {
#   責務: [RuntimeRetentionReport: 実行したGC 1回の結果と、その時のsnapshot・遅延計測を表す]
#   フィールド: [trigger: 起動理由, report: Collectorの結果, snapshot_generation/source_positions: 使ったsnapshotの世代と位置,
#     deferred_artifacts: 作業上限で今回見送った削除候補数, pressure: GC後の容量圧迫, metrics: ms単位の計測,
#     blocked_refs: 参照中で残した記録数（上限による見送りを除く）]
# }
@dataclass(frozen=True)
class RuntimeRetentionReport:
    trigger: RetentionTrigger
    report: RetentionReport
    snapshot_generation: int
    source_positions: Mapping[str, str]
    deferred_artifacts: int
    pressure: PressureLevel
    metrics: Mapping[str, float]
    blocked_refs: int = 0


# {
#   責務: [RetentionTraceEvent: Journalへ記録するretention.gc/v1の内容（digestとpathを含めない）]
#   フィールド: [trigger: 起動理由, outcome: collected/pressure/skipped, skip_reason: skip時の理由, gc_count: 累積GC回数,
#     counts: 削除・残した件数とbyte, metrics: 各latency, snapshot_generation/source_positions: snapshot情報]
# }
@dataclass(frozen=True)
class RetentionTraceEvent:
    trigger: RetentionTrigger
    outcome: str
    skip_reason: str | None
    gc_count: int
    counts: Mapping[str, Any]
    metrics: Mapping[str, float]
    snapshot_generation: int | None
    source_positions: Mapping[str, str]

    # {
    #   責務: [to_payload: Journal eventのpayloadへ入れるJSON互換dictを作る]
    #   処理: [固定の項目名へ展開する]
    #   引数: [なし]
    #   戻り値: [dict[str, Any]: digest・pathを含まないpayload]
    # }
    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "trigger": self.trigger.value,
            "outcome": self.outcome,
            "skip_reason": self.skip_reason,
            "gc_count": self.gc_count,
            "snapshot_generation": self.snapshot_generation,
            "source_positions": dict(self.source_positions),
        }
        payload.update(self.counts)
        payload.update(self.metrics)
        return payload


# {
#   責務: [PersistedArtifact: persistが返す、保存・登録済みArtifactとpin token]
#   フィールド: [reference: 保存したArtifactの参照, entry_id: 索引のentry_id, token: 記録するまでdigestを守るpin]
# }
@dataclass(frozen=True)
class PersistedArtifact:
    reference: ArtifactReference
    entry_id: str
    token: PinToken


# {
#   責務: [RetentionStats: adapterの累積観測値の読取専用snapshot]
#   フィールド: [gc_count: 実行したGC数, skipped: 理由ごとのskip数, requests: request()の件数, persisted: 保存件数,
#     trace_errors: Trace記録の失敗数, worker_join_timeout: close時にworkerのjoinが期限切れになった数]
# }
@dataclass(frozen=True)
class RetentionStats:
    gc_count: int
    skipped: Mapping[str, int]
    requests: int
    persisted: int
    trace_errors: int
    worker_join_timeout: int


_STATE_OPEN = "open"
_STATE_CLOSING = "closing"
_STATE_CLOSED = "closed"


# {
#   責務: [RetentionWorker: request()とperiodic周期でGCを1本のthreadから起動し、hot pathを待たせない]
#   フィールド: [_adapter: 呼出先, _periodic_s: 周期秒, _cond: 要求flagとstopを守るCondition, _pending: 畳んだ要求,
#     _stop: 停止要求, _thread: 非daemonのworker thread]
# }
class RetentionWorker:
    # {
    #   責務: [__init__: 呼出先adapterと周期を固定する（threadは開始しない）]
    #   処理: [Conditionと状態を初期化する]
    #   引数: [adapter: maybe_collectを持つadapter, periodic_interval_ms: 周期ms]
    #   戻り値: [なし]
    # }
    def __init__(
        self, adapter: RuntimeRetentionAdapter, *, periodic_interval_ms: int
    ) -> None:
        self._adapter = adapter
        self._periodic_s = periodic_interval_ms / _MILLISECONDS_PER_SECOND
        self._cond = threading.Condition()
        self._pending: RetentionTrigger | None = None
        self._stop = False
        self._thread: threading.Thread | None = None

    # {
    #   責務: [start: workerを一度だけ開始する]
    #   処理: [未開始かつ未停止ならnon-daemon threadを作って開始する]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def start(self) -> None:
        with self._cond:
            if self._thread is not None or self._stop:
                return
            self._thread = threading.Thread(
                target=self._loop, name="retention-worker", daemon=False
            )
            self._thread.start()

    # {
    #   責務: [signal: 要求を立ててworkerを起こす（O(1)・非blocking）]
    #   処理: [複数の要求は最後の1件へ畳む]
    #   引数: [trigger: 起動理由]
    #   戻り値: [なし]
    # }
    def signal(self, trigger: RetentionTrigger) -> None:
        with self._cond:
            self._pending = trigger
            self._cond.notify()

    # {
    #   責務: [stop: workerを停止してjoinする]
    #   処理: [stopを立てて起こし、期限までjoinする]
    #   引数: [timeout_seconds: join上限秒]
    #   戻り値: [bool: threadが終了した（または未開始）ならTrue]
    # }
    def stop(self, timeout_seconds: float) -> bool:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    # {
    #   責務: [_loop: 要求または周期で起き、maybe_collectを1回呼ぶ]
    #   処理: [停止まで繰り返す。見送り中は最短間隔の残りで起き直す。adapterの例外は握りつぶして継続する]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def _loop(self) -> None:
        while True:
            # adapterの状態は条件変数のlockの外で読む（signalを待たせない）
            delay = self._adapter.deferred_delay_seconds()
            with self._cond:
                if not self._stop and self._pending is None:
                    timeout = self._periodic_s if delay is None else delay
                    self._cond.wait(timeout)
                    if self._pending is None and not self._stop:
                        self._pending = (
                            RetentionTrigger.PERSIST_COUNT
                            if delay is not None
                            else RetentionTrigger.PERIODIC
                        )
                if self._stop:
                    return
                trigger, self._pending = self._pending, None
            if trigger is None:
                continue
            try:
                self._adapter.maybe_collect(trigger)
            except Exception:
                # worker自体は止めない（結果はadapterのstatsとTraceに残る）
                continue


# {
#   責務: [RuntimeRetentionAdapter: 検証済みsnapshotからだけRetentionCollectorのGCを、bounded triggerと作業上限付きで呼ぶ]
#   フィールド: [_collector/_admission: #164のCollectorと#789のcoordinator, _provider: 参照snapshot, _pins: 保存途中の参照,
#     _trigger_policy: 起動条件, _gate: 参照追加とGCを直列化するRLock, _run_lock: GCを同時に1本にするlock,
#     _state_lock: 計数と状態を守る葉のlock, _state: open/closing/closed]
#   処理: [persist(reserve→admit→on_artifact_persisted)・recording(gate)・maybe_collect(snapshot検証→作業上限→collect)・close_session]
#   補足: [collectを呼ぶのはこのclassだけ。lock順序は gate → root lock → journal lock]
# }
class RuntimeRetentionAdapter:
    # {
    #   責務: [__init__: 依存と起動policyを固定する（ディスクには書かない）]
    #   処理: [pin集合・gate・計数を初期化し、背景workerを（開始せずに）用意する]
    #   引数: [collector: 対象Collector, admission: 同じCollectorのcoordinator, provider: 参照snapshot, policy: 起動policy,
    #     session_id: このSessionのID, clock: UNIXエポックms時計, trace: Trace受信関数, pins: 共有するpin集合,
    #     background: 背景workerを使うか]
    #   戻り値: [なし]
    #   エラー: [ValueError: session_id空]
    # }
    def __init__(
        self,
        collector: RetentionCollector,
        admission: ArtifactAdmissionCoordinator,
        provider: LiveReferenceProvider,
        policy: RetentionTriggerPolicy,
        *,
        session_id: str,
        clock: Callable[[], int] = current_time_ms,
        trace: Callable[[RetentionTraceEvent], None] | None = None,
        pins: PinnedReferenceSource | None = None,
        background: bool = True,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self._collector = collector
        self._admission = admission
        self._provider = provider
        self._trigger_policy = policy
        self.session_id = session_id
        self._clock = clock
        self._trace = trace
        self._pins = pins if pins is not None else PinnedReferenceSource()
        self._gate = threading.RLock()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._local = threading.local()
        self._state = _STATE_OPEN
        self._last_generation = 0
        self._last_gc_started_ms: int | None = None
        self._persisted_since = 0
        self._bytes_since = 0
        self._base_bytes = 0
        self._deferred_pending = False
        self._pressure: PressureLevel = "none"
        self._gc_count = 0
        self._requests = 0
        self._persisted_total = 0
        self._trace_errors = 0
        self._worker_join_timeout = 0
        self._skipped: dict[str, int] = {}
        self.last_close_result: RuntimeRetentionReport | SkippedRetention | None = None
        self._worker = (
            RetentionWorker(self, periodic_interval_ms=policy.periodic_interval_ms)
            if background
            else None
        )

    # {
    #   責務: [pressure: 直近のGC後に容量圧迫が残っているかを返す]
    #   処理: [state lock内で読む]
    #   引数: [なし]
    #   戻り値: [Literal: none/over_budget/over_hard_limit]
    # }
    @property
    def pressure(self) -> PressureLevel:
        with self._state_lock:
            return self._pressure

    # {
    #   責務: [stats: 累積観測値を読取専用snapshotで返す]
    #   処理: [state lock内で複製する]
    #   引数: [なし]
    #   戻り値: [RetentionStats: GC数・skip数・保存件数など]
    # }
    @property
    def stats(self) -> RetentionStats:
        with self._state_lock:
            return RetentionStats(
                gc_count=self._gc_count,
                skipped=dict(self._skipped),
                requests=self._requests,
                persisted=self._persisted_total,
                trace_errors=self._trace_errors,
                worker_join_timeout=self._worker_join_timeout,
            )

    # {
    #   責務: [reserve: digestをadmit前にpinし、保存途中の新規entryがGCで退避される窓を無くす]
    #   処理: [open中だけpinを登録してtokenを返す]
    #   引数: [sha256: 保存予定のdigest, reason: 診断用の短い理由]
    #   戻り値: [PinToken: release/recordingで外すtoken]
    #   エラー: [RuntimeError: Session終了済み, ValueError: digest・理由が契約外]
    # }
    def reserve(self, sha256: str, *, reason: str) -> PinToken:
        # 終了判定とpin登録を同じ区間で行い、終了後に新しいpinが残る窓を作らない
        # （PinnedReferenceSourceのlockは葉なので、state lock → pins lockの順で固定）
        with self._state_lock:
            if self._state == _STATE_CLOSED:
                raise RuntimeError("retention adapter is closed")
            return self._pins.pin(sha256, reason)

    # {
    #   責務: [release: producerが記録を諦めたpinを外す]
    #   処理: [closed後は無視し、それ以外はpinを外す]
    #   引数: [token: reserveで得たtoken]
    #   戻り値: [なし]
    # }
    def release(self, token: PinToken) -> None:
        with self._state_lock:
            if self._state == _STATE_CLOSED:
                return
        self._pins.release(token)

    # {
    #   責務: [persist: reserve→admit→on_artifact_persistedの順を内部で守る本番の保存口]
    #   処理: [expected_sha256を必須にしてpinし、#789のcoordinatorでadmitし、成功後に保存を数える。失敗時はpinを外す]
    #   引数: [request: 保存要求（expected_sha256必須）, writer: AdmissionPutPortを使うwriter]
    #   戻り値: [PersistedArtifact: 参照・entry_id・pin token]
    #   エラー: [ValueError: expected_sha256なし, RuntimeError: 終了済み,
    #     RetentionLockTimeoutError/ArtifactAdmissionError等: #789のadmitの例外をそのまま返す]
    # }
    def persist(
        self, request: ArtifactAdmissionRequest, writer: AdmissionWriter
    ) -> PersistedArtifact:
        if request.expected_sha256 is None:
            raise ValueError("persist requires request.expected_sha256")
        token = self.reserve(request.expected_sha256, reason=request.source_ref)
        try:
            reference = self._admission.admit(request, writer)
        except BaseException:
            self._pins.release(token)
            raise
        # 保存済みになった後の失敗ではpinを外さない（記録前のArtifactをGCさせない。closeまで残る）
        self.on_artifact_persisted(token, reference, request.entry_id)
        return PersistedArtifact(reference, request.entry_id, token)

    # {
    #   責務: [on_artifact_persisted: admit完了後の保存を数え、件数・推定使用量のtriggerを判定する]
    #   処理: [tokenとreferenceのdigest一致を確認し、protectedならProtectedへ切り替え、件数と推定byteを加えてrequestする]
    #   引数: [token: admit前のpin, reference: admitが返した参照, entry_id: 索引のentry_id, protected: 追加でProtected化するか]
    #   戻り値: [なし]
    #   エラー: [ValueError: tokenとreferenceのdigestが違う, RetentionIndexError: Protected化に失敗]
    # }
    def on_artifact_persisted(
        self,
        token: PinToken,
        reference: ArtifactReference,
        entry_id: str,
        protected: bool = False,
    ) -> None:
        if token.sha256 != reference.sha256:
            raise ValueError("token digest does not match the persisted reference")
        if protected:
            self._collector.set_protected(entry_id, True)
        try:
            size = self._collector.store.read_metadata(reference.sha256).size_bytes
        except Exception:
            # 大きさは推定使用量の更新にだけ使う。取れなければ次のGCの実測に任せる
            size = 0
        policy = self._collector.policy
        with self._state_lock:
            self._persisted_since += 1
            self._persisted_total += 1
            self._bytes_since += size
            if self._persisted_since >= self._trigger_policy.persist_count_threshold:
                trigger: RetentionTrigger | None = RetentionTrigger.PERSIST_COUNT
            elif self._base_bytes + self._bytes_since > policy.soft_budget_bytes:
                trigger = RetentionTrigger.SOFT_BUDGET_CROSSING
            else:
                trigger = None
        if trigger is not None:
            self.request(trigger)

    # {
    #   責務: [assert_recording: 参照付きJournal appendが、その参照を持つrecording()の中から呼ばれているか確認する]
    #   処理: [呼出threadが入っているrecordingのdigestの和集合に、全参照のdigestが含まれることを確認する]
    #   引数: [references: appendしようとしている参照]
    #   戻り値: [なし]
    #   エラー: [RetentionReferenceError: gate外またはrecordingに含まれない参照]
    #   補足: [Journalのlock内で呼ばれるため、他のlockを取らない]
    # }
    def assert_recording(self, references: Iterable[ArtifactReference]) -> None:
        allowed: set[str] = set()
        for digests in getattr(self._local, "stack", ()):
            allowed |= digests
        if any(
            not isinstance(reference, ArtifactReference)
            or reference.sha256 not in allowed
            for reference in references
        ):
            raise RetentionReferenceError(
                "artifact references must be appended inside recording()"
            )

    # {
    #   責務: [recording: 参照をJournal/Experienceへ書く間、GCとの競合を防ぐgateを期限付きで持つ]
    #   処理: [gateを取り、各digestのobject存在を確認し、本体の間だけguardを許す。例外なく抜けた時だけ該当pinを外し、
    #     Experienceならsourceの版を進める。例外で抜けた時はpinを残す]
    #   引数: [references: これから書く参照, target: journal または experience:<name>, timeout_seconds: gate待ち上限秒]
    #   戻り値: [ContextManager: with本体でappendを行う]
    #   エラー: [RetentionLockTimeoutError: gate待ち超過（何も書かれない）, RetentionReferenceError: objectが無い・不正なtarget,
    #     RuntimeError: 終了済み]
    # }
    @contextmanager
    def recording(
        self,
        references: Iterable[ArtifactReference],
        *,
        target: str = "journal",
        timeout_seconds: float = 1.0,
    ) -> Iterator[None]:
        refs = tuple(references)
        if any(not isinstance(item, ArtifactReference) for item in refs):
            raise TypeError("references must be ArtifactReference values")
        if target != "journal" and not (
            isinstance(target, str)
            and target.startswith("experience:")
            and _EXPERIENCE_NAME.fullmatch(target[len("experience:") :]) is not None
        ):
            raise RetentionReferenceError(
                "recording target must be journal or experience:<name>"
            )
        _require_seconds(timeout_seconds, "timeout_seconds")
        with self._state_lock:
            if self._state == _STATE_CLOSED:
                raise RuntimeError("retention adapter is closed")
        if not self._gate.acquire(timeout=timeout_seconds):
            raise RetentionLockTimeoutError("retention gate timed out")
        try:
            store = self._collector.store
            for reference in refs:
                if not Path(store.object_path(reference.sha256)).is_dir():
                    raise RetentionReferenceError(
                        "referenced artifact object does not exist"
                    )
            stack = self._local.__dict__.setdefault("stack", [])
            stack.append(frozenset(item.sha256 for item in refs))
            try:
                yield
            finally:
                stack.pop()
            # ここへ届くのはappendがcommit済みの時だけ（例外ならpinを残す）
            for reference in refs:
                self._pins.release_digest(reference.sha256)
            note = getattr(self._provider, "note_recorded", None)
            if callable(note):
                note(target)
        finally:
            self._gate.release()

    # {
    #   責務: [request: GCを非同期に要求する（hot path用・O(1)・非blocking）]
    #   処理: [open中だけ要求を数え、背景workerを（初回に）開始して起こす]
    #   引数: [trigger: 起動理由]
    #   戻り値: [なし]
    # }
    def request(self, trigger: RetentionTrigger) -> None:
        with self._state_lock:
            if self._state != _STATE_OPEN:
                return
            self._requests += 1
        worker = self._worker
        if worker is not None:
            worker.start()
            worker.signal(trigger)

    # {
    #   責務: [deferred_delay_seconds: 作業上限で見送った分があるとき、次に実行できるまでの秒を返す]
    #   処理: [見送りが無ければNone。あれば最短間隔の残りを返す]
    #   引数: [なし]
    #   戻り値: [float | None: 待ち秒。見送り無しはNone]
    # }
    def deferred_delay_seconds(self) -> float | None:
        with self._state_lock:
            if not self._deferred_pending:
                return None
            last = self._last_gc_started_ms
        if last is None:
            return 0.0
        remaining = self._trigger_policy.min_interval_ms - (self._clock() - last)
        return max(0.0, remaining / _MILLISECONDS_PER_SECOND) + 0.001

    # {
    #   責務: [maybe_collect: 条件を満たす時だけGCを1回実行する]
    #   処理: [closedなら何もせず、実行中なら畳み、取得できたら条件判定→GCを行う]
    #   引数: [trigger: 起動理由]
    #   戻り値: [RuntimeRetentionReport | SkippedRetention: 実行結果または見送り理由（削除0）]
    # }
    def maybe_collect(
        self, trigger: RetentionTrigger
    ) -> RuntimeRetentionReport | SkippedRetention:
        with self._state_lock:
            if self._state != _STATE_OPEN:
                return self._count_skip(SkippedRetention(trigger, "closed"))
        if not self._run_lock.acquire(blocking=False):
            return self._count_skip(SkippedRetention(trigger, "already_running"))
        try:
            return self._run(trigger)
        finally:
            self._run_lock.release()

    # {
    #   責務: [close_session: workerを止め、最短間隔と条件を迂回して最終GCを1回行い、pinを破棄する]
    #   処理: [openでなければ前回結果を返す。workerを停止・join（期限切れは記録して続行）し、run lockを期限付きで取って
    #     session_close triggerで実行する。結果はlast_close_resultへ保持する]
    #   引数: [なし]
    #   戻り値: [RuntimeRetentionReport | SkippedRetention: 最終GCの結果（失敗でも例外にしない）]
    # }
    def close_session(self) -> RuntimeRetentionReport | SkippedRetention:
        trigger = RetentionTrigger.SESSION_CLOSE
        with self._state_lock:
            if self._state != _STATE_OPEN:
                return self.last_close_result or SkippedRetention(trigger, "closed")
            self._state = _STATE_CLOSING
        policy = self._trigger_policy
        if self._worker is not None and not self._worker.stop(
            policy.close_lock_timeout_seconds + 1
        ):
            with self._state_lock:
                self._worker_join_timeout += 1
        result: RuntimeRetentionReport | SkippedRetention
        if self._run_lock.acquire(timeout=policy.close_lock_timeout_seconds):
            try:
                result = self._run(trigger)
            except Exception as error:
                result = self._count_skip(
                    SkippedRetention(trigger, "safety_error", type(error).__name__)
                )
            finally:
                self._run_lock.release()
        else:
            result = self._count_skip(SkippedRetention(trigger, "already_running"))
        self._pins.clear()
        with self._state_lock:
            self._state = _STATE_CLOSED
            self.last_close_result = result
        return result

    # {
    #   責務: [_count_skip: skipをstatsへ数えて返す]
    #   処理: [理由ごとの件数を加算する]
    #   引数: [skipped: 見送り結果]
    #   戻り値: [SkippedRetention: 同じ値]
    # }
    def _count_skip(self, skipped: SkippedRetention) -> SkippedRetention:
        with self._state_lock:
            self._skipped[skipped.reason] = self._skipped.get(skipped.reason, 0) + 1
        return skipped

    # {
    #   責務: [_is_due: forced以外のtriggerが条件を満たすか判定する]
    #   処理: [見送り中なら常に真。件数・推定使用量・周期をtriggerごとに見る]
    #   引数: [trigger: 起動理由, now: 現在ms]
    #   戻り値: [bool: 実行してよいか]
    # }
    def _is_due(self, trigger: RetentionTrigger, now: int) -> bool:
        if self._deferred_pending:
            return True
        if trigger is RetentionTrigger.PERSIST_COUNT:
            return self._persisted_since >= self._trigger_policy.persist_count_threshold
        if trigger is RetentionTrigger.SOFT_BUDGET_CROSSING:
            estimated = self._base_bytes + self._bytes_since
            return estimated > self._collector.policy.soft_budget_bytes
        last = self._last_gc_started_ms
        return last is None or (now - last >= self._trigger_policy.periodic_interval_ms)

    # {
    #   責務: [_run: 条件判定→GC→Trace記録を行う（run lockを持った状態で呼ぶ）]
    #   処理: [forced以外は最短間隔と条件で畳む。実行すると開始時刻を記録し、lockとgateを解放した後でTraceを記録する]
    #   引数: [trigger: 起動理由]
    #   戻り値: [RuntimeRetentionReport | SkippedRetention: 結果]
    # }
    def _run(
        self, trigger: RetentionTrigger
    ) -> RuntimeRetentionReport | SkippedRetention:
        started = time.perf_counter()
        policy = self._trigger_policy
        now = self._clock()
        with self._state_lock:
            if trigger not in _FORCED_TRIGGERS:
                last = self._last_gc_started_ms
                if last is not None and 0 <= now - last < policy.min_interval_ms:
                    return self._count_skip_locked(
                        SkippedRetention(trigger, "coalesced")
                    )
                if not self._is_due(trigger, now):
                    return self._count_skip_locked(SkippedRetention(trigger, "not_due"))
            self._last_gc_started_ms = now
        timeout = (
            policy.close_lock_timeout_seconds
            if trigger is RetentionTrigger.SESSION_CLOSE
            else policy.trigger_lock_timeout_seconds
        )
        metrics: dict[str, float] = {}
        try:
            result = self._collect_once(trigger, timeout, metrics)
        except Exception as error:
            result = SkippedRetention(trigger, "safety_error", type(error).__name__)
        metrics["total_ms"] = (time.perf_counter() - started) * _MILLISECONDS_PER_SECOND
        if isinstance(result, SkippedRetention):
            self._count_skip(result)
        else:
            with self._state_lock:
                self._gc_count += 1
        self._emit_trace(result, metrics)
        if isinstance(result, RuntimeRetentionReport) and result.deferred_artifacts:
            self.request(RetentionTrigger.PERSIST_COUNT)
        return result

    # {
    #   責務: [_count_skip_locked: state lockを持った状態でskipを数えて返す]
    #   処理: [理由ごとの件数を加算する]
    #   引数: [skipped: 見送り結果]
    #   戻り値: [SkippedRetention: 同じ値]
    # }
    def _count_skip_locked(self, skipped: SkippedRetention) -> SkippedRetention:
        self._skipped[skipped.reason] = self._skipped.get(skipped.reason, 0) + 1
        return skipped

    # {
    #   責務: [_collect_once: gateとroot lockを取り、検証済みsnapshotでGCを1回適用する]
    #   処理: [rootが無ければ見送り、gate→root lockの順に期限付きで取り、_collect_lockedを実行して必ず解放する]
    #   引数: [trigger: 起動理由, timeout_seconds: lock待ち上限, metrics: 計測の出力先]
    #   戻り値: [RuntimeRetentionReport | SkippedRetention: 結果]
    # }
    def _collect_once(
        self,
        trigger: RetentionTrigger,
        timeout_seconds: float,
        metrics: dict[str, float],
    ) -> RuntimeRetentionReport | SkippedRetention:
        root = Path(self._collector.store.root)
        # root lockはrootを作るため、無い間は取らずに見送る
        if not os.path.lexists(root):
            return SkippedRetention(trigger, "store_absent")
        gate_started = time.perf_counter()
        if not self._gate.acquire(timeout=timeout_seconds):
            return SkippedRetention(trigger, "lock_timeout", "gate")
        metrics["gate_wait_ms"] = (
            time.perf_counter() - gate_started
        ) * _MILLISECONDS_PER_SECOND
        try:
            try:
                with retention_root_lock(
                    root, timeout_seconds=timeout_seconds
                ) as waited_ms:
                    metrics["lock_wait_ms"] = waited_ms
                    return self._collect_locked(trigger, metrics)
            except RetentionLockTimeoutError:
                return SkippedRetention(trigger, "lock_timeout", "root")
            except RetentionRootUnsafeError:
                return SkippedRetention(trigger, "safety_error", "unsafe_root")
        finally:
            self._gate.release()

    # {
    #   責務: [_collect_locked: gateとroot lockを持った状態で、snapshot検証・作業上限・collectを行う]
    #   処理: [1: snapshotを取り失敗・不完全・別Session・古い・世代逆行を見送る
    #     2: 増分を再読取して世代が変わっていれば見送る  3: snapshotの参照にpin中のdigestを加える
    #     4: 削除候補が上限を超えるなら今回分以外を全てliveに加えて見送り件数を数える  5: collectを呼ぶ]
    #   引数: [trigger: 起動理由, metrics: 計測の出力先]
    #   戻り値: [RuntimeRetentionReport | SkippedRetention: 結果（見送りは削除0）]
    # }
    def _collect_locked(
        self, trigger: RetentionTrigger, metrics: dict[str, float]
    ) -> RuntimeRetentionReport | SkippedRetention:
        policy = self._trigger_policy
        snapshot_started = time.perf_counter()
        try:
            snapshot = self._provider.snapshot()
        except Exception as error:
            return SkippedRetention(trigger, "snapshot_failed", type(error).__name__)
        if not isinstance(snapshot, RetentionReferenceSnapshot):
            return SkippedRetention(trigger, "snapshot_failed", "invalid_snapshot")
        if not snapshot.complete:
            return SkippedRetention(
                trigger, "snapshot_incomplete", ",".join(snapshot.incomplete_reasons)
            )
        if snapshot.session_id != self.session_id:
            return SkippedRetention(trigger, "session_mismatch")
        with self._state_lock:
            last_generation = self._last_generation
        if (
            self._clock() - snapshot.created_at_ms > policy.snapshot_max_age_ms
            or snapshot.generation < last_generation
        ):
            return SkippedRetention(trigger, "snapshot_stale")
        # collect直前の増分再読取。gate内なので通常は0件で、世代が動いていれば見送る
        try:
            confirmation = self._provider.snapshot()
        except Exception as error:
            return SkippedRetention(trigger, "snapshot_failed", type(error).__name__)
        if not isinstance(confirmation, RetentionReferenceSnapshot):
            return SkippedRetention(trigger, "snapshot_failed", "invalid_snapshot")
        if not confirmation.complete:
            return SkippedRetention(
                trigger,
                "snapshot_incomplete",
                ",".join(confirmation.incomplete_reasons),
            )
        if confirmation.generation != snapshot.generation:
            return SkippedRetention(trigger, "generation_mismatch")
        with self._state_lock:
            self._last_generation = max(self._last_generation, snapshot.generation)
        metrics["snapshot_ms"] = (
            time.perf_counter() - snapshot_started
        ) * _MILLISECONDS_PER_SECOND
        live = self._with_pinned(snapshot.references)
        collect_started = time.perf_counter()
        try:
            plan = self._collector.plan(live)
            deferred: frozenset[str] = frozenset()
            collect_live = live
            if len(plan.deleted_artifacts) > policy.max_deleted_artifacts_per_run:
                keep = policy.max_deleted_artifacts_per_run
                this_run = frozenset(plan.deleted_artifacts[:keep])
                deferred = frozenset(plan.deleted_artifacts[keep:])
                # 今回分以外の索引上の全digestをliveにし、削除対象を今回分へ限る
                indexed = {entry.artifact_sha256 for entry in plan.kept_entries}
                indexed |= {entry.artifact_sha256 for entry in plan.removed_entries}
                collect_live = (
                    *live,
                    *(_placeholder_reference(d) for d in sorted(indexed - this_run)),
                )
            report = self._collector.collect(collect_live)
        except RetentionIndexError as error:
            return SkippedRetention(trigger, "index_error", type(error).__name__)
        except RetentionSafetyError as error:
            return SkippedRetention(trigger, "safety_error", type(error).__name__)
        except (OSError, ValueError, TypeError) as error:
            return SkippedRetention(trigger, "index_error", type(error).__name__)
        metrics["collect_ms"] = (
            time.perf_counter() - collect_started
        ) * _MILLISECONDS_PER_SECOND
        # 見送りがある時は、全件適用後の状態（見送り前の計画）で圧迫を判定する
        judged = plan if deferred else report.plan
        pressure: PressureLevel = (
            "over_hard_limit"
            if judged.over_hard_limit
            else "over_budget"
            if judged.over_budget
            else "none"
        )
        digest_by_entry = {
            entry.entry_id: entry.artifact_sha256 for entry in report.plan.kept_entries
        }
        blocked_refs = sum(
            1
            for blocked in report.plan.blocked_entries
            if digest_by_entry.get(blocked.entry_id) not in deferred
        )
        with self._state_lock:
            self._persisted_since = 0
            self._bytes_since = 0
            self._base_bytes = report.plan.bytes_after
            self._pressure = pressure
            self._deferred_pending = bool(deferred)
        return RuntimeRetentionReport(
            trigger=trigger,
            report=report,
            snapshot_generation=snapshot.generation,
            source_positions=dict(snapshot.source_positions),
            deferred_artifacts=len(deferred),
            pressure=pressure,
            metrics=dict(metrics),
            blocked_refs=blocked_refs,
        )

    # {
    #   責務: [_with_pinned: snapshotの参照に、その時点でpin中のdigestの参照を加える]
    #   処理: [pin中でsnapshotに無いdigestだけStoreのmetadataから参照を作る。objectが無いdigestは削除対象になり得ないため飛ばす]
    #   引数: [references: snapshotの参照]
    #   戻り値: [tuple: collectへ渡す参照]
    # }
    def _with_pinned(
        self, references: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        known = {reference.sha256 for reference in references}
        extra: list[ArtifactReference] = []
        for digest in sorted(self._pins.digests() - known):
            try:
                extra.append(self._collector.store.read_metadata(digest).to_reference())
            except (ArtifactIntegrityError, OSError, ValueError):
                continue
        return (*references, *extra)

    # {
    #   責務: [_emit_trace: GC結果をTraceへ記録する（lockを解放した後に呼ぶ）]
    #   処理: [判定に使わなかったskipは記録せず、記録失敗はstatsに数えるだけで結果を変えない]
    #   引数: [result: 結果, metrics: 計測]
    #   戻り値: [なし]
    # }
    def _emit_trace(
        self,
        result: RuntimeRetentionReport | SkippedRetention,
        metrics: Mapping[str, float],
    ) -> None:
        if self._trace is None:
            return
        if isinstance(result, SkippedRetention):
            if result.reason in _UNTRACED_SKIPS:
                return
            event = RetentionTraceEvent(
                trigger=result.trigger,
                outcome="skipped",
                skip_reason=result.reason,
                gc_count=self.stats.gc_count,
                counts={},
                metrics=_round_metrics(metrics),
                snapshot_generation=None,
                source_positions={},
            )
        else:
            plan = result.report.plan
            event = RetentionTraceEvent(
                trigger=result.trigger,
                outcome="pressure" if result.pressure != "none" else "collected",
                skip_reason=None,
                gc_count=self.stats.gc_count,
                counts={
                    "removed_entries": len(plan.removed_entries),
                    "deleted_artifacts": len(result.report.deleted_artifacts),
                    "removed_bytes": plan.bytes_before - plan.bytes_after,
                    "bytes_after": plan.bytes_after,
                    "blocked_refs": result.blocked_refs,
                    "deferred_artifacts": result.deferred_artifacts,
                    "over_budget": result.pressure != "none",
                    "over_hard_limit": result.pressure == "over_hard_limit",
                },
                metrics=_round_metrics(metrics),
                snapshot_generation=result.snapshot_generation,
                source_positions=result.source_positions,
            )
        try:
            self._trace(event)
        except Exception:
            with self._state_lock:
                self._trace_errors += 1


# {
#   責務: [_placeholder_reference: collectの参照型検査を通すための、digestだけが意味を持つ参照を作る]
#   処理: [collectはsha256だけを使うため、型・MIMEは固定値にする]
#   引数: [digest: 見送りで削除させないdigest]
#   戻り値: [ArtifactReference: 削除抑止用の参照]
# }
def _placeholder_reference(digest: str) -> ArtifactReference:
    return ArtifactReference(
        "retention.deferred", digest, "application/octet-stream", False
    )


# {
#   責務: [_round_metrics: 計測値をTraceで読みやすい小数3桁へ丸め、必要な項目を0で補う]
#   処理: [gate_wait_ms等の欠けた項目を0.0にする]
#   引数: [metrics: 計測値]
#   戻り値: [dict[str, float]: 丸めた計測値]
# }
def _round_metrics(metrics: Mapping[str, float]) -> dict[str, float]:
    keys = ("lock_wait_ms", "snapshot_ms", "collect_ms", "total_ms", "gate_wait_ms")
    return {key: round(float(metrics.get(key, 0.0)), 3) for key in keys}
