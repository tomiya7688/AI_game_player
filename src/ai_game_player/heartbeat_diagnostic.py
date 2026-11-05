"""明示検証時だけのheartbeat固定metadata。authority時刻・入力・永続化を変更しない。"""

from collections import deque
from dataclasses import dataclass
import json
import math
import time
from typing import cast


_MARKS = {
    "entry_mono_ns": "entry",
    "lock_acquired_mono_ns": "runtime_lock",
    "persisted_read_started_mono_ns": "persisted_read",
    "persisted_read_finished_mono_ns": "persisted_read",
    "renewal_mono_ns": "renewal",
    "lease_write_started_mono_ns": "lease_write",
    "lease_write_finished_mono_ns": "lease_write",
    "finished_mono_ns": "complete",
}
_RESULTS = frozenset(
    (
        "pending",
        "renewed",
        "not_active",
        "lease_expired",
        "persisted_stop",
        "storage_failure",
        "raised",
    )
)
_SUMMARY_SAMPLES = 2048


# {
#   責務: [_sample_monotonic_ns: 診断専用の同一process内monotonic nsを取得する]
#   引数: [なし] 戻り値: [int: 診断用時刻。leaseのUnix時計とは比較しない]
#   エラー: [時計取得の失敗は呼出元markが隔離し、入力許可へ使用しない]
# }
def _sample_monotonic_ns() -> int:
    return time.monotonic_ns()


# {
#   責務: [_Entry: 一heartbeatの固定長診断と完了状態を所有する]
#   フィールド: [sequence: 有界連番, phase/result: 固定enum語彙, clock_invalid: 診断値不正,
#     *_mono_ns: 同一processの処理時刻または未計測None,
#     renewal_unix_seconds/lease_expires_unix_seconds: 既存renewが取得したUnix秒の複製,
#     _last_ns/_done: 診断内の後退検査と二重完了防止。authorityやStopを変更しない]
# }
@dataclass(slots=True)
class _Entry:
    sequence: int
    phase: str = "entry"
    result: str = "pending"
    clock_invalid: bool = False
    entry_mono_ns: int | None = None
    lock_acquired_mono_ns: int | None = None
    renewal_mono_ns: int | None = None
    persisted_read_started_mono_ns: int | None = None
    persisted_read_finished_mono_ns: int | None = None
    lease_write_started_mono_ns: int | None = None
    lease_write_finished_mono_ns: int | None = None
    finished_mono_ns: int | None = None
    renewal_unix_seconds: float | None = None
    lease_expires_unix_seconds: float | None = None
    _last_ns: int = 0
    _done: bool = False


