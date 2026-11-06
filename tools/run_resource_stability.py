"""長時間Sessionの資源安定性Gate CLI(Issue #787)。

run / evaluate / list-scenarios。enforce/observeはCLI引数でなくprofileの
`enforcement.<layer>`で決まる。緩和用の引数・環境変数は持たない。
設計書: doc/design/issue-787-long-session-resource-stability-gate.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai_game_player.runtime import resource_probes  # noqa: E402
from ai_game_player.runtime.resource_stability import (  # noqa: E402
    SAMPLE_SCHEMA,
    ProfileError,
    evaluate_rows,
    metric_traits,
    validate_profile,
)
from tools import resource_stability_scenarios as scenarios  # noqa: E402

DEFAULT_PROFILE = REPO_ROOT / "config" / "resource_stability_profile.json"
DEFAULT_OUTPUT = REPO_ROOT / "build" / "resource-stability" / "report.json"
DEFAULT_SAMPLES = REPO_ROOT / "build" / "resource-stability" / "samples.jsonl"
EXIT_OK, EXIT_FAIL, EXIT_INVALID = 0, 1, 2
TEMP_SCAN_EVERY = 5


# {
#   責務: [load_profile: profile JSONを読み検証する(hard limit超過・語彙外はProfileError)]
#   処理: [JSONを読む, validate_profileを通す]
#   引数: [path: profileのpath]
#   戻り値: [検証済みprofile]
#   エラー: [ProfileError: 読めない・不正]
# }
def load_profile(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"profile unreadable: {type(exc).__name__}") from exc
    return validate_profile(raw)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit() -> dict[str, Any]:
    # GITHUB_SHAがあれば使い、無ければgit rev-parse。取れなければreason付きでunknown
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return {"commit": sha}
    try:
        out = subprocess.run(  # nosemgrep
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()
        return {"commit": out}
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "reason": "git_unavailable"}


# {
#   責務: [environment_info: reportに入れる実行時情報(版・commit・host)を作る(環境変数値・絶対path・user名は含めない)]
#   処理: [Python版とplatformの要約, commit, CPU数を集める]
#   引数: []
#   戻り値: [runtime/code/hostの辞書]
# }
def environment_info() -> dict[str, Any]:
    return {
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
        },
        "code": _commit(),
        "host": {"cpu_count": os.cpu_count() or 0, "gpu": None},
    }


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_rows(path: Path) -> list[Any]:
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError) as exc:
        raise ProfileError(f"samples unreadable: {type(exc).__name__}") from exc


# {
#   責務: [run_fast: fast層のscenarioを実行・判定し, pass期待とcanary(fail期待)を確認する]
#   処理: [scenarioごとにrowsを作る, evaluate_rowsで判定, pass期待はpass・canaryは期待codeでfailを確認, 結果をsuiteに集約]
#   引数: [profile: 検証済みprofile, run_profile: fast/deep, names: 実行するscenario(空なら全て), seed: 乱数seed, workdir: 作業dir, judge: 判定関数(canary試験で差し替え), python_heap: tracemallocを有効にするか]
#   戻り値: [(suite辞書, scenario名->(rows, report))]
# }
def run_fast(
    profile: dict[str, Any],
    run_profile: str,
    names: Sequence[str],
    seed: int,
    workdir: Path,
    judge: Callable[..., dict[str, Any]] | None = None,
    python_heap: bool = False,
) -> tuple[dict[str, Any], dict[str, tuple[list[dict[str, Any]], dict[str, Any]]]]:
    cfg = profile["layers"]["fast"]["run_profiles"][run_profile]
    selected = list(names) or [n for n, _ in scenarios.scenario_names()]
    environment = environment_info()
    host = scenarios.host_profile_id()
    results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    suite_items: list[dict[str, Any]] = []
    gauge = resource_probes.TracemallocGauge() if python_heap else None
    for name in selected:
        behavior = scenarios.get_behavior(name)
        if gauge is not None:
            gauge.start()
        started = time.perf_counter()
        try:
            rows = scenarios.build_rows(
                name,
                behavior,
                cfg,
                run_profile,
                seed,
                host,
                workdir,
                gauge.read if gauge else None,
            )
        finally:
            if gauge is not None:
                gauge.stop()
        elapsed = time.perf_counter() - started
        report = evaluate_rows(profile, rows, environment, _now_utc(), judge=judge)
        codes = sorted({reason["code"] for reason in report["reasons"]})
        if behavior.expected_codes:
            detected = report["verdict"] == "fail" and all(
                c in codes for c in behavior.expected_codes
            )
            item = {
                "scenario": name,
                "kind": "canary",
                "expected": list(behavior.expected_codes),
                "verdict": report["verdict"],
                "codes": codes,
                "ok": detected,
            }
            if not detected:
                item["code"] = "canary_not_detected"
        else:
            item = {
                "scenario": name,
                "kind": "pass",
                "verdict": report["verdict"],
                "codes": codes,
                "ok": report["verdict"] == "pass",
            }
        item["elapsed_seconds"] = round(elapsed, 3)
        suite_items.append(item)
        results[name] = (rows, report)
    suite = {
        "schema": "resource-stability-suite/v1",
        "scenarios": suite_items,
        "ok": all(i["ok"] for i in suite_items),
    }
    return suite, results


def _cmd_run_fast(args: argparse.Namespace, profile: dict[str, Any]) -> int:
    workdir = DEFAULT_OUTPUT.parent / "work"
    suite, results = run_fast(
        profile,
        args.run_profile,
        args.scenario or [],
        args.seed,
        workdir,
        python_heap=args.python_heap,
    )
    out = Path(args.output)
    samples = Path(args.samples)
    first = next(iter(results))
    _write_json(out, results[first][1])
    _write_rows(samples, results[first][0])
    for name, (rows, report) in results.items():
        _write_json(out.parent / "scenarios" / f"{name}.report.json", report)
        _write_rows(out.parent / "scenarios" / f"{name}.samples.jsonl", rows)
    _write_json(out.parent / "suite.json", suite)
    for item in suite["scenarios"]:
        print(
            f"{'OK  ' if item['ok'] else 'FAIL'} {item['kind']:<6} {item['scenario']} verdict={item['verdict']} codes={','.join(item['codes'])}"
        )
    # fast層は常にenforce。canaryの未検出とpass期待の失敗はどちらも終了コード1
    return EXIT_OK if suite["ok"] else EXIT_FAIL


def _tree_header(run_profile: str, host: str, seed: int, run_id: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for metric, support in resource_probes.process_support().items():
        unit, cls = metric_traits(metric) or ("count", "count")
        metrics[metric] = support.as_header("external", cls, unit)
    for metric in ("temp.file_count", "temp.bytes"):
        unit, cls = metric_traits(metric) or ("count", "count")
        metrics[metric] = resource_probes.Support().as_header("external", cls, unit)
    return {
        "schema": SAMPLE_SCHEMA,
        "kind": "header",
        "run_id": run_id,
        "layer": "integration",
        "run_profile": run_profile,
        "host_profile": host,
        "scenario": "observed_command",
        "seed": seed,
        "metrics": metrics,
        "declarations": {
            "declared_retained": {},
            "declared_ceiling": {},
            "declared_fill_steps": 0,
            "exclusive_gpu": False,
        },
    }


# {
#   責務: [observe_command: 観測対象commandのprocess treeをinterval_seconds間隔で標本化し, samples行を返す]
#   処理: [run専用のTEMP/TMPでcommandを起動, pre_open(temp)→標本化→closing→孤児確認→post_close(temp)の順に記録, timeout時は自分が起動した木だけ終了]
#   引数: [cfg: integration層のrun profile, command: 観測対象, workdir: 作業dir, timeout: 秒(省略可), run_profile/seed: header用]
#   戻り値: [(rows, command終了コード)]
#   副作用: [workdir配下にtemp rootを作る, commandを子processとして起動する]
# }
def observe_command(
    cfg: dict[str, Any],
    run_profile: str,
    command: Sequence[str],
    workdir: Path,
    seed: int,
    timeout: float | None = None,
    lower_priority: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    run_id = f"observed-{seed}"
    # 実行ごとに固有のtemp root(並行実行で互いのfileを消さない・測らない)
    temp_root = Path(tempfile.mkdtemp(prefix="rs-"))
    try:
        return _observe(
            cfg, run_profile, command, temp_root, run_id, seed, timeout, lower_priority
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _observe(
    cfg: dict[str, Any],
    run_profile: str,
    command: Sequence[str],
    temp_root: Path,
    run_id: str,
    seed: int,
    timeout: float | None,
    lower_priority: bool,
) -> tuple[list[dict[str, Any]], int]:
    temp_gauge = resource_probes.TempTreeGauge(temp_root)
    header = _tree_header(run_profile, scenarios.host_profile_id(), seed, run_id)
    rows: list[dict[str, Any]] = [header]
    interval = float(cfg["interval_seconds"])

    def emit(phase: str, step: int, values: dict[str, int | None]) -> None:
        rows.append(
            {
                "schema": SAMPLE_SCHEMA,
                "kind": "sample",
                "run_id": run_id,
                "session_index": 0,
                "phase": phase,
                "step": step,
                "t_monotonic_ns": time.monotonic_ns(),
                "values": values,
            }
        )

    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(temp_root)
    nothing: dict[str, int | None] = dict.fromkeys(resource_probes.PROCESS_METRICS)
    emit("pre_open", 0, {**nothing, **temp_gauge.read()})
    started = time.monotonic()
    proc = subprocess.Popen(list(command), env=env)  # nosemgrep
    tree = resource_probes.ProcessTreeGauges(proc.pid)
    if lower_priority:
        # 観測側は観測対象より低い優先度で動かす(優先度は子へ継承されるため起動後に下げる)
        resource_probes.lower_current_priority()
    last_temp = temp_gauge.read()
    step = 0
    warmup = float(cfg["warmup_seconds"])
    max_samples = int(cfg.get("max_samples", 20000))
    limit_reached = False
    timed_out = False
    while proc.poll() is None:
        elapsed = time.monotonic() - started
        if timeout is not None and elapsed > timeout:
            timed_out = True
            _terminate_tree(proc, tree)
            break
        if len(rows) >= max_samples:
            # 後から黙って間引かない: 上限に達したら標本化を止めて報告に残す
            limit_reached = True
            time.sleep(interval)
            continue
        step += 1
        # temp rootの走査はIOを伴うため TEMP_SCAN_EVERY 標本に1回だけ行い、間は直近の走査値を使う
        if step % TEMP_SCAN_EVERY == 1:
            last_temp = temp_gauge.read()
        emit(
            "warmup" if elapsed < warmup else "steady",
            step,
            {**tree.read(), **last_temp},
        )
        time.sleep(interval)
    exit_code = proc.wait()
    closed = time.monotonic()
    emit("closing", step + 1, {**nothing, **temp_gauge.read()})
    orphans = tree.orphans()
    # settle: 終了直後の後始末が済むまで(settle_ms以内)temp rootが安定するのを待つ
    settled, settle_timeout = _settle(temp_gauge, float(cfg["settle_ms"]) / 1000)
    emit("post_close", step + 2, {**nothing, **settled})
    footer = {
        "schema": SAMPLE_SCHEMA,
        "kind": "footer",
        "run_id": run_id,
        "duration_seconds": round(closed - started, 3),
        "orphan_count": len(orphans),
        "command_exit_code": -1 if timed_out else exit_code,
        "total_steps": step,
        "sampling_limit_reached": limit_reached,
        "sessions": [
            {
                "index": 0,
                "cleanup_latency_ms": round((time.monotonic() - closed) * 1000, 1),
                "settle_timeout": settle_timeout,
            }
        ],
    }
    rows.append(footer)
    return rows, footer["command_exit_code"]  # type: ignore[return-value]


# {
#   責務: [_settle: temp rootの値が2連続で同じになるまで, 最大settle秒待って最後の値を返す]
#   処理: [100msごとに走査, 連続一致で安定, 時間切れなら最後の値とsettle_timeout=Trueを返す(失敗の免除にしない)]
#   引数: [gauge: temp走査, seconds: 待つ上限]
#   戻り値: [(最後の走査値, settle_timeoutか)]
# }
def _settle(
    gauge: resource_probes.TempTreeGauge, seconds: float
) -> tuple[dict[str, int | None], bool]:
    deadline = time.monotonic() + seconds
    previous = gauge.read()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = gauge.read()
        if current == previous:
            return current, False
        previous = current
    return previous, True


def _terminate_tree(
    proc: subprocess.Popen[bytes], tree: resource_probes.ProcessTreeGauges
) -> None:
    # 自分が起動した木だけを終了する(他processは触らない)
    for pid, created in list(tree.seen):
        if resource_probes.is_alive(pid, created):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    proc.kill()


def _cmd_run_integration(
    args: argparse.Namespace, profile: dict[str, Any], command: list[str]
) -> int:
    if not command:
        print("integration layer needs a command after --", file=sys.stderr)
        return EXIT_INVALID
    cfg = profile["layers"]["integration"]["run_profiles"][args.run_profile]
    workdir = DEFAULT_OUTPUT.parent / "work"
    rows, command_exit = observe_command(
        cfg,
        args.run_profile,
        command,
        workdir,
        args.seed,
        args.timeout_seconds,
        lower_priority=True,
    )
    _write_rows(Path(args.samples), rows)
    report = evaluate_rows(profile, rows, environment_info(), _now_utc())
    _write_json(Path(args.output), report)
    return _exit_for(report, command_exit)


def _exit_for(report: dict[str, Any], command_exit: int | None) -> int:
    # 優先順位: 観測対象の失敗(1) > enforceのfail(1) > observeのfail(0・warning)
    if command_exit:
        return EXIT_FAIL
    if report["verdict"] == "fail":
        if report["run"]["gate_effective"]:
            return EXIT_FAIL
        codes = ",".join(sorted({r["code"] for r in report["reasons"]}))
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::resource stability gate (observe) would fail: {codes}")
        print(f"resource stability verdict=fail (observe, not enforced): {codes}")
    return EXIT_OK


def _cmd_evaluate(args: argparse.Namespace, profile: dict[str, Any]) -> int:
    rows = _read_rows(Path(args.samples))
    report = evaluate_rows(profile, rows, environment_info(), _now_utc())
    _write_json(Path(args.output), report)
    footer_exit = (
        rows[-1].get("command_exit_code")
        if rows and isinstance(rows[-1], dict)
        else None
    )
    return _exit_for(report, footer_exit)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="scenarioまたは観測対象commandを実行して判定する")
    run.add_argument("--layer", choices=["fast", "integration"], required=True)
    run.add_argument("--run-profile", choices=["fast", "pr", "deep"], required=True)
    run.add_argument(
        "--scenario", action="append", help="fast層で実行するscenario(複数可)"
    )
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--output", default=str(DEFAULT_OUTPUT))
    run.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    run.add_argument(
        "--python-heap", action="store_true", help="診断用にtracemallocを有効にする"
    )
    run.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="integration層で自分が起動した木を打切る秒数",
    )
    run.add_argument(
        "--profile-file",
        default=str(DEFAULT_PROFILE),
        help="profile JSON(hard limitは常にコードで検査)",
    )
    run.add_argument(
        "command", nargs="*", help="-- の後ろ: integration層の観測対象command"
    )
    ev = sub.add_parser("evaluate", help="保存済みsamples.jsonlを判定する(#250の入力)")
    ev.add_argument("--samples", required=True)
    ev.add_argument("--output", required=True)
    ev.add_argument("--profile-file", default=str(DEFAULT_PROFILE))
    sub.add_parser("list-scenarios", help="登録済みscenarioを列挙する")
    return parser


# {
#   責務: [main: CLIの入口。終了コード 2=不正入力 > 1=観測対象失敗/canary未検出/enforceのfail > 0]
#   処理: [引数を解釈, profileを検証, サブコマンドを実行]
#   引数: [argv: 引数列(省略時はsys.argv)]
#   戻り値: [終了コード]
# }
def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "list-scenarios":
        for name, kind in scenarios.scenario_names():
            print(f"{kind}\t{name}")
        return EXIT_OK
    try:
        profile = load_profile(Path(args.profile_file))
        if args.cmd == "evaluate":
            return _cmd_evaluate(args, profile)
        allowed = {"fast": ("fast", "deep"), "integration": ("pr", "deep")}[args.layer]
        if args.run_profile not in allowed or args.layer not in profile["layers"]:
            raise ProfileError(
                f"layer {args.layer} does not allow run profile {args.run_profile}"
            )
        if args.layer == "fast":
            return _cmd_run_fast(args, profile)
        return _cmd_run_integration(args, profile, list(args.command))
    except ProfileError as exc:
        print(f"profile_invalid: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except KeyError as exc:
        print(f"profile_invalid: unknown scenario or key {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
