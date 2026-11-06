"""Session resource lease registry and bounded cleanup coordinator (Issue #786).

設計: doc/design/issue-786-session-resource-registry.md
契約: doc/architecture/runtime_resource_lifecycle.md
"""

from __future__ import annotations

import heapq
import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_PRESSURE_LEVELS = ("none", "elevated", "critical")
# levelごとの (最大件数, 最大ミリ秒)。step周期(既定1000ms)を圧迫しない値
PRESSURE_LIMITS: Mapping[str, tuple[int, float]] = {
    "none": (0, 0.0),
    "elevated": (8, 10.0),
    "critical": (32, 50.0),
}
_ERROR_CODES = frozenset(
    {
        "registry_closed",
        "generation_closed",
        "stale_generation",
        "duplicate_resource_id",
        "not_active",
        "invalid_lease",
        "no_active_task",
        "scope_not_supported",
        "capacity_exceeded",
    }
)
# registryごとの現在task id(contextごと)。(registry id, task id)の不変tupleで保持する
_CURRENT_TASKS: ContextVar[tuple[tuple[int, str], ...]] = ContextVar(
    "ai_game_player_resource_tasks", default=()
)


# {
#   責務: [ResourceScope: resourceの所有範囲を表す固定語彙]
#   フィールド: [SESSION/GENERATION/TASK: Registryが扱う範囲, PROCESS: 拒否される予約語彙]
# }
class ResourceScope(Enum):
    SESSION = "session"
    GENERATION = "generation"
    TASK = "task"
    PROCESS = "process"


# {
#   責務: [ResourceState: leaseの公開状態]
#   フィールド: [ACTIVE/RELEASING/RELEASED/FAILED: 状態遷移の各段階]
# }
class ResourceState(Enum):
    ACTIVE = "active"
    RELEASING = "releasing"
    RELEASED = "released"
    FAILED = "failed"


# {
#   責務: [ResourceLifecycleError: 契約違反を固定語彙codeで拒否する]
#   フィールド: [code: 固定語彙の拒否理由]
# }
class ResourceLifecycleError(RuntimeError):
    # {
    #   責務: [__init__: codeを検証して例外を作る]
    #   引数: [code: 固定語彙, detail: 利用者データを含まない補足]
    #   戻り値: []
    # }
    def __init__(self, code: str, detail: str = "") -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown resource lifecycle error code: {code}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# {
#   責務: [ResourceReleaser: owner側の解放APIを1行で呼ぶAdapterの契約]
#   処理: [releaseは有限時間で戻り、失敗は例外で通知する]
# }
@runtime_checkable
class ResourceReleaser(Protocol):
    def release(self, resource_id: str) -> None: ...


# {
#   責務: [ResourceTraceSink: Traceの受け口]
# }
class ResourceTraceSink(Protocol):
    def record(self, event: str, payload: Mapping[str, Any]) -> None: ...


# {
#   責務: [ClosableOwnerAdapter: close()またはshutdown()を持つownerをReleaserへ変換する]
#   フィールド: [_close: ownerのclose/shutdown]
# }
class ClosableOwnerAdapter:
    # {
    #   責務: [__init__: close優先、無ければshutdownを選ぶ]
    #   引数: [owner: closeまたはshutdownを持つ資源]
    #   戻り値: []
    #   エラー: [TypeError: どちらも無い場合]
    # }
    def __init__(self, owner: Any) -> None:
        close = getattr(owner, "close", None)
        if not callable(close):
            close = getattr(owner, "shutdown", None)
        if not callable(close):
            raise TypeError("owner needs close() or shutdown()")
        self._close = close

    # {
    #   責務: [release: ownerを閉じる]
    #   引数: [resource_id: 解放対象ID(未使用)]
    #   戻り値: []
    #   エラー: [owner例外: そのまま伝播]
    # }
    def release(self, resource_id: str) -> None:
        self._close()