# {
#   責務: [HeartbeatDiagnostics: last8と進行中1だけを保持する検証専用memory recorder]
#   フィールド: [_completed: 最大8件の完了record, _in_progress: 最新の進行中1件,
#     _sequence: 診断専用連番。入力命令の連番・世代とは独立]
#   処理: [全入口をbest-effortにし、Runtime lock/file write/authority clock呼出しを持たない]
# }
class HeartbeatDiagnostics:
    # {
    #   責務: [__init__: 上限8の完了ringと空の進行中recordを準備する]
    #   引数: [なし] 戻り値: [None: 時計取得・file I/Oをせず初期化]
    #   エラー: [確保失敗はconfigure側で診断OFFへ隔離する]
    # }
    def __init__(self) -> None:
        self._completed: deque[_Entry] = deque(maxlen=8)
        self._in_progress: _Entry | None = None
        self._sequence = 0
        self._renewals = 0
        self._negative_margins = 0
        self._margin_count = 0
        self._margin_min: float | None = None
        self._margins_ms: deque[float] = deque(maxlen=_SUMMARY_SAMPLES)
        self._durations_ms: deque[float] = deque(maxlen=_SUMMARY_SAMPLES)
        self._previous_renewal: _Entry | None = None

    # {
    #   責務: [begin: heartbeat入口の診断連番とmonotonic時刻を固定する]
    #   引数: [なし] 戻り値: [_EntryまたはNone: 上限・確保失敗時は記録しない]
    #   処理: [診断失敗でもheartbeat実行やauthority時計を変更しない]
    # }
    def begin(self) -> _Entry | None:
        try:
            if self._sequence >= 10**12:
                return None
            self._sequence += 1
            row = _Entry(self._sequence)
            self._in_progress = row
            self.mark(row, "entry_mono_ns")
            return row
        except Exception:
            return None

    # {
    #   責務: [mark: 許可したphaseの同一process内monotonic時刻だけを記録する]
    #   引数: [row: 進行中recordまたはNone, field: 固定phase名] 戻り値: [なし]
    #   処理: [後退・不正値・取得失敗はclock_invalidへ落とし、元処理を妨げない]
    # }
    def mark(self, row: _Entry | None, field: str) -> None:
        try:
            if row is None or row._done or field not in _MARKS:
                return
            row.phase = _MARKS[field]
            value = _sample_monotonic_ns()
            if type(value) is not int or not row._last_ns <= value <= 2**63 - 1:
                row.clock_invalid = True
                return
            setattr(row, field, value)
            row._last_ns = value
        except Exception:
            try:
                if row is not None:
                    row.clock_invalid = True
            except Exception:
                pass

    # {
    #   責務: [renewal: 既存renewが取得したUnix秒と元lease期限を複製する]
    #   引数: [row: 対象record, renewed/expires: 同じauthority時計の既存値]
    #   戻り値: [なし] 処理: [追加の時計取得・期限更新はせず、不正値や記録失敗を隔離]
    # }
    def renewal(self, row: _Entry | None, renewed: float, expires: float) -> None:
        try:
            if row is None or row._done:
                return
            if (
                not _valid_wall(renewed)
                or not _valid_wall(expires)
                or renewed > expires
            ):
                row.clock_invalid = True
                return
            row.renewal_unix_seconds = renewed
            row.lease_expires_unix_seconds = expires
        except Exception:
            pass

    # {
    #   責務: [finish: 固定結果とmonotonic完了時刻を最大8件のringへ保持する]
    #   引数: [row: 対象recordまたはNone, result: 固定結果語彙] 戻り値: [なし]
    #   処理: [二重完了や記録失敗は無視し、元の結果・例外・Stopを変更しない]
    # }
    def finish(self, row: _Entry | None, result: str) -> None:
        try:
            if row is None or row._done or result not in _RESULTS:
                return
            self.mark(row, "finished_mono_ns")
            row.result = result
            row._done = True
            self._completed.append(row)
            if self._in_progress is row:
                self._in_progress = None
            self._summarize(row)
        except Exception:
            pass

    # {
    #   責務: [_summarize: 完了heartbeatの処理時間と前回期限に対する保存完了の余裕を有界に集計する]
    #   引数: [row: 完了record] 戻り値: [なし]
    #   処理: [余裕ms = 前回heartbeatの期限 − (今回の更新Unix秒 + 更新から書込完了までのmonotonic差)。
    #     main threadの更新は含まないため実際の余裕の下限。連番が途切れた・時計不正の行は余裕を計算しない]
    # }
    def _summarize(self, row: _Entry) -> None:
        if row.result != "renewed" or row.clock_invalid:
            self._previous_renewal = None
            return
        self._renewals += 1
        if row.entry_mono_ns is not None and row.finished_mono_ns is not None:
            self._durations_ms.append((row.finished_mono_ns - row.entry_mono_ns) / 1e6)
        previous = self._previous_renewal
        self._previous_renewal = row
        if (
            previous is None
            or previous.sequence + 1 != row.sequence
            or previous.lease_expires_unix_seconds is None
            or row.renewal_unix_seconds is None
            or row.renewal_mono_ns is None
            or row.lease_write_finished_mono_ns is None
        ):
            return
        committed = (
            row.renewal_unix_seconds
            + (row.lease_write_finished_mono_ns - row.renewal_mono_ns) / 1e9
        )
        margin = (previous.lease_expires_unix_seconds - committed) * 1000
        self._margins_ms.append(margin)
        self._margin_count += 1
        if self._margin_min is None or margin < self._margin_min:
            self._margin_min = margin
        if margin < 0:
            self._negative_margins += 1

    # {
    #   責務: [summary: 集計値を固定keyの有限数値metadataへ複製する]
    #   引数: [なし] 戻り値: [dict: 標本が無い統計はNone]
    #   処理: [renewals/margin_samples/negative_margin_count/margin_ms_minは全期間の累積、
    #     p05/p50/cycle統計は直近window_samples件。余裕を計算できない更新はrenewals−margin_samplesに現れる]
    # }
    def summary(self) -> dict[str, object]:
        margins = sorted(self._margins_ms)
        durations = sorted(self._durations_ms)
        return {
            "renewals": self._renewals,
            "margin_samples": self._margin_count,
            "negative_margin_count": self._negative_margins,
            "margin_ms_min": (
                None if self._margin_min is None else _rounded(self._margin_min)
            ),
            "window_samples": _SUMMARY_SAMPLES,
            "margin_ms_p05": _percentile(margins, 0.05),
            "margin_ms_p50": _percentile(margins, 0.50),
            "duration_ms_p50": _percentile(durations, 0.50),
            "duration_ms_p95": _percentile(durations, 0.95),
            "duration_ms_max": _rounded(durations[-1]) if durations else None,
        }

    # {
    #   責務: [snapshot: 完了8件と進行中1件を32KiB未満の固定metadataへ複製する]
    #   引数: [なし] 戻り値: [dictまたはNone: 途中更新・OOM・不正値は診断欠落]
    #   処理: [Runtime lock・file I/O・時計取得を行わず、元の失敗やcleanupを妨げない]
    # }
    def snapshot(self) -> dict[str, object] | None:
        """Runtime lock無し。途中更新/OOM/不正値は診断なしへ戻す。file I/Oは行わない。"""
        try:
            rows = tuple(self._completed)
            pending = self._in_progress
            if len(rows) > 8 or (pending is not None and pending._done):
                return None
            completed = [_export(row) for row in rows]
            current = None if pending is None else _export(pending)
            if any(row is None for row in completed) or (
                pending is not None and current is None
            ):
                return None
            result: dict[str, object] = {
                "schema_version": 2,
                "clock_scope": "current_parent_process_only",
                "capacity": 8,
                "completed": completed,
                "in_progress": current,
                "summary": self.summary(),
            }
            if (
                len(
                    json.dumps(result, ensure_ascii=True, allow_nan=False).encode(
                        "utf-8"
                    )
                )
                >= 32 * 1024
            ):
                return None
            return result
        except Exception:
            return None


