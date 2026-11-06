"""Fast gate用のfake資源世界とscenario(Issue #787)。

pass期待scenarioと、判定が静かに緩むことをCIで検出するcanary(fail期待)scenarioを持つ。
乱数は`random.Random(seed)`のみ。時計は疑似時計(1 step = 1ms)で環境に依存しない。
"""

from __future__ import annotations

import os
import random
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ai_game_player.runtime import resource_probes
from ai_game_player.runtime.resource_stability import (
    MIB,
    SAMPLE_SCHEMA,
    metric_traits,
)

NS_PER_STEP = 1_000_000
BASE_HANDLES = 120
BASE_MEMORY = 80 * MIB
CACHE_BYTES = 32 * MIB
SYNTHETIC_METRICS = (
    "registry.active_leases",
    "registry.releasing_leases",
    "registry.failed_leases",
    "registry.kind.model_cache.count",
    "registry.kind.model_cache.bytes",
    "registry.max_cleanup_latency_ms",
    "os.handle_count",
    "os.gdi_objects",
    "os.user_objects",
    "temp.file_count",
    "temp.bytes",
    "queue.length.main",
    "mem.resident_bytes",
)
UNSUPPORTED_METRICS = (
    ("native.alloc_count", "no_counter"),
    ("native.copy_count", "no_counter"),
    ("task.pending_count", "no_counter"),
)
REAL_METRICS = ("os.handle_count", "temp.file_count", "temp.bytes")


# {
#   責務: [Behavior: 1つのscenarioの欠陥・保持・停止方法の宣言]
#   フィールド: [leak_lease_per_step: stepごとに解放しないlease数, leak_handle_every/leak_gdi_every/leak_temp_every: N stepごとに解放しない資源,
#     cache_fill_steps: bounded cacheを充填するstep数, pinned_cache: model cacheをSession間で保持, declare_pinned: 保持を宣言するか, pinned_extra: 宣言を超えて保持する数,
#     stop_mode: 最後のstepをstop/errorで中断, stop_leaks: 中断時に確保済み資源を解放しないか, staircase_handles: close時に残すhandle数,
#     real_probe: handle・tempを実probeで測る, expected_codes: canaryが出すべきreasons.code]
# }
@dataclass(frozen=True)
class Behavior:
    leak_lease_per_step: int = 0
    leak_handle_every: int = 0
    leak_gdi_every: int = 0
    leak_temp_every: int = 0
    cache_fill_steps: int = 0
    pinned_cache: bool = False
    declare_pinned: bool = True
    pinned_extra: int = 0
    stop_mode: str = ""
    stop_leaks: bool = False
    staircase_handles: int = 0
    real_probe: bool = False
    steps: int | None = None
    sample_every: int | None = None
    expected_codes: tuple[str, ...] = ()


SCENARIOS: dict[str, Behavior] = {
    "clean": Behavior(),
    "bounded_cache_plateau": Behavior(cache_fill_steps=400),
    "pinned_model_cache": Behavior(pinned_cache=True),
    "stop_mid_step_clean": Behavior(stop_mode="stop"),
    "error_mid_step_clean": Behavior(stop_mode="error"),
    "real_probe_smoke": Behavior(real_probe=True),
}
CANARIES: dict[str, Behavior] = {
    "leak_lease_per_step": Behavior(
        leak_lease_per_step=1, expected_codes=("tail_growth",)
    ),
    "leak_handle": Behavior(leak_handle_every=50, expected_codes=("tail_growth",)),
    "leak_gdi": Behavior(leak_gdi_every=100, expected_codes=("tail_growth",)),
    "leak_temp_file": Behavior(leak_temp_every=400, expected_codes=("tail_growth",)),
    "stop_leaves_resource": Behavior(
        stop_mode="stop", stop_leaks=True, expected_codes=("not_reclaimed",)
    ),
    "error_leaves_resource": Behavior(
        stop_mode="error", stop_leaks=True, expected_codes=("not_reclaimed",)
    ),
    "baseline_staircase": Behavior(
        staircase_handles=4, expected_codes=("baseline_staircase",)
    ),
}


class _Stop(Exception):
    pass


# {
#   責務: [FakeResourceWorld: lease・handle・GDI・USER・temp・bytes・queueのfake counter世界]
#   フィールド: [counters: 資源名ごとの現在値, rng: seed固定の乱数]
# }
class FakeResourceWorld:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.counters: dict[str, int] = {m: 0 for m in SYNTHETIC_METRICS}
        self.counters["os.handle_count"] = BASE_HANDLES
        self.counters["registry.max_cleanup_latency_ms"] = 12
        self.counters["mem.resident_bytes"] = BASE_MEMORY

    def add(self, metric: str, delta: int) -> None:
        self.counters[metric] += delta

    def values(self) -> dict[str, int | None]:
        out: dict[str, int | None] = dict(self.counters)
        # 診断用のbytesは疑似乱数の揺らぎを持つ(判定対象ではない)
        out["mem.resident_bytes"] = BASE_MEMORY + self.rng.randrange(0, MIB)
        out["queue.length.main"] = self.rng.randrange(0, 2)
        return out


