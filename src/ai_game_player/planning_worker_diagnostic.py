"""互換workerの固定metadata診断。状態・例外文言・stderrを収集しない。"""

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import time
from typing import Any


# {
#   責務: [WorkerPhase: trusted wrapperの段階を固定enumで限定する]
#   フィールド: [固定enumまたは相対時刻だけ。状態・message・stderrを含まない]
# }
class WorkerPhase(str, Enum):
    BOOTSTRAP = "bootstrap"
    RESOURCE_LIMITS = "resource_limits"
    REQUEST_READ = "request_read"
    READY_PUBLISH = "ready_publish"
    FACTORY_IMPORT = "factory_import"
    FACTORY_CREATE = "factory_create"
    STATE_DECODE = "state_decode"
    SEARCH = "search"
    RESULT_BUILD = "result_build"
    CHECKPOINT_PUBLISH = "checkpoint_publish"
    FINISHED_PUBLISH = "finished_publish"


_CATEGORIES = frozenset(
    ("memory", "os", "import", "validation", "runtime", "base_exception", "other")
)
_FAILURE_FIELDS = frozenset(
    (
        "schema_version",
        "phase",
        "exception_category",
        "errno",
        "winerror",
        "phase_elapsed_ns",
        "exception_elapsed_ns",
    )
)


# {
#   責務: [_code: 符号付き/Windows整数codeだけを有界に保持する]
#   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
# }
def _code(value: object) -> int | None:
    return value if type(value) is int and -(2**31) <= value <= 2**32 - 1 else None


# {
#   責務: [_category: 例外の任意class名や文言を固定分類へ写す]
#   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
# }
def _category(error: BaseException) -> str:
    if isinstance(error, MemoryError):
        return "memory"
    if isinstance(error, OSError):
        return "os"
    if isinstance(error, ImportError):
        return "import"
    if isinstance(error, (ValueError, TypeError, OverflowError)):
        return "validation"
    if isinstance(error, RuntimeError):
        return "runtime"
    return "other" if isinstance(error, Exception) else "base_exception"


# {
#   責務: [WorkerDiagnostic: 一worker内のphaseと相対monotonic経過だけを保持する]
#   フィールド: [固定enumまたは相対時刻だけ。状態・message・stderrを含まない]
# }
@dataclass(slots=True)
class WorkerDiagnostic:
    started_ns: int = field(default_factory=time.monotonic_ns)
    phase: WorkerPhase = WorkerPhase.BOOTSTRAP
    phase_elapsed_ns: int = 0

    # {
    #   責務: [mark: trusted wrapperが指定した段階の開始時刻を更新する]
    #   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
    # }
    def mark(self, phase: WorkerPhase) -> None:
        self.phase = phase
        try:
            self.phase_elapsed_ns = max(0, time.monotonic_ns() - self.started_ns)
        except Exception:
            self.phase_elapsed_ns = 0

    # {
    #   責務: [record_exception: 最初のPython例外の固定metadataだけを1024byte以内で一時保存する]
    #   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
    # }
    def record_exception(self, directory: Path, error: BaseException) -> None:
        # 最初のPython例外だけを記録する。書込失敗も元の終了処理を変更しない。
        try:
            payload = {
                "schema_version": 1,
                "phase": self.phase.value,
                "exception_category": _category(error),
                "errno": _code(getattr(error, "errno", None)),
                "winerror": _code(getattr(error, "winerror", None)),
                "phase_elapsed_ns": self.phase_elapsed_ns,
                "exception_elapsed_ns": max(
                    self.phase_elapsed_ns, time.monotonic_ns() - self.started_ns
                ),
            }
            raw = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode(
                "ascii"
            )
            if len(raw) <= 1024:
                with (directory / "python_failure.json").open("xb") as stream:
                    stream.write(raw)
        except Exception:
            pass