# {
#   責務: [_percentile/_rounded: 整列済み標本の最近傍順位値を小数3桁の有限値で返す]
#   引数: [ordered: 昇順標本, fraction: 0..1] 戻り値: [floatまたはNone: 標本なし・非有限はNone]
# }
def _percentile(ordered: list[float], fraction: float) -> float | None:
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return _rounded(ordered[index])


def _rounded(value: float) -> float | None:
    return round(value, 3) if math.isfinite(value) else None


# {
#   責務: [_valid_wall:複製したUnix秒を有限かつ有界な診断値へ限定する]
#   引数: [value: 診断へ複製する既存値] 戻り値: [bool: 型・範囲・有限性の適合]
#   処理: [bool・巨大整数・非数値を先に拒否し、時計やauthorityは更新しない]
# }
def _valid_wall(value: object) -> bool:
    return (
        type(value) in (int, float)
        and 0 <= cast(float, value) <= 10**12
        and math.isfinite(cast(float, value))
    )


# {
#   責務: [_export: 一recordの固定enum・単調時刻・Unix秒を検証して公開する]
#   引数: [row: recorder所有record] 戻り値: [dictまたはNone: 不正metadataを公開しない]
#   処理: [時計域を分離し、読取例外はsnapshot側で隔離する。入力許可へ利用しない]
# }
def _export(row: _Entry) -> dict[str, object] | None:
    if (
        type(row) is not _Entry
        or type(row.sequence) is not int
        or not 1 <= row.sequence <= 10**12
        or row.phase not in _MARKS.values()
        or row.result not in _RESULTS
        or type(row.clock_invalid) is not bool
    ):
        return None
    result: dict[str, object] = {
        "sequence": row.sequence,
        "phase": row.phase,
        "result": row.result,
        "clock_invalid": row.clock_invalid,
    }
    previous = 0
    for field in _MARKS:
        value = getattr(row, field)
        if value is not None:
            if type(value) is not int or not previous <= value <= 2**63 - 1:
                return None
            previous = value
        result[field] = value
    for field in ("renewal_unix_seconds", "lease_expires_unix_seconds"):
        value = getattr(row, field)
        if value is not None and not _valid_wall(value):
            return None
        result[field] = value
    return result