def _header(
    name: str,
    layer_profile: str,
    behavior: Behavior,
    seed: int,
    run_id: str,
    host_profile: str,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for metric in SYNTHETIC_METRICS:
        unit, cls = metric_traits(metric) or ("count", "count")
        # 実probeで測るmetricだけsourceをprocessにする(syntheticを実測として扱わない)
        real = (behavior.real_probe and metric in REAL_METRICS) or (
            bool(behavior.leak_temp_every) and metric in REAL_METRICS[1:]
        )
        source = "process" if real else "synthetic"
        info = {"source": source, "class": cls, "unit": unit, "status": "measured"}
        if (
            source == "process"
            and metric == "os.handle_count"
            and resource_probes.process_support()["os.handle_count"].status
            != "measured"
        ):
            info.update(status="unsupported", unsupported_reason="platform")
        metrics[metric] = info
    for metric, reason in UNSUPPORTED_METRICS:
        unit, cls = metric_traits(metric) or ("count", "count")
        metrics[metric] = {
            "source": "synthetic",
            "class": cls,
            "unit": unit,
            "status": "unsupported",
            "unsupported_reason": reason,
        }
    retained: dict[str, float] = {}
    ceiling: dict[str, float] = {}
    if behavior.pinned_cache and behavior.declare_pinned:
        retained = {
            "registry.active_leases": 1,
            "registry.kind.model_cache.count": 1,
            "registry.kind.model_cache.bytes": CACHE_BYTES,
        }
    if behavior.cache_fill_steps:
        ceiling = {"registry.active_leases": behavior.cache_fill_steps}
    return {
        "schema": SAMPLE_SCHEMA,
        "kind": "header",
        "run_id": run_id,
        "layer": "fast",
        "run_profile": layer_profile,
        "host_profile": host_profile,
        "scenario": name,
        "seed": seed,
        "metrics": metrics,
        "declarations": {
            "declared_retained": retained,
            "declared_ceiling": ceiling,
            "declared_fill_steps": behavior.cache_fill_steps,
            "exclusive_gpu": False,
        },
    }


# {
#   責務: [build_rows: 1 scenarioを決定論的に実行しsamples.jsonl相当の行(header・sample・footer)を作る]
#   処理: [Sessionごとにpre_open→steps→closing→close→post_close(settle後)を行い, 疑似時計でsampleを記録し, footerにcleanup latencyを残す]
#   引数: [name: scenario名, behavior: 欠陥宣言, cfg: fast層のrun profile, run_profile_name: fast/deep, seed: 乱数seed, host_profile: windows/linux, workdir: 実file用の作業dir, extra: 追加gauge(省略可)]
#   戻り値: [行(辞書)の列]
#   副作用: [real_probe/leak_temp_file scenarioはworkdir配下に一時fileを作り終了時に削除する]
# }
def build_rows(
    name: str,
    behavior: Behavior,
    cfg: dict[str, Any],
    run_profile_name: str,
    seed: int,
    host_profile: str,
    workdir: Path,
    extra: Callable[[], dict[str, int | None]] | None = None,
) -> list[dict[str, Any]]:
    run_id = f"{name}-{seed}"
    warmup = int(cfg["warmup_steps"])
    if behavior.real_probe:
        # 実probeは実fileを触るため短く保つ。warmup(deepでは長い)の後にsteady標本が残る長さにする
        steps = warmup * 2
        every = max(1, steps // 200)
    else:
        steps = behavior.steps or int(cfg["steps_per_session"])
        every = behavior.sample_every or int(cfg["sample_every_steps"])
    header = _header(name, run_profile_name, behavior, seed, run_id, host_profile)
    if extra is not None:
        # --python-heap: 診断専用のPython heap(判定には使わない)
        header["metrics"]["py.heap_traced_bytes"] = {
            "source": "process",
            "class": "bytes",
            "unit": "bytes",
            "status": "measured",
        }
    rows: list[dict[str, Any]] = [header]
    world = FakeResourceWorld(seed)
    needs_dir = behavior.real_probe or behavior.leak_temp_every
    tmp_dir = workdir / f"scenario-{name}"
    if needs_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
    gauge = resource_probes.TempTreeGauge(tmp_dir) if needs_dir else None
    clock = 0
    footer_sessions: list[dict[str, Any]] = []
    held_cache = False

    def sample(session: int, phase: str, step: int) -> None:
        values = world.values()
        if behavior.real_probe:
            values["os.handle_count"] = resource_probes.read_process()[
                "os.handle_count"
            ]
        if gauge is not None:
            values.update(gauge.read())
        if extra is not None:
            values.update(extra())
        rows.append(
            {
                "schema": SAMPLE_SCHEMA,
                "kind": "sample",
                "run_id": run_id,
                "session_index": session,
                "phase": phase,
                "step": step,
                "t_monotonic_ns": clock,
                "values": values,
            }
        )

    try:
        for session in range(int(cfg["sessions"])):
            sample(session, "pre_open", 0)
            if behavior.pinned_cache and not held_cache:
                _load_pinned(world, 1 + behavior.pinned_extra)
                held_cache = True
            cache_held = 0
            temp_serial = 0
            for step in range(steps):
                clock += NS_PER_STEP
                _run_step(world, behavior, step, tmp_dir)
                if behavior.cache_fill_steps and cache_held < behavior.cache_fill_steps:
                    world.add("registry.active_leases", 1)
                    cache_held += 1
                if behavior.leak_temp_every and step % behavior.leak_temp_every == 0:
                    (tmp_dir / f"leak-{session}-{temp_serial}.tmp").write_bytes(b"x")
                    temp_serial += 1
                if step % every == every - 1:
                    sample(session, "warmup" if step < warmup else "steady", step)
            _stop_step(world, behavior)
            clock += NS_PER_STEP
            sample(session, "closing", steps)
            # Session close: bounded cacheを解放し、欠陥宣言の残骸だけを残す
            if cache_held:
                world.add("registry.active_leases", -cache_held)
            if behavior.staircase_handles:
                world.add("os.handle_count", behavior.staircase_handles)
            clock += NS_PER_STEP
            sample(session, "post_close", steps + 1)
            footer_sessions.append(
                {"index": session, "cleanup_latency_ms": 0.0, "settle_timeout": False}
            )
    finally:
        if needs_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    rows.append(
        {
            "schema": SAMPLE_SCHEMA,
            "kind": "footer",
            "run_id": run_id,
            "duration_seconds": clock / 1e9,
            "orphan_count": 0,
            "total_steps": steps * int(cfg["sessions"]),
            "sessions": footer_sessions,
        }
    )
    return rows


def _load_pinned(world: FakeResourceWorld, count: int) -> None:
    # process寿命で保持するmodel cache(宣言は1件)
    world.add("registry.active_leases", count)
    world.add("registry.kind.model_cache.count", count)
    world.add("registry.kind.model_cache.bytes", CACHE_BYTES * count)


def _run_step(
    world: FakeResourceWorld, behavior: Behavior, step: int, tmp_dir: Path
) -> None:
    # 1 stepで確保した資源をstep内で解放する。欠陥宣言があるものだけ解放しない
    if behavior.leak_lease_per_step:
        world.add("registry.active_leases", behavior.leak_lease_per_step)
    if behavior.leak_handle_every and step % behavior.leak_handle_every == 0:
        world.add("os.handle_count", 1)
    if behavior.leak_gdi_every and step % behavior.leak_gdi_every == 0:
        world.add("os.gdi_objects", 1)
    if behavior.real_probe:
        # 実handleと実tempを毎step確保し解放する
        with open(os.devnull, "rb"):
            if step % 20 == 0:
                path = tmp_dir / "probe.tmp"
                path.write_bytes(b"x" * 16)
                path.unlink()


def _stop_step(world: FakeResourceWorld, behavior: Behavior) -> None:
    # Stop/errorでstep途中に中断される最後のstep。確保済み資源を解放するのがfinally、残すのが欠陥
    if not behavior.stop_mode:
        return
    leaked = {"registry.active_leases": 3, "os.handle_count": 20, "temp.file_count": 1}
    for metric, amount in leaked.items():
        world.add(metric, amount)
    try:
        raise (
            _Stop() if behavior.stop_mode == "stop" else RuntimeError("scenario error")
        )
    except (_Stop, RuntimeError):
        pass
    finally:
        if not behavior.stop_leaks:
            for metric, amount in leaked.items():
                world.add(metric, -amount)


# {
#   責務: [scenario_names: 登録済みscenario名を種別付きで返す]
#   処理: [pass期待とcanaryを列挙する]
#   引数: []
#   戻り値: [(名前, 種別pass/canary)の列]
# }
def scenario_names() -> list[tuple[str, str]]:
    return [(n, "pass") for n in SCENARIOS] + [(n, "canary") for n in CANARIES]


# {
#   責務: [get_behavior: 名前からBehaviorを返す(未知名はKeyError)]
#   処理: [passとcanaryの両方から探す]
#   引数: [name: scenario名]
#   戻り値: [Behavior]
# }
def get_behavior(name: str) -> Behavior:
    if name in SCENARIOS:
        return SCENARIOS[name]
    return CANARIES[name]


def with_behavior(name: str, **changes: Any) -> Behavior:
    # 試験用: 既存scenarioの欠陥宣言を一部変える(例: pinnedの宣言なし・超過)
    return replace(get_behavior(name), **changes)


# {
#   責務: [host_profile_id: 実行中platformのhost profile ID(windows/linux/other)]
#   処理: [sys.platformを固定語彙へ写す]
#   引数: []
#   戻り値: [host profile ID]
# }
def host_profile_id() -> str:
    if sys.platform == "win32":
        return "windows"
    return "linux" if sys.platform.startswith("linux") else "other"
