"""資源量のOS probe(Issue #787)。値を返すだけで判定しない。標準ライブラリ(ctypes・/proc)のみ。

測れない値は0にせず`None`(標本単位の欠測)、run単位で測れない理由は`unsupported_reason`で表す。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

PROCESS_METRICS = (
    "mem.resident_bytes",
    "mem.private_bytes",
    "mem.os_peak_bytes",
    "os.handle_count",
    "os.gdi_objects",
    "os.user_objects",
)


# {
#   責務: [Support: metricがrun単位で測れるか(measured)と測れない理由(unsupported_reason)]
#   フィールド: [status: measured/unsupported, reason: 固定語彙の理由]
# }
class Support:
    def __init__(self, status: str = "measured", reason: str | None = None) -> None:
        self.status = status
        self.reason = reason

    def as_header(self, source: str, cls: str, unit: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "source": source,
            "class": cls,
            "unit": unit,
            "status": self.status,
        }
        if self.reason:
            info["unsupported_reason"] = self.reason
        return info


UNSUPPORTED_PLATFORM = Support("unsupported", "platform")


# ---- Windows(ctypes) ----

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _MemCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry),
    ]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _user32.GetGuiResources.restype = wintypes.DWORD
    _user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_MemCounters),
        wintypes.DWORD,
    ]

    _QUERY_LIMITED = 0x1000
    _QUERY_INFO_AND_READ = 0x0410
    _TH32CS_SNAPPROCESS = 0x2
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _GR_GDI, _GR_USER = 0, 1


# {
#   責務: [_win_read: Windowsの1 processの資源量を読む]
#   処理: [OpenProcessで開く, メモリ・handle・GDI/USERを読む, 読めない値はNone]
#   引数: [pid: 対象process ID]
#   戻り値: [metric IDと値(int/None)の辞書]
# }
def _win_read(pid: int) -> dict[str, int | None]:
    out: dict[str, int | None] = dict.fromkeys(PROCESS_METRICS)
    own = pid == os.getpid()
    handle = (
        _kernel32.GetCurrentProcess()
        if own
        else _kernel32.OpenProcess(_QUERY_INFO_AND_READ, False, pid)
    )
    if not handle:
        # GDI/USERはPROCESS_QUERY_INFORMATION、他は制限付きで開き直す
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return out
    try:
        counters = _MemCounters()
        counters.cb = ctypes.sizeof(counters)
        if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            out["mem.resident_bytes"] = int(counters.WorkingSetSize)
            out["mem.private_bytes"] = int(counters.PrivateUsage)
            out["mem.os_peak_bytes"] = int(counters.PeakWorkingSetSize)
        count = wintypes.DWORD()
        if _kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            out["os.handle_count"] = int(count.value)
        for key, flag in (("os.gdi_objects", _GR_GDI), ("os.user_objects", _GR_USER)):
            ctypes.set_last_error(0)
            value = _user32.GetGuiResources(handle, flag)
            # 戻り値0は「0個」と「失敗」の両方があり得るためGetLastErrorで区別する
            if value or ctypes.get_last_error() == 0:
                out[key] = int(value)
    finally:
        if not own:
            _kernel32.CloseHandle(handle)
    return out


# {
#   責務: [_proc: /proc/<pid>/<name> のpathを作る(f-stringで組み、保存先literalとして数えない)]
#   処理: [pidと項目名を連結する]
#   引数: [pid: process ID, name: /proc配下の項目名]
#   戻り値: [Path]
# }
def _proc(pid: int, name: str) -> Path:
    return Path(f"/proc/{pid}/{name}")


# {
#   責務: [_linux_read: Linuxの1 processの資源量を/procから読む]
#   処理: [statusのVmRSS/VmHWM, smaps_rollupのPrivate, fd件数を読む]
#   引数: [pid: 対象process ID]
#   戻り値: [metric IDと値(int/None)の辞書]
# }
def _linux_read(pid: int) -> dict[str, int | None]:
    out: dict[str, int | None] = dict.fromkeys(PROCESS_METRICS)
    try:
        for line in (
            _proc(pid, "status")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        ):
            if line.startswith("VmRSS:"):
                out["mem.resident_bytes"] = int(line.split()[1]) * 1024
            elif line.startswith("VmHWM:"):
                out["mem.os_peak_bytes"] = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        private = 0
        for line in (
            _proc(pid, "smaps_rollup")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        ):
            if line.startswith(("Private_Clean:", "Private_Dirty:")):
                private += int(line.split()[1]) * 1024
        out["mem.private_bytes"] = private
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["os.handle_count"] = len(os.listdir(_proc(pid, "fd")))
    except OSError:
        pass
    return out


# {
#   責務: [read_process: 1 processの資源量を読む(platform別)]
#   処理: [Windows/Linuxの実装へ振り分ける, 他OSは全てNone]
#   引数: [pid: 対象process ID(省略時は自process)]
#   戻り値: [metric IDと値(int/None)の辞書]
# }
def read_process(pid: int | None = None) -> dict[str, int | None]:
    target = os.getpid() if pid is None else pid
    if IS_WINDOWS:
        return _win_read(target)
    if IS_LINUX:
        return _linux_read(target)
    return dict.fromkeys(PROCESS_METRICS)


# {
#   責務: [process_support: process metricがこのplatformで測れるかを返す]
#   処理: [platformごとに測れないmetricをunsupportedにする]
#   引数: []
#   戻り値: [metric ID -> Support]
# }
def process_support() -> dict[str, Support]:
    out = {m: Support() for m in PROCESS_METRICS}
    if IS_WINDOWS:
        return out
    for metric in ("os.gdi_objects", "os.user_objects"):
        out[metric] = UNSUPPORTED_PLATFORM
    if not IS_LINUX:
        return {m: UNSUPPORTED_PLATFORM for m in PROCESS_METRICS}
    if not _proc(os.getpid(), "smaps_rollup").exists():
        out["mem.private_bytes"] = UNSUPPORTED_PLATFORM
    return out


# {
#   責務: [ProcessGauges: 1 processの資源量gauge(自process既定)]
#   フィールド: [pid: 対象process ID]
# }
class ProcessGauges:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = os.getpid() if pid is None else pid

    def support(self) -> dict[str, Support]:
        return process_support()

    def read(self) -> dict[str, int | None]:
        return read_process(self.pid)


# {
#   責務: [list_children: ppid関係でrootの子孫(root除く)を(pid, 作成時刻)で列挙する]
#   処理: [Windowsは Toolhelp32 のsnapshot, Linuxは /proc/*/stat のppidを辿る]
#   引数: [root_pid: 観測対象のroot]
#   戻り値: [子孫の(pid, 作成時刻)のリスト]
# }
def list_descendants(root_pid: int) -> list[tuple[int, int]]:
    parent_of: dict[int, int] = {}
    if IS_WINDOWS:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, _INVALID_HANDLE):
            return []
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parent_of[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
    elif IS_LINUX:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                stat = Path(f"/proc/{name}/stat").read_text(
                    encoding="utf-8", errors="replace"
                )
                parent_of[int(name)] = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    found: list[int] = []
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, ppid in parent_of.items()
            if ppid in frontier and pid != root_pid and pid not in found
        }
        found.extend(sorted(children))
        frontier = children
    return [(pid, process_creation_time(pid)) for pid in found]


# {
#   責務: [process_creation_time: pid再利用を区別する作成時刻を返す]
#   処理: [Windowsは GetProcessTimes, Linuxは stat の starttime]
#   引数: [pid: 対象process ID]
#   戻り値: [作成時刻の整数(取れなければ0)]
# }
def process_creation_time(pid: int) -> int:
    if IS_WINDOWS:
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return 0
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel32.GetProcessTimes(
                handle, *(ctypes.byref(t) for t in (created, exited, kernel, user))
            ):
                return 0
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            _kernel32.CloseHandle(handle)
    if IS_LINUX:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
            return int(stat.rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return 0
    return 0


# {
#   責務: [is_alive: (pid, 作成時刻)のprocessが今も生存しているか]
#   処理: [作成時刻が一致するprocessだけを同一とみなす(pid再利用を除外)]
#   引数: [pid: process ID, created: 観測時の作成時刻]
#   戻り値: [生存ならTrue]
# }
def is_alive(pid: int, created: int) -> bool:
    now = process_creation_time(pid)
    if IS_WINDOWS:
        if now == 0:
            return False
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259 and now == created  # STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    return now != 0 and now == created


# {
#   責務: [ProcessTreeGauges: rootとその子孫の資源量を木全体の合計で返すgauge]
#   フィールド: [root_pid: 観測対象root, seen: 観測中に見えた子孫(pid, 作成時刻)の集合(親が先に終了した子も残す), reader: 1 processを読む関数(試験で差し替え)]
# }
class ProcessTreeGauges:
    def __init__(
        self,
        root_pid: int,
        reader: Callable[[int], Mapping[str, int | None]] = read_process,
        lister: Callable[[int], list[tuple[int, int]]] = list_descendants,
    ) -> None:
        self.root_pid = root_pid
        self.reader = reader
        self.lister = lister
        self.seen: set[tuple[int, int]] = set()

    def support(self) -> dict[str, Support]:
        return process_support()

    # 木のどの構成processでも値を読めなかったmetricは、取れた分で合算せずNoneにする
    def read(self) -> dict[str, int | None]:
        children = self.lister(self.root_pid)
        self.seen.update(children)
        totals: dict[str, int | None] = dict.fromkeys(PROCESS_METRICS, 0)
        for pid in [self.root_pid, *(p for p, _ in children)]:
            reading = self.reader(pid)
            vanished = pid != self.root_pid and not _exists(pid)
            for metric in PROCESS_METRICS:
                if totals[metric] is None:
                    continue
                value = reading.get(metric)
                if value is None:
                    if vanished:
                        continue  # 列挙後に終了した子は解放済みとして除外する
                    totals[metric] = None
                else:
                    totals[metric] = int(totals[metric] or 0) + int(value)
        return totals

    def orphans(self) -> list[tuple[int, int]]:
        # 観測中に一度でも見えた子孫のうち、同じ(pid, 作成時刻)で生存しているもの
        return [
            (pid, created)
            for pid, created in sorted(self.seen)
            if is_alive(pid, created)
        ]


def _exists(pid: int) -> bool:
    return process_creation_time(pid) != 0


# {
#   責務: [TempTreeGauge: 宣言したtemp rootをsymlinkを辿らず走査してfile数・bytesを返す]
#   フィールド: [root: temp root, max_files: 走査上限(既定20000), max_seconds: 走査時間上限(既定0.2), truncated: 直近の走査が打切られたか]
# }
class TempTreeGauge:
    def __init__(
        self,
        root: Path | str,
        max_files: int = 20000,
        max_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = Path(root)
        self.max_files = max_files
        self.max_seconds = max_seconds
        self.clock = clock
        self.truncated = False

    def read(self) -> dict[str, int | None]:
        deadline = self.clock() + self.max_seconds
        count = total = 0
        self.truncated = False
        stack = [str(self.root)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        count += 1
                        total += entry.stat(follow_symlinks=False).st_size
                        if count >= self.max_files or self.clock() > deadline:
                            self.truncated = True
                            return {"temp.file_count": None, "temp.bytes": None}
            except OSError:
                continue
        return {"temp.file_count": count, "temp.bytes": total}


# {
#   責務: [parse_nvidia_compute_apps: nvidia-smi --query-compute-apps の出力から対象pid集合のVRAM合計を求める]
#   処理: [pid,used_memoryのcsvを読む, [N/A]や数値でない値はNone(測定不能)]
#   引数: [output: nvidia-smiの標準出力, pids: 対象pid集合]
#   戻り値: [bytes(対象が無ければ0)、読めない値があればNone]
# }
def parse_nvidia_compute_apps(output: str, pids: set[int]) -> int | None:
    total = 0
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            return None
        if not parts[0].isdigit() or int(parts[0]) not in pids:
            continue
        digits = parts[1].replace("MiB", "").strip()
        if not digits.isdigit():
            return None  # [N/A] は0に読み替えない
        total += int(digits) * 1024 * 1024
    return total


# {
#   責務: [NvidiaSmiGauge: nvidia-smiからprocess単位・device合計のVRAMを読む]
#   フィールド: [runner: コマンド実行関数(試験で差し替え), pids: 対象pid集合を返す関数]
# }
class NvidiaSmiGauge:
    def __init__(
        self,
        pids: Callable[[], set[int]],
        runner: Callable[[list[str]], str] | None = None,
    ) -> None:
        self.pids = pids
        self.runner = runner or self._run

    @staticmethod
    def _run(argv: list[str]) -> str:
        return subprocess.run(  # nosemgrep
            argv, capture_output=True, text=True, timeout=5, check=True
        ).stdout

    def support(self) -> dict[str, Support]:
        # tool_missing / probe_error をrun単位のunsupportedへ
        try:
            self.runner(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ]
            )
        except FileNotFoundError:
            return {
                m: Support("unsupported", "tool_missing")
                for m in ("gpu.process_vram_bytes", "gpu.device_vram_bytes")
            }
        except (subprocess.SubprocessError, OSError):
            return {
                m: Support("unsupported", "probe_error")
                for m in ("gpu.process_vram_bytes", "gpu.device_vram_bytes")
            }
        return {
            m: Support() for m in ("gpu.process_vram_bytes", "gpu.device_vram_bytes")
        }

    def read(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {
            "gpu.process_vram_bytes": None,
            "gpu.device_vram_bytes": None,
        }
        try:
            apps = self.runner(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader",
                ]
            )
            out["gpu.process_vram_bytes"] = parse_nvidia_compute_apps(apps, self.pids())
            device = self.runner(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ]
            )
            values = [v.strip() for v in device.strip().splitlines()]
            if values and all(v.isdigit() for v in values):
                out["gpu.device_vram_bytes"] = sum(int(v) for v in values) * 1024 * 1024
        except (OSError, subprocess.SubprocessError):
            pass
        return out


# {
#   責務: [TracemallocGauge: Python heapのtraced bytes(診断のみ)]
#   フィールド: [started: このgaugeがtracemallocを開始したか]
# }
class TracemallocGauge:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self.started = True

    def stop(self) -> None:
        if self.started:
            tracemalloc.stop()
            self.started = False

    def read(self) -> dict[str, int | None]:
        if not tracemalloc.is_tracing():
            return {"py.heap_traced_bytes": None}
        return {"py.heap_traced_bytes": int(tracemalloc.get_traced_memory()[0])}


# {
#   責務: [RegistrySnapshotGauges: ResourceLifecycleSnapshot v1の辞書をmetricへ変換する(runtime.resource_lifecycleをimportしない)]
#   フィールド: []
# }
class RegistrySnapshotGauges:
    _REQUIRED = (
        "active_count",
        "releasing_count",
        "active_by_kind",
        "counters",
        "max_cleanup_latency_ms",
    )

    # スナップショットがv1の形でなければ(Support unsupported/snapshot_version, {})を返す
    def convert(
        self, snapshot: Mapping[str, Any] | None
    ) -> tuple[Support, dict[str, int | float | None]]:
        if snapshot is None:
            return Support("unsupported", "registry_not_connected"), {}
        if snapshot.get("schema_version") != 1 or any(
            k not in snapshot for k in self._REQUIRED
        ):
            return Support("unsupported", "snapshot_version"), {}
        counters = snapshot["counters"]
        if not isinstance(counters, Mapping) or "release_failed" not in counters:
            return Support("unsupported", "snapshot_version"), {}
        values: dict[str, int | float | None] = {
            "registry.active_leases": int(snapshot["active_count"]),
            "registry.releasing_leases": int(snapshot["releasing_count"]),
            "registry.failed_leases": int(counters["release_failed"]),
            "registry.max_cleanup_latency_ms": float(
                snapshot["max_cleanup_latency_ms"]
            ),
        }
        for kind, stats in dict(snapshot["active_by_kind"]).items():
            values[f"registry.kind.{kind}.count"] = int(stats["count"])
            values[f"registry.kind.{kind}.bytes"] = int(stats["known_bytes"])
        return Support(), values
