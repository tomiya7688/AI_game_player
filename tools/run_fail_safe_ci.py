from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ai_game_player.fail_safe_runtime import AtomicJsonStore, InputLedger


def _owner() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _watchdog(root: Path, state_dir: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    src = str(root / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.Popen(
        [sys.executable, "-m", "ai_game_player", "--failsafe-watchdog", str(state_dir)],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_active(
    state_dir: Path,
    *,
    owner_pid: int,
    target_pid: int = 0,
    lease_seconds: float = 5.0,
    observation_seconds: float = 5.0,
    hold_ttl: float | None = None,
) -> InputLedger:
    state_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    AtomicJsonStore(state_dir / "lease.json").write(
        {
            "schema": "kadoka-control-lease/v1",
            "state": "active",
            "reason": "ci_active",
            "epoch": "ci-epoch",
            "session_id": "ci-session",
            "owner_pid": owner_pid,
            "target_pid": target_pid,
            "target_handle": 0,
            "lease_expires_at": now + lease_seconds,
            "observation_at": now,
            "observation_expires_at": now + observation_seconds,
            "last_sequence": 0,
            "inflight": 0,
            "watchdog_poll_seconds": 0.02,
            "updated_at": now,
        }
    )
    ledger = InputLedger(state_dir / "input_ledger.json")
    ledger.configure(target_handle=0, input_mode="mouse")
    if hold_ttl is not None:
        ledger.hold_key(0x20, hold_ttl)
    return ledger


def _wait_recovery(state_dir: Path, expected_reason: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    store = AtomicJsonStore(state_dir / "lease.json")
    while time.monotonic() < deadline:
        value = store.read()
        if value.get("state") == "recovery_required":
            reason = str(value.get("reason", ""))
            if reason != expected_reason:
                raise AssertionError(f"expected {expected_reason}, got {reason}")
            return
        time.sleep(0.02)
    raise AssertionError(f"watchdog did not enter recovery_required: {store.read()}")


def _wait_ledger_empty(ledger: InputLedger, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = ledger.snapshot()
        if not snapshot.get("held_keys") and not snapshot.get("held_mouse"):
            return
        time.sleep(0.02)
    raise AssertionError(f"input ledger was not released: {ledger.snapshot()}")


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _scenario_parent_loss(root: Path, base: Path, name: str) -> dict[str, object]:
    owner = _owner()
    watcher = None
    try:
        state_dir = base / name
        ledger = _write_active(state_dir, owner_pid=owner.pid, hold_ttl=30.0)
        watcher = _watchdog(root, state_dir)
        time.sleep(0.15)
        owner.kill()
        owner.wait(timeout=3)
        _wait_recovery(state_dir, "owner_process_lost")
        _wait_ledger_empty(ledger)
        watcher.wait(timeout=3)
        return {"reason": "owner_process_lost", "released": True}
    finally:
        _stop(owner)
        _stop(watcher)


def _scenario_lease_loss(root: Path, base: Path) -> dict[str, object]:
    owner = _owner()
    watcher = None
    try:
        state_dir = base / "network-loss"
        ledger = _write_active(
            state_dir,
            owner_pid=owner.pid,
            lease_seconds=0.25,
            observation_seconds=5.0,
            hold_ttl=30.0,
        )
        watcher = _watchdog(root, state_dir)
        _wait_recovery(state_dir, "lease_expired")
        _wait_ledger_empty(ledger)
        watcher.wait(timeout=3)
        return {"reason": "lease_expired", "released": True}
    finally:
        _stop(owner)
        _stop(watcher)


def _scenario_capture_loss(root: Path, base: Path) -> dict[str, object]:
    owner = _owner()
    watcher = None
    try:
        state_dir = base / "capture-loss"
        ledger = _write_active(
            state_dir,
            owner_pid=owner.pid,
            lease_seconds=5.0,
            observation_seconds=0.25,
            hold_ttl=30.0,
        )
        watcher = _watchdog(root, state_dir)
        _wait_recovery(state_dir, "observation_stale")
        _wait_ledger_empty(ledger)
        watcher.wait(timeout=3)
        return {"reason": "observation_stale", "released": True}
    finally:
        _stop(owner)
        _stop(watcher)


def _scenario_target_loss(root: Path, base: Path) -> dict[str, object]:
    owner = _owner()
    target = _owner()
    watcher = None
    try:
        state_dir = base / "target-loss"
        ledger = _write_active(
            state_dir,
            owner_pid=owner.pid,
            target_pid=target.pid,
            lease_seconds=5.0,
            observation_seconds=5.0,
            hold_ttl=30.0,
        )
        watcher = _watchdog(root, state_dir)
        time.sleep(0.15)
        target.kill()
        target.wait(timeout=3)
        _wait_recovery(state_dir, "target_process_lost")
        _wait_ledger_empty(ledger)
        watcher.wait(timeout=3)
        return {"reason": "target_process_lost", "released": True}
    finally:
        _stop(owner)
        _stop(target)
        _stop(watcher)


def _scenario_hold_ttl(root: Path, base: Path) -> dict[str, object]:
    owner = _owner()
    watcher = None
    try:
        state_dir = base / "hold-ttl"
        ledger = _write_active(
            state_dir,
            owner_pid=owner.pid,
            lease_seconds=5.0,
            observation_seconds=5.0,
            hold_ttl=0.15,
        )
        watcher = _watchdog(root, state_dir)
        _wait_ledger_empty(ledger)
        if watcher.poll() is not None:
            raise AssertionError("watchdog exited while active lease was still healthy")
        return {"released_by_ttl": True}
    finally:
        _stop(owner)
        _stop(watcher)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    base = (root / "build" / "fail-safe-ci").resolve()
    if base.exists():
        import shutil

        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)

    report = {
        "process_kill": _scenario_parent_loss(root, base, "process-kill"),
        "simulated_oom_parent_loss": _scenario_parent_loss(root, base, "simulated-oom"),
        "network_heartbeat_loss": _scenario_lease_loss(root, base),
        "capture_freshness_loss": _scenario_capture_loss(root, base),
        "target_process_loss": _scenario_target_loss(root, base),
        "input_hold_ttl": _scenario_hold_ttl(root, base),
    }
    report["all_failure_injections_passed"] = all(bool(value) for value in report.values())
    report_path = base / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
