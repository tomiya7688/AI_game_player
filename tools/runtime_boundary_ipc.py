"""固定4KiBのIPC往復を測定し、無応答時には専用workerを回収します。"""

from __future__ import annotations

import math
import multiprocessing

if __package__:
    from .runtime_boundary_metrics import measure_operation
else:
    from runtime_boundary_metrics import measure_operation

IPC_TIMEOUT_SECONDS = 5.0
IPC_SHUTDOWN_SECONDS = 2.0


def _ipc_worker(connection) -> None:
    """固定されたベンチマークデータを返し、終了通知または切断で終了します。"""
    try:
        while True:
            value = connection.recv_bytes()
            if value == b"__stop__":
                return
            connection.send_bytes(value)
    except EOFError:
        return
    finally:
        connection.close()


def measure_ipc_roundtrip(
    samples: int,
    timeout_seconds: float = IPC_TIMEOUT_SECONDS,
    worker_target=_ipc_worker,
) -> dict[str, object]:
    """4KiBの往復を測り、応答待ちの期限を超えたworkerを終了・回収します。"""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("IPC timeout must be finite and positive")
    # spawnを共通に使い、他テストのスレッドやTk状態をforkで引き継ぎません。
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=worker_target, args=(child,))
    payload = b"k" * 4096
    try:
        process.start()
        child.close()

        def roundtrip() -> None:
            parent.send_bytes(payload)
            if not parent.poll(timeout_seconds):
                raise TimeoutError("IPC benchmark response timed out")
            received = parent.recv_bytes()
            if received != payload:
                raise RuntimeError("IPC benchmark payload mismatch")

        result = measure_operation("ipc_pipe_4k_roundtrip", roundtrip, samples)
        parent.send_bytes(b"__stop__")
        process.join(timeout=IPC_SHUTDOWN_SECONDS)
        if process.is_alive():
            raise TimeoutError("IPC benchmark worker did not exit")
        if process.exitcode != 0:
            raise RuntimeError(f"IPC benchmark worker exited with {process.exitcode}")
        return result
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=IPC_SHUTDOWN_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(timeout=IPC_SHUTDOWN_SECONDS)
            if process.is_alive():
                raise RuntimeError("IPC benchmark worker could not be stopped")
            process.close()