# {
#   責務: [ResourceLease: 登録されたresourceの不変metadata(releaser参照を含まない)]
#   フィールド: [resource_id/kind: 識別, owner_scope/owner_id/generation: 所有者, estimated_bytes: 推定量(Noneは不明),
#     created/expires_monotonic_ns: 時刻, pinned/evictable/priority: eviction方針, provenance: 取得箇所の固定ラベル]
# }
@dataclass(frozen=True)
class ResourceLease:
    resource_id: str
    kind: str
    owner_scope: ResourceScope
    owner_id: str
    generation: int
    estimated_bytes: int | None
    created_monotonic_ns: int
    expires_monotonic_ns: int | None
    pinned: bool
    evictable: bool
    priority: int
    provenance: str


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# {
#   責務: [ResourcePressureSnapshot: 資源圧を消費側の最小契約v1で表す]
#   フィールド: [schema_version: 1固定, level: none/elevated/critical, reclaim_target_bytes: 回収目標(Noneは件数上限まで),
#     kinds: 対象kind(Noneは全kind), observed_monotonic_ns: 観測時刻, source: 発生源ラベル]
#   エラー: [ValueError: 未知version/level・負値・非int・非有限(fail-closed)]
# }
@dataclass(frozen=True)
class ResourcePressureSnapshot:
    level: str
    observed_monotonic_ns: int
    source: str
    reclaim_target_bytes: int | None = None
    kinds: frozenset[str] | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _is_int(self.schema_version) or self.schema_version != 1:
            raise ValueError("unsupported pressure snapshot schema_version")
        if self.level not in _PRESSURE_LEVELS:
            raise ValueError("unknown pressure level")
        target = self.reclaim_target_bytes
        if target is not None and (not _is_int(target) or target < 0):
            raise ValueError("reclaim_target_bytes must be a non-negative int")
        if not _is_int(self.observed_monotonic_ns) or self.observed_monotonic_ns < 0:
            raise ValueError("observed_monotonic_ns must be a non-negative int")
        if not isinstance(self.source, str) or len(self.source) > 128:
            raise ValueError("source must be a short string")
        if self.kinds is not None:
            kinds = frozenset(self.kinds)
            if not all(isinstance(k, str) for k in kinds):
                raise ValueError("kinds must be strings")
            object.__setattr__(self, "kinds", kinds)


# {
#   責務: [CleanupReport: 1回の解放処理の件数・量・時間(Trace/#787の入力)]
#   フィールド: [trigger: release/task/generation/session/pressure/expired, attempted〜skipped_not_evictable: 件数,
#     freed_estimated_bytes/unknown_bytes_count: 推定量, latency_ms: 所要時間, truncated: 上限で止めたか, target_met: 回収目標達成(目標なしはNone)]
# }
@dataclass(frozen=True)
class CleanupReport:
    trigger: str
    attempted: int = 0
    released: int = 0
    failed: int = 0
    deferred: int = 0
    skipped_pinned: int = 0
    skipped_in_use: int = 0
    skipped_not_evictable: int = 0
    freed_estimated_bytes: int = 0
    unknown_bytes_count: int = 0
    latency_ms: float = 0.0
    truncated: bool = False
    target_met: bool | None = None

    # {
    #   責務: [to_payload: JSON化できるdictへ変換する]
    #   引数: []
    #   戻り値: [dict: 値だけのpayload]
    # }
    def to_payload(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


# {
#   責務: [ResourceLifecycleSnapshot: Registryの現況(schema_version 1。#787の入力)]
#   フィールド: [session_state: OPEN/CLOSING/CLOSED, active/releasing/pinned_count: 件数, active_by_kind: kind別集計,
#     counters: 累計, last_cleanup: 直近のreport, max_cleanup_latency_ms: 最大所要時間, recent_failures: 直近の失敗(最大16件)]
# }
@dataclass(frozen=True)
class ResourceLifecycleSnapshot:
    session_id: str
    session_state: str
    current_generation: int
    active_count: int
    releasing_count: int
    pinned_count: int
    active_by_kind: Mapping[str, Mapping[str, int]]
    counters: Mapping[str, int]
    last_cleanup: CleanupReport | None
    max_cleanup_latency_ms: float
    recent_failures: tuple[Mapping[str, str], ...]
    schema_version: int = 1

    # {
    #   責務: [to_payload: JSON化できるdictへ変換する]
    #   引数: []
    #   戻り値: [dict: 値だけのpayload]
    # }
    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "session_state": self.session_state,
            "current_generation": self.current_generation,
            "active_count": self.active_count,
            "releasing_count": self.releasing_count,
            "pinned_count": self.pinned_count,
            "active_by_kind": {k: dict(v) for k, v in self.active_by_kind.items()},
            "counters": dict(self.counters),
            "last_cleanup": None
            if self.last_cleanup is None
            else self.last_cleanup.to_payload(),
            "max_cleanup_latency_ms": self.max_cleanup_latency_ms,
            "recent_failures": [dict(item) for item in self.recent_failures],
        }


# 内部状態: active / pending(解放待ち) / running(releaser実行中) / deferred(借用終了待ち)
@dataclass
class _Entry:
    lease: ResourceLease
    releaser: Any
    seq: int
    state: str = "active"
    borrows: int = 0


