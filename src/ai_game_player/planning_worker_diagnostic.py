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