# {
#   責務: [collect_worker_diagnostic: cleanup後のfile有無・exitcodeと検証済み固定例外metadataを読む]
#   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
# }
def collect_worker_diagnostic(
    directory: Path,
    exit_code: int | None,
    *,
    cleanup_requested: bool,
    exit_seen_before_cleanup: bool,
    highest_received_depth: int = 0,
) -> dict[str, Any]:
    from ai_game_player.planning_wire import read_planning_json

    payload = None
    status = "absent"
    try:
        value = read_planning_json(directory / "python_failure.json", 1024)
        if (
            set(value) != _FAILURE_FIELDS
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["phase"] not in {phase.value for phase in WorkerPhase}
            or value["exception_category"] not in _CATEGORIES
            or any(
                value[key] is not None and _code(value[key]) != value[key]
                for key in ("errno", "winerror")
            )
            or any(
                type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1
                for key in ("phase_elapsed_ns", "exception_elapsed_ns")
            )
            or value["exception_elapsed_ns"] < value["phase_elapsed_ns"]
        ):
            raise ValueError("invalid fixed worker diagnostic")
        payload, status = value, "recorded"
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError):
        status = "invalid"
    raw_code = _code(exit_code)
    unsigned = raw_code & 0xFFFFFFFF if raw_code is not None else None
    finished = _present(directory / "finished.json")
    return {
        "schema_version": 1,
        "python_exception_status": status,
        "python_exception": payload,
        "exitcode_raw": raw_code,
        "exitcode_signed32": unsigned - 2**32
        if unsigned is not None and unsigned >= 2**31
        else unsigned,
        "exitcode_hex": f"0x{unsigned:08x}" if unsigned is not None else None,
        "exit_observed_before_cleanup": exit_seen_before_cleanup,
        "parent_cleanup_requested": cleanup_requested,
        "finished_exists": finished,
        "checkpoint_exists": _present(directory / "checkpoint.json"),
        "highest_received_depth": highest_received_depth,
        "failure_classification": "python_exception"
        if status == "recorded"
        else "exit_unobserved"
        if raw_code is None
        else "parent_cleanup"
        if cleanup_requested
        else "abnormal_exit"
        if raw_code not in (0, None) or not finished
        else "normal_exit",
    }


# {
#   責務: [_present: 診断I/O失敗を状態不明Noneとして扱い本来の処理を妨げない]
#   処理: [固定型と容量を検査し、任意の例外文言を返さない。診断は権限や成果の証拠にしない]
# }
def _present(path: Path) -> bool | None:
    try:
        return path.is_file()
    except OSError:
        return None


_PARENT_MARKS = frozenset(
    (
        "directory_create_started_at_ns",
        "directory_created_at_ns",
        "spawn_started_at_ns",
        "spawn_returned_at_ns",
        "ready_read_started_at_ns",
        "ready_received_at_ns",
        "checkpoint_first_seen_at_ns",
        "checkpoint_raw_read_started_at_ns",
        "checkpoint_raw_read_finished_at_ns",
        "checkpoint_json_read_started_at_ns",
        "checkpoint_json_read_finished_at_ns",
        "first_checkpoint_received_at_ns",
        "first_valid_checkpoint_received_at_ns",
        "first_checkpoint_validated_at_ns",
        "finished_first_seen_at_ns",
        "finished_read_started_at_ns",
        "finished_read_finished_at_ns",
        "process_cleanup_started_at_ns",
        "process_cleanup_finished_at_ns",
        "directory_cleanup_started_at_ns",
        "directory_cleanup_finished_at_ns",
    )
)
_PARENT_IO_PHASES = frozenset(
    (
        "directory_create",
        "request_publish",
        "worker_start",
        "ready_read",
        "checkpoint_raw_read",
        "checkpoint_json_read",
        "checkpoint_validate",
        "finished_read",
        "process_cleanup",
        "directory_cleanup",
    )
)


# {
#   責務: [_parent_clock_ns: 親工程の診断用monotonic時刻を取得する]
#   処理: [採用deadlineとは独立したclock入口として呼出元で計測失敗を隔離する]
#   引数: [なし] 戻り値: [int: 親process内だけで比較するnanosecond時刻]
# }
def _parent_clock_ns() -> int:
    return time.monotonic_ns()


