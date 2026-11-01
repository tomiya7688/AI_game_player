"""登録済pure planner workerのOSメモリ制限。任意Pythonのsecurity sandboxではない。"""

import ctypes
import math
import os
import sys


# {
#   責務: [apply_worker_limits: model/factory import前にworkerのOS資源上限を設定する]
#   処理: [LinuxはRLIMIT_AS/CPU/CORE、WindowsはJob private-commit制限。失敗/未対応は探索しない]
#   引数: [memory_bytes: worker全体の上限, wall_time_ms: CPU backstop算出用]
#   戻り値: [tuple: 制限方式と、Windows Jobの寿命を保持するhandle]
#   エラー: [OSError/RuntimeError: 制限を適用できないplatform]
# }
def apply_worker_limits(
    memory_bytes: int, wall_time_ms: int
) -> tuple[str, object | None]:
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        cpu = max(1, math.ceil(wall_time_ms / 1000) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return "linux_rlimit_as", None
    if os.name == "nt":
        return "windows_job_commit", _windows_memory_job(memory_bytes)
    raise RuntimeError("worker resource limits are unsupported on this platform")


# {
#   責務: [_windows_memory_job: 現workerをprivate commit上限付きJobへ所属させる]
#   処理: [process/job memory、子process数1、handle終了時killを設定し、実適用に失敗したら閉じる]
#   引数: [memory_bytes: process/jobのcommit上限] 戻り値: [object: worker終了まで保持するJob handle]
#   エラー: [OSError: Job生成/設定/所属に失敗]
# }
def _windows_memory_job(memory_bytes: int) -> object:
    from ctypes import wintypes

    # {
    #   責務: [IoCounters: Windows Job APIのIO統計ABI fieldを宣言する]
    #   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
    # }
    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    # {
    #   責務: [BasicLimits: Windows Jobの基本制限ABI fieldを宣言する]
    #   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
    # }
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    # {
    #   責務: [ExtendedLimits: Windows Jobのprocess・job commit制限ABI fieldを宣言する]
    #   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
    # }
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    information = ExtendedLimits()
    information.BasicLimitInformation.LimitFlags = 0x100 | 0x200 | 0x8 | 0x2000
    information.BasicLimitInformation.ActiveProcessLimit = 1
    information.ProcessMemoryLimit = memory_bytes
    information.JobMemoryLimit = memory_bytes
    if not kernel.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    ) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = getattr(ctypes, "get_last_error")()
        kernel.CloseHandle(job)
        raise getattr(ctypes, "WinError")(error)
    return job