def _group(entry: _Entry) -> int:
    return {ResourceScope.TASK: 0, ResourceScope.GENERATION: 1}.get(
        entry.lease.owner_scope, 2
    )


def _close_order(entry: _Entry) -> tuple[int, int, int]:
    group = _group(entry)
    # GENERATIONは新しい世代から、同じ群の中は取得の逆順
    return (group, -entry.lease.generation if group == 1 else 0, -entry.seq)


# {
#   責務: [ResourceBorrow: borrow中のlease(context manager)。終了時に保留中の解放を実行する]
#   フィールド: [_registry/_entry: 借用元]
# }
class ResourceBorrow:
    def __init__(self, registry: "SessionResourceRegistry", entry: _Entry) -> None:
        self._registry = registry
        self._entry = entry
        self._done = False

    def __enter__(self) -> ResourceLease:
        return self._entry.lease

    def __exit__(self, *_exc: object) -> None:
        if not self._done:
            self._done = True
            self._registry._end_borrow(self._entry)


# {
#   責務: [SessionResourceRegistry: Session内resourceのlease・借用・解放を1か所で管理する(thread-safe)]
#   処理: [状態変更は1つのlock内、releaserとTrace sinkはlock外で呼ぶ。RELEASINGへの遷移で二重解放を防ぐ]
#   フィールド: [_entries: 活動中の表, _history: 固定長の終了記録, _kind_stats: kind別集計, _expiry: 期限の最小heap, _counters: 累計]
# }
class SessionResourceRegistry:
    # {
    #   責務: [__init__: Registryを作る]
    #   引数: [session_id: Session識別子, clock_ns: 単調時計, trace_sink: Trace受け口, initial_generation: 開始世代,
    #     max_active: 活動中の上限(1..65536), terminal_history: 終了履歴の長さ(0..4096)]
    #   戻り値: []
    #   エラー: [ValueError: 引数の範囲外]
    # }
    def __init__(
        self,
        session_id: str,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        trace_sink: ResourceTraceSink | None = None,
        initial_generation: int = 0,
        max_active: int = 4096,
        terminal_history: int = 256,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if not _is_int(max_active) or not 1 <= max_active <= 65536:
            raise ValueError("max_active must be 1..65536")
        if not _is_int(terminal_history) or not 0 <= terminal_history <= 4096:
            raise ValueError("terminal_history must be 0..4096")
        if not _is_int(initial_generation) or initial_generation < 0:
            raise ValueError("initial_generation must be a non-negative int")
        self.session_id = session_id
        self._clock = clock_ns
        self._trace = trace_sink
        self._max_active = max_active
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._terminal: OrderedDict[str, str] = OrderedDict()
        self._terminal_cap = terminal_history
        self._kind_stats: dict[str, list[int]] = {}
        self._expiry: list[tuple[int, int, str]] = []
        self._seq = 0
        self._generation = initial_generation
        self._closed_generations: set[int] = set()
        self._open_tasks: set[str] = set()
        self._state = "OPEN"
        self._n_pinned = 0
        self._stats_updates = 0
        self._last_cleanup: CleanupReport | None = None
        self._max_latency_ms = 0.0
        self._failures: deque[dict[str, str]] = deque(maxlen=16)
        self._counters = {
            name: 0
            for name in (
                "acquired",
                "released",
                "evicted",
                "expired",
                "release_failed",
                "stale_rejected",
                "double_release",
                "deferred",
                "trace_failed",
            )
        }

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    # ---- 取得 ----

    # {
    #   責務: [acquire: ownerが作った資源をleaseとして登録する(consumerへ渡す前に呼ぶ)]
    #   処理: [引数と宣言を検証し、scopeからowner_idを決め、集計と期限heapを更新する。例外時の解放はowner側の責任]
    #   引数: [resource_id/kind: 識別, releaser: 解放Adapter, owner_scope: SESSION/GENERATION/TASK, estimated_bytes: 推定量,
    #     ttl_ms: 寿命(1以上の有限値), pinned/evictable/priority: eviction方針, provenance: 取得箇所ラベル, generation: 省略時はcurrent]
    #   戻り値: [ResourceLease: 登録されたlease]
    #   エラー: [ResourceLifecycleError: 閉鎖・stale・重複・不正宣言・容量超過など]
    # }
    def acquire(
        self,
        resource_id: str,
        kind: str,
        releaser: ResourceReleaser,
        *,
        owner_scope: ResourceScope,
        estimated_bytes: int | None = None,
        ttl_ms: float | None = None,
        pinned: bool = False,
        evictable: bool = False,
        priority: int = 500,
        provenance: str = "",
        generation: int | None = None,
    ) -> ResourceLease:
        if not isinstance(owner_scope, ResourceScope):
            raise ResourceLifecycleError("invalid_lease", "owner_scope")
        if owner_scope is ResourceScope.PROCESS:
            raise ResourceLifecycleError("scope_not_supported", "process scope")
        if not isinstance(resource_id, str) or not _ID_PATTERN.match(resource_id):
            raise ResourceLifecycleError("invalid_lease", "resource_id")
        if not isinstance(kind, str) or not _KIND_PATTERN.match(kind):
            raise ResourceLifecycleError("invalid_lease", "kind")
        if not callable(getattr(releaser, "release", None)):
            raise ResourceLifecycleError("invalid_lease", "releaser")
        if estimated_bytes is not None and (
            not _is_int(estimated_bytes) or estimated_bytes < 0
        ):
            raise ResourceLifecycleError("invalid_lease", "estimated_bytes")
        if ttl_ms is not None and (
            isinstance(ttl_ms, bool)
            or not isinstance(ttl_ms, (int, float))
            or not math.isfinite(ttl_ms)
            or ttl_ms < 1
        ):
            raise ResourceLifecycleError("invalid_lease", "ttl_ms")
        if not isinstance(pinned, bool) or not isinstance(evictable, bool):
            raise ResourceLifecycleError("invalid_lease", "flags")
        if pinned and (ttl_ms is not None or evictable):
            raise ResourceLifecycleError(
                "invalid_lease", "pinned conflicts with ttl/evictable"
            )
        if not _is_int(priority) or not 0 <= priority <= 1000:
            raise ResourceLifecycleError("invalid_lease", "priority")
        if not isinstance(provenance, str) or len(provenance) > 128:
            raise ResourceLifecycleError("invalid_lease", "provenance")
        if generation is not None and not _is_int(generation):
            raise ResourceLifecycleError("invalid_lease", "generation")
        now = self._clock()
        with self._lock:
            self._require_open()
            if generation is not None and generation != self._generation:
                self._counters["stale_rejected"] += 1
                raise ResourceLifecycleError("stale_generation", "acquire")
            if self._generation in self._closed_generations:
                raise ResourceLifecycleError("generation_closed", "acquire")
            if owner_scope is ResourceScope.SESSION:
                owner_id = self.session_id
            elif owner_scope is ResourceScope.GENERATION:
                owner_id = f"gen-{self._generation}"
            else:
                owner_id = self._current_task()
                if owner_id is None:
                    raise ResourceLifecycleError("no_active_task", "acquire")
            if resource_id in self._entries:
                raise ResourceLifecycleError("duplicate_resource_id", "acquire")
            if len(self._entries) >= self._max_active:
                raise ResourceLifecycleError("capacity_exceeded", "acquire")
            expires = None if ttl_ms is None else now + int(ttl_ms * 1_000_000)
            lease = ResourceLease(
                resource_id,
                kind,
                owner_scope,
                owner_id,
                self._generation,
                estimated_bytes,
                now,
                expires,
                pinned,
                evictable,
                priority,
                provenance,
            )
            self._seq += 1
            entry = _Entry(lease, releaser, self._seq)
            self._entries[resource_id] = entry
            self._terminal.pop(resource_id, None)
            self._adjust_stats(lease, +1)
            if pinned:
                self._n_pinned += 1
            if expires is not None:
                heapq.heappush(self._expiry, (expires, entry.seq, resource_id))
                self._rebuild_expiry_if_needed()
            self._counters["acquired"] += 1
            return lease

    # {
    #   責務: [borrow: 世代を検査してleaseを借用する(context manager)]
    #   引数: [resource_id: 対象, expected_generation: 呼出元が前提とする世代]
    #   戻り値: [ResourceBorrow: withで使うcontext manager]
    #   エラー: [ResourceLifecycleError: registry_closed/not_active/stale_generation]
    # }
    def borrow(self, resource_id: str, *, expected_generation: int) -> ResourceBorrow:
        with self._lock:
            self._require_open()
            entry = self._entries.get(resource_id)
            if entry is None or entry.state != "active":
                raise ResourceLifecycleError("not_active", "borrow")
            lease = entry.lease
            if lease.owner_scope is not ResourceScope.SESSION and (
                lease.generation != expected_generation
                or lease.generation != self._generation
            ):
                self._counters["stale_rejected"] += 1
                raise ResourceLifecycleError("stale_generation", "borrow")
            entry.borrows += 1
            return ResourceBorrow(self, entry)

    # ---- 解放 ----

    # {
    #   責務: [release: 1件を冪等に解放する(例外を送出しない)]
    #   引数: [resource_id: 対象]
    #   戻り値: [ResourceState|None: 解放後(または現在)の状態。未知IDはNone]
    # }
    def release(self, resource_id: str) -> ResourceState | None:
        with self._lock:
            entry = self._entries.get(resource_id)
            if entry is None:
                state = self._terminal.get(resource_id)
                if state is None:
                    return None
                self._counters["double_release"] += 1
                return ResourceState(state)
            if entry.state != "active":
                self._counters["double_release"] += 1
                return ResourceState.RELEASING
            self._claim(entry)
            if entry.state == "deferred":
                return ResourceState.RELEASING
        report = self._drain([entry], "release", budget_ms=None)
        return ResourceState.FAILED if report.failed else ResourceState.RELEASED

    # {
    #   責務: [task_scope: task終了時(正常・例外・cancel・timeout)に同じ解放経路を通すcontext manager]
    #   引数: [task_id: task識別子]
    #   戻り値: [Iterator: with用]
    #   エラー: [ResourceLifecycleError: registry_closed/invalid_lease(同じtask idの入れ子)]
    # }
    @contextmanager
    def task_scope(self, task_id: str) -> Iterator[None]:
        if not isinstance(task_id, str) or not _ID_PATTERN.match(task_id):
            raise ResourceLifecycleError("invalid_lease", "task_id")
        with self._lock:
            self._require_open()
            if task_id in self._open_tasks:
                raise ResourceLifecycleError("invalid_lease", "nested task_id")
            self._open_tasks.add(task_id)
        reset = _CURRENT_TASKS.set(_CURRENT_TASKS.get() + ((id(self), task_id),))
        try:
            yield
        finally:
            _CURRENT_TASKS.reset(reset)
            try:
                self.close_task(task_id)
            finally:
                with self._lock:
                    self._open_tasks.discard(task_id)

    # {
    #   責務: [close_task: そのtaskのTASK資源を解放する]
    #   引数: [task_id: 対象]
    #   戻り値: [CleanupReport]
    # }
    def close_task(self, task_id: str) -> CleanupReport:
        with self._lock:
            entries = self._claim_where(
                lambda e: (
                    e.lease.owner_scope is ResourceScope.TASK
                    and e.lease.owner_id == task_id
                )
            )
        return self._drain(entries, "task", budget_ms=None)

    # {
    #   責務: [advance_generation: 世代を進め、旧世代のGENERATION/TASK資源を同じlock内でpending化して解放する]
    #   引数: [generation: 新しい世代(currentより大きい値)]
    #   戻り値: [CleanupReport]
    #   エラー: [ResourceLifecycleError: stale_generation/registry_closed]
    # }
    def advance_generation(self, generation: int) -> CleanupReport:
        if not _is_int(generation):
            raise ResourceLifecycleError("invalid_lease", "generation")
        with self._lock:
            self._require_open()
            if generation <= self._generation:
                self._counters["stale_rejected"] += 1
                raise ResourceLifecycleError("stale_generation", "advance")
            self._generation = generation
            self._closed_generations = {
                g for g in self._closed_generations if g >= generation
            }
            entries = self._claim_where(
                lambda e: (
                    e.lease.owner_scope is not ResourceScope.SESSION
                    and e.lease.generation < generation
                )
            )
        return self._drain(entries, "generation", budget_ms=None)

    # {
    #   責務: [close_generation: 指定世代のGENERATION/TASK資源を解放し、その世代を閉じる]
    #   引数: [generation: 対象世代]
    #   戻り値: [CleanupReport]
    # }
    def close_generation(self, generation: int) -> CleanupReport:
        with self._lock:
            self._closed_generations.add(generation)
            entries = self._claim_where(
                lambda e: (
                    e.lease.owner_scope is not ResourceScope.SESSION
                    and e.lease.generation == generation
                )
            )
        return self._drain(entries, "generation", budget_ms=None)

    # {
    #   責務: [close_session: Sessionを閉じ、TASK→GENERATION(新しい順)→SESSIONの順に取得の逆順で解放する]
    #   処理: [時間上限で打ち切った分はpendingのまま残し、再呼出しで続きから処理する]
    #   引数: [budget_ms: 時間上限(有限・正)]
    #   戻り値: [CleanupReport]
    #   エラー: [ValueError: budget_msが不正]
    # }
    def close_session(self, *, budget_ms: float = 2000.0) -> CleanupReport:
        _check_budget(budget_ms)
        with self._lock:
            if self._state == "OPEN":
                self._state = "CLOSING"
            self._claim_where(lambda e: True)
            entries = [e for e in self._entries.values() if e.state == "pending"]
        report = self._drain(entries, "session", budget_ms=budget_ms)
        with self._lock:
            deferred = sum(1 for e in self._entries.values() if e.state == "deferred")
            if not self._entries:
                self._state = "CLOSED"
        if deferred:
            report = _replace(report, deferred=deferred)
        return report

    # {
    #   責務: [snapshot: 現況を履歴・heapを走査せず返す]
    #   引数: []
    #   戻り値: [ResourceLifecycleSnapshot]
    # }
    def snapshot(self) -> ResourceLifecycleSnapshot:
        with self._lock:
            active = sum(1 for e in self._entries.values() if e.state == "active")
            return ResourceLifecycleSnapshot(
                session_id=self.session_id,
                session_state=self._state,
                current_generation=self._generation,
                active_count=active,
                releasing_count=len(self._entries) - active,
                pinned_count=self._n_pinned,
                active_by_kind={
                    kind: {"count": s[0], "known_bytes": s[1], "unknown_count": s[2]}
                    for kind, s in self._kind_stats.items()
                },
                counters=dict(self._counters),
                last_cleanup=self._last_cleanup,
                max_cleanup_latency_ms=self._max_latency_ms,
                recent_failures=tuple(dict(f) for f in self._failures),
            )

    # ---- 内部(lockを保持して呼ぶものは名前に注記) ----

    def _require_open(self) -> None:
        if self._state != "OPEN":
            raise ResourceLifecycleError("registry_closed")

    def _current_task(self) -> str | None:
        for registry_id, task_id in reversed(_CURRENT_TASKS.get()):
            if registry_id == id(self):
                return task_id
        return None

    def _adjust_stats(self, lease: ResourceLease, sign: int) -> None:
        self._stats_updates += 1
        stats = self._kind_stats.setdefault(lease.kind, [0, 0, 0])
        stats[0] += sign
        if lease.estimated_bytes is None:
            stats[2] += sign
        else:
            stats[1] += sign * lease.estimated_bytes
        if stats[0] == 0:
            del self._kind_stats[lease.kind]

    def _rebuild_expiry_if_needed(self) -> None:
        if len(self._expiry) > 2 * len(self._entries) + 64:
            self._expiry = [
                (e.lease.expires_monotonic_ns, e.seq, rid)
                for rid, e in self._entries.items()
                if e.lease.expires_monotonic_ns is not None and e.state == "active"
            ]
            heapq.heapify(self._expiry)

    def _record_terminal(self, resource_id: str, state: str) -> None:
        # lock保持。固定長の終了記録(ID→状態)。上限を超えたら古い順に捨てる
        if self._terminal_cap == 0:
            return
        self._terminal[resource_id] = state
        self._terminal.move_to_end(resource_id)
        while len(self._terminal) > self._terminal_cap:
            self._terminal.popitem(last=False)

    def _claim(self, entry: _Entry) -> None:
        # lock保持。借用中はdeferred、それ以外はpendingにする
        if entry.state == "active":
            entry.state = "deferred" if entry.borrows else "pending"
            if entry.state == "deferred":
                self._counters["deferred"] += 1

    def _claim_where(self, predicate: Callable[[_Entry], bool]) -> list[_Entry]:
        # lock保持。対象のactiveをclaimし、解放待ち(pending)の一覧を返す
        picked = []
        for entry in list(self._entries.values()):
            if entry.state == "active" and predicate(entry):
                self._claim(entry)
        for entry in self._entries.values():
            if entry.state == "pending" and predicate(entry):
                picked.append(entry)
        return picked

    def _end_borrow(self, entry: _Entry) -> None:
        with self._lock:
            entry.borrows -= 1
            if entry.borrows or entry.state != "deferred":
                return
            entry.state = "pending"
        self._drain([entry], "release", budget_ms=None)

    def _drain(
        self,
        entries: list[_Entry],
        trigger: str,
        *,
        budget_ms: float | None,
        reason: str = "",
    ) -> CleanupReport:
        start = self._clock()
        attempted = released = failed = freed = unknown = 0
        truncated = False
        for entry in sorted(entries, key=_close_order):
            if (
                budget_ms is not None
                and (self._clock() - start) / 1e6 >= budget_ms
                and attempted
            ):
                truncated = True
                break
            ok = self._run_release(entry, reason)
            if ok is None:
                continue
            attempted += 1
            if ok:
                released += 1
                if entry.lease.estimated_bytes is None:
                    unknown += 1
                else:
                    freed += entry.lease.estimated_bytes
            else:
                failed += 1
        report = CleanupReport(
            trigger,
            attempted=attempted,
            released=released,
            failed=failed,
            freed_estimated_bytes=freed,
            unknown_bytes_count=unknown,
            latency_ms=(self._clock() - start) / 1e6,
            truncated=truncated,
        )
        self._finish(report)
        return report

    def _run_release(self, entry: _Entry, reason: str = "") -> bool | None:
        # pending→running の権利を得た1者だけがreleaserを呼ぶ。権利が無ければNone
        with self._lock:
            if entry.state != "pending":
                return None
            entry.state = "running"
        error_type = None
        try:
            entry.releaser.release(entry.lease.resource_id)
        except Exception as exc:  # 1件の失敗で他の解放を止めない
            error_type = type(exc).__name__
        failure_payload = None
        with self._lock:
            lease = entry.lease
            self._entries.pop(lease.resource_id, None)
            self._adjust_stats(lease, -1)
            if lease.pinned:
                self._n_pinned -= 1
            if error_type is None:
                self._record_terminal(lease.resource_id, "released")
                self._counters["released"] += 1
                if reason:
                    self._counters[reason] += 1
            else:
                self._record_terminal(lease.resource_id, "failed")
                self._counters["release_failed"] += 1
                self._failures.append(
                    {
                        "resource_id": lease.resource_id,
                        "kind": lease.kind,
                        "error_type": error_type,
                    }
                )
                failure_payload = {
                    "resource_id": lease.resource_id,
                    "kind": lease.kind,
                    "owner_scope": lease.owner_scope.value,
                    "generation": lease.generation,
                    "error_type": error_type,
                }
        if failure_payload is not None:
            self._emit("resource.release_failed/v1", failure_payload)
        return error_type is None

    def _finish(self, report: CleanupReport) -> None:
        with self._lock:
            self._last_cleanup = report
            self._max_latency_ms = max(self._max_latency_ms, report.latency_ms)
        noisy = (
            report.attempted,
            report.deferred,
            report.truncated,
            report.skipped_pinned,
            report.skipped_in_use,
            report.skipped_not_evictable,
        )
        if report.trigger != "release" and any(noisy):
            self._emit("resource.cleanup/v1", report.to_payload())

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        if self._trace is None:
            return
        try:
            self._trace.record(event, payload)
        except Exception:  # Traceの失敗で解放を止めない
            with self._lock:
                self._counters["trace_failed"] += 1


def _check_budget(budget_ms: float) -> None:
    if (
        isinstance(budget_ms, bool)
        or not isinstance(budget_ms, (int, float))
        or not math.isfinite(budget_ms)
        or budget_ms <= 0
    ):
        raise ValueError("budget_ms must be a finite positive number")


def _replace(report: CleanupReport, **changes: Any) -> CleanupReport:
    values = {name: getattr(report, name) for name in report.__dataclass_fields__}
    values.update(changes)
    return CleanupReport(**values)


# {
#   責務: [CleanupCoordinator: pressureと期限切れ時の上限付き解放順序を決める]
#   フィールド: [_registry: 対象Registry, _clock: 単調時計]
# }
class CleanupCoordinator:
    # {
    #   責務: [__init__: Registryと時計を受け取る]
    #   引数: [registry: 対象, clock_ns: 単調時計(省略時はRegistryと同じ)]
    #   戻り値: []
    # }
    def __init__(
        self,
        registry: SessionResourceRegistry,
        *,
        clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock_ns or registry._clock

    # {
    #   責務: [on_pressure: levelの上限内で期限切れ→priority→古い順→idの順にevictableな資源を解放する]
    #   引数: [snapshot: 検証済みの資源圧]
    #   戻り値: [CleanupReport: skip件数・target_met・truncatedを含む]
    #   エラー: [ValueError: snapshotが不正]
    # }
    def on_pressure(self, snapshot: ResourcePressureSnapshot) -> CleanupReport:
        if not isinstance(snapshot, ResourcePressureSnapshot):
            raise ValueError("snapshot must be a ResourcePressureSnapshot")
        registry = self._registry
        if snapshot.level == "none":
            report = CleanupReport("pressure")
            registry._finish(report)
            return report
        max_items, budget_ms = PRESSURE_LIMITS[snapshot.level]
        target = snapshot.reclaim_target_bytes
        now = self._clock()
        pinned = in_use = not_evictable = 0
        candidates: list[_Entry] = []
        with registry._lock:
            for entry in registry._entries.values():
                lease = entry.lease
                if entry.state != "active":
                    continue
                if snapshot.kinds is not None and lease.kind not in snapshot.kinds:
                    continue
                if lease.pinned:
                    pinned += 1
                elif entry.borrows:
                    in_use += 1
                elif not lease.evictable:
                    not_evictable += 1
                else:
                    candidates.append(entry)
        candidates.sort(
            key=lambda e: (
                0
                if e.lease.expires_monotonic_ns is not None
                and now >= e.lease.expires_monotonic_ns
                else 1,
                e.lease.priority,
                e.lease.created_monotonic_ns,
                e.lease.resource_id,
            )
        )
        return self._bounded_release(
            candidates,
            "pressure",
            max_items=max_items,
            budget_ms=budget_ms,
            target=target,
            skipped=(pinned, in_use, not_evictable),
            reason="evicted",
        )

    # {
    #   責務: [cleanup_expired: 期限切れで借用数0の資源をevictableに関係なく上限内で解放する]
    #   引数: [max_items: 1..1024, budget_ms: 有限・正]
    #   戻り値: [CleanupReport]
    #   エラー: [ValueError: 引数が範囲外]
    # }
    def cleanup_expired(
        self, *, max_items: int = 64, budget_ms: float = 20.0
    ) -> CleanupReport:
        if not _is_int(max_items) or not 1 <= max_items <= 1024:
            raise ValueError("max_items must be 1..1024")
        _check_budget(budget_ms)
        registry = self._registry
        now = self._clock()
        candidates: list[_Entry] = []
        in_use: list[tuple[int, int, str]] = []
        with registry._lock:
            heap = registry._expiry
            while heap and heap[0][0] <= now:
                item = heapq.heappop(heap)
                entry = registry._entries.get(item[2])
                if entry is None or entry.seq != item[1] or entry.state != "active":
                    continue
                if entry.borrows:
                    in_use.append(item)
                else:
                    candidates.append(entry)
            for item in in_use:
                heapq.heappush(heap, item)
        report = self._bounded_release(
            candidates,
            "expired",
            max_items=max_items,
            budget_ms=budget_ms,
            target=None,
            skipped=(0, len(in_use), 0),
            reason="expired",
        )
        return report

    def _bounded_release(
        self,
        candidates: list[_Entry],
        trigger: str,
        *,
        max_items: int,
        budget_ms: float,
        target: int | None,
        skipped: tuple[int, int, int],
        reason: str,
    ) -> CleanupReport:
        registry = self._registry
        start = self._clock()
        attempted = released = failed = freed = unknown = 0
        truncated = False
        stopped_by_target = False
        for index, entry in enumerate(candidates):
            if target is not None and freed >= target:
                stopped_by_target = True
                break
            if attempted >= max_items or (self._clock() - start) / 1e6 >= budget_ms:
                truncated = True
                break
            with registry._lock:
                # 借用が始まっていたら解放せず次回へ回す
                if entry.state != "active" or entry.borrows:
                    skipped = (skipped[0], skipped[1] + 1, skipped[2])
                    continue
                registry._claim(entry)
            ok = registry._run_release(entry, reason)
            if ok is None:
                continue
            attempted += 1
            if ok:
                released += 1
                if entry.lease.estimated_bytes is None:
                    unknown += 1
                else:
                    freed += entry.lease.estimated_bytes
            else:
                failed += 1
        if trigger == "pressure" and target is not None:
            target_met: bool | None = freed >= target
        else:
            target_met = None
        report = CleanupReport(
            trigger,
            attempted=attempted,
            released=released,
            failed=failed,
            skipped_pinned=skipped[0],
            skipped_in_use=skipped[1],
            skipped_not_evictable=skipped[2],
            freed_estimated_bytes=freed,
            unknown_bytes_count=unknown,
            latency_ms=(self._clock() - start) / 1e6,
            truncated=truncated and not stopped_by_target,
            target_met=target_met,
        )
        registry._finish(report)
        return report


__all__ = [
    "CleanupCoordinator",
    "CleanupReport",
    "ClosableOwnerAdapter",
    "PRESSURE_LIMITS",
    "ResourceBorrow",
    "ResourceLease",
    "ResourceLifecycleError",
    "ResourceLifecycleSnapshot",
    "ResourcePressureSnapshot",
    "ResourceReleaser",
    "ResourceScope",
    "ResourceState",
    "ResourceTraceSink",
    "SessionResourceRegistry",
]