# {
#   責務: [new_parent_timing: 一requestの親clock内だけで比較する固定容量metadataを作る]
#   処理: [元deadlineを複製するだけで新しい権限やI/Oを作らず、OOM時は診断なしへ戻る]
# }
def new_parent_timing(started: int, cutoff: int) -> dict[str, Any] | None:
    try:
        if (
            type(started) is not int
            or type(cutoff) is not int
            or not 0 <= started <= cutoff <= 2**63 - 1
        ):
            return None
        return {
            "schema_version": 1,
            "clock_scope": "current_parent_process_only",
            "plan_started_at_ns": started,
            "compute_cutoff_at_ns": cutoff,
            "last_observed_at_ns": started,
            "clock_invalid": False,
            "io_errors": [],
        }
    except Exception:
        return None


# {
#   責務: [mark_parent_timing: 各固定phaseの最初の時刻だけをbest-effortで保持する]
#   処理: [時計不正/後退/OOMで元の結果を変えず、既存phaseへの反復はclockも呼ばない]
# }
def mark_parent_timing(
    timing: dict[str, Any] | None, phase: str, *, observed_ns: int | None = None
) -> None:
    try:
        if timing is None or phase not in _PARENT_MARKS or phase in timing:
            return
        now = _parent_clock_ns() if observed_ns is None else observed_ns
        if (
            type(now) is not int
            or not timing["last_observed_at_ns"] <= now <= 2**63 - 1
        ):
            timing["clock_invalid"] = True
            return
        timing[phase] = now
        timing["last_observed_at_ns"] = now
    except Exception:
        pass


# {
#   責務: [record_parent_io_error: 親の失敗を固定phase/category/code最大2件へ限定する]
#   処理: [自由な例外文/path/stateを保存せず、診断の失敗も元例外を変更しない]
# }
def record_parent_io_error(
    timing: dict[str, Any] | None, phase: str, error: BaseException
) -> None:
    try:
        if (
            timing is None
            or phase not in _PARENT_IO_PHASES
            or len(timing["io_errors"]) >= 2
        ):
            return
        timing["io_errors"].append(
            {
                "phase": phase,
                "exception_category": _category(error),
                "errno": _code(getattr(error, "errno", None)),
                "winerror": _code(getattr(error, "winerror", None)),
            }
        )
    except Exception:
        pass


# {
#   責務: [merge_parent_timing: cleanup後の一request診断を独立snapshotへ複製する]
#   処理: [資源不足や不正metadataでも元の診断/結果を返し、時刻を結果採用に使用しない]
# }
def merge_parent_timing(
    diagnostic: dict[str, Any] | None, timing: dict[str, Any] | None
) -> dict[str, Any] | None:
    try:
        if timing is None:
            return diagnostic
        fields = {
            "schema_version",
            "clock_scope",
            "plan_started_at_ns",
            "compute_cutoff_at_ns",
            "last_observed_at_ns",
            "clock_invalid",
            "io_errors",
        }
        if (
            type(timing) is not dict
            or set(timing) - fields - _PARENT_MARKS
            or not fields <= set(timing)
            or timing["schema_version"] != 1
            or timing["clock_scope"] != "current_parent_process_only"
            or type(timing["clock_invalid"]) is not bool
            or type(timing["io_errors"]) is not list
            or len(timing["io_errors"]) > 2
            or any(
                type(timing[key]) is not int or not 0 <= timing[key] <= 2**63 - 1
                for key in set(timing)
                - {"schema_version", "clock_scope", "clock_invalid", "io_errors"}
            )
            or not timing["plan_started_at_ns"] <= timing["compute_cutoff_at_ns"]
            or timing["last_observed_at_ns"] < timing["plan_started_at_ns"]
        ):
            return diagnostic
        for item in timing["io_errors"]:
            if (
                type(item) is not dict
                or set(item) != {"phase", "exception_category", "errno", "winerror"}
                or item["phase"] not in _PARENT_IO_PHASES
                or item["exception_category"] not in _CATEGORIES
                or any(
                    item[key] is not None and _code(item[key]) != item[key]
                    for key in ("errno", "winerror")
                )
            ):
                return diagnostic
        snapshot = dict(timing)
        snapshot["io_errors"] = [dict(item) for item in timing["io_errors"]]
        return {**(diagnostic or {}), "parent_timing": snapshot}
    except Exception:
        return diagnostic
