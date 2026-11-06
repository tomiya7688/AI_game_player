"""Windows長枠比較(#795): Cycling・Baseline・Belief Plannerを同一VMで各600秒、同条件で比較するRunner。"""

from __future__ import annotations

import argparse
from hashlib import sha256
from itertools import permutations
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any, Callable

from tools.windows_long_comparison_stats import (
    classify_run,
    episode_record,
    run_metrics,
)

RUN_SCHEMA = "windows-long-run/v1"
BLOCK_SCHEMA = "windows-long-block/v1"
TRACK = "windows_long_600s"
PROVIDERS = ("cycling", "baseline", "planner")
REF_CLASSES = ("main", "pr", "manual")
PLAY_SECONDS = 600
HARD_TIMEOUT_SECONDS = 780
ATTEMPT_CAP = 5000
RECOVERY_LIMIT = 2
MIN_EPISODE_REMAINING_SECONDS = 60
SEED_STRIDE = 1000
# 全provider・全blockで共通に固定する観測・入力・OCR設定(短比較と同じ)。
PROFILE = {
    "wgc_timeout_ms": 250,
    "settle_ms": 1000,
    "frame_preprocessing": "native FAST_CV",
    "score_ocr": "post-decision screenshot only, confidence >= 0.65",
    "baseline": {"min_margin": 0, "min_recognition": 0.65},
    "planning": {
        "depth": 2,
        "nodes": 10000,
        "wall_time_ms": 400,
        "global_world_risk": "unknown",
    },
}
BUDGET_KEYS = (
    "play_seconds",
    "hard_timeout_seconds",
    "attempt_cap",
    "recovery_limit",
    "min_episode_remaining_seconds",
)


# {
#   責務: [
#     provider_order: block番号から3 providerの実行順(6順列を巡回)を決める
#   ]
#   処理: [
#     1: permutations順の block_index % 6 番目を返す
#   ]
#   引数: [
#     block_index: 0始まりのblock番号
#   ]
#   戻り値: [
#     tuple: providerの実行順
#   ]
# }
def provider_order(block_index: int) -> tuple[str, ...]:
    return list(permutations(PROVIDERS))[block_index % 6]


# {
#   責務: [
#     episode_seed: episode番号k(1始まり)のseedを3 provider共通で決める
#   ]
#   処理: [
#     1: (base + 1000*(k-1)) mod 2^32 を返す
#   ]
#   引数: [
#     base_seed: block共通のbase seed
#     index: episode番号
#   ]
#   戻り値: [
#     int: uint32 seed
#   ]
# }
def episode_seed(base_seed: int, index: int) -> int:
    return (base_seed + SEED_STRIDE * (index - 1)) % (1 << 32)


# {
#   責務: [
#     budget_record: 予算をreportへ保存する値にする
#   ]
#   処理: [
#     1: 固定定数と指定の再生時間を辞書にする
#   ]
#   引数: [
#     play_seconds: wall予算(秒)
#   ]
#   戻り値: [
#     dict: budget
#   ]
# }
def budget_record(play_seconds: int = PLAY_SECONDS) -> dict[str, Any]:
    return {
        "play_seconds": play_seconds,
        "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
        "attempt_cap": ATTEMPT_CAP,
        "recovery_limit": RECOVERY_LIMIT,
        "min_episode_remaining_seconds": MIN_EPISODE_REMAINING_SECONDS,
    }


# {
#   責務: [
#     ConditionUnavailable: 比較条件(hardware・native DLL・OCR等)を取得できず、入力前に非0で終了する理由を表す
#   ]
#   フィールド: []
# }
class ConditionUnavailable(RuntimeError):
    """比較条件(hardware・native・OCR等)を取得できない。入力前に非0で終了する理由。"""


# {
#   責務: [
#     hardware_fingerprint: 同一VM照合用のhardware情報を取得する(取得不能はunknownにせず失敗)
#   ]
#   処理: [
#     1: CPU型番・論理core・RAM・OS build・runner imageを集める
#     2: 必須値が欠ければConditionUnavailable
#   ]
#   引数: []
#   戻り値: [
#     dict: hardware情報
#   ]
#   エラー: [
#     必須値を取得できない場合ConditionUnavailable
#   ]
# }
def hardware_fingerprint() -> dict[str, Any]:
    import psutil

    info = {
        "cpu_model": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
        "logical_cores": os.cpu_count(),
        "ram_bytes": psutil.virtual_memory().total,
        "os_build": platform.version(),
        "machine": platform.machine(),
        "image_version": os.environ.get("ImageVersion", "not_ci"),
        "image_os": os.environ.get("ImageOS", "not_ci"),
    }
    if (
        not info["cpu_model"]
        or not info["logical_cores"]
        or not info["ram_bytes"]
        or not info["os_build"]
    ):
        raise ConditionUnavailable("hardware fingerprint is unavailable")
    return info


# {
#   責務: [
#     _file_sha256: ファイル内容のSHA256を返す
#   ]
#   処理: [
#     1: バイト列を読んでsha256を取る
#   ]
#   引数: [
#     path: 対象ファイル
#   ]
#   戻り値: [
#     str: 16進SHA256
#   ]
# }
def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


# {
#   責務: [
#     condition_fingerprint: 全run・全blockで一致すべき比較条件のfingerprintを作る
#   ]
#   処理: [
#     1: git commit・Python source・game lock・native DLL・harness・OCR版・Python版・profileを集める
#     2: 正準JSONのSHA256を返す
#   ]
#   引数: []
#   戻り値: [
#     tuple: (fingerprint, components)
#   ]
#   エラー: [
#     native DLL・OCRが取得できない場合ConditionUnavailable
#   ]
# }
def condition_fingerprint() -> tuple[str, dict[str, Any]]:
    from tools import compare_2048_windows_planning as planning
    from tools.planning_comparison_source import source_fingerprint
    from tools.reference_game_2048_source import SOURCE_LOCK_PATH

    runtime = os.environ.get("KADOKA_NATIVE_RUNTIME", "")
    if not runtime or not Path(runtime).is_file():
        raise ConditionUnavailable("KADOKA_NATIVE_RUNTIME native DLL is unavailable")
    tesseract = os.environ.get("TESSERACT_CMD", "tesseract")
    try:
        version = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            [tesseract, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if version.returncode != 0:
            raise ConditionUnavailable("Tesseract version command failed")
        ocr_version = (version.stdout or version.stderr).splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError) as error:
        raise ConditionUnavailable("Tesseract version is unavailable") from error
    planning_file = getattr(planning, "__file__", None)
    if not planning_file:
        raise ConditionUnavailable("short comparison harness path is unavailable")
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, encoding="utf-8"
        ).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ConditionUnavailable("git commit is unavailable") from error
    here = Path(__file__).resolve().parent
    components = {
        "git_commit": git_commit,
        "source_sha256": source_fingerprint()["sha256"],
        "game_source_lock_sha256": _file_sha256(SOURCE_LOCK_PATH),
        "native_runtime_sha256": _file_sha256(Path(runtime)),
        "harness_sha256": {
            "compare_2048_windows_planning": _file_sha256(Path(planning_file)),
            "compare_2048_windows_long": _file_sha256(Path(__file__)),
            "windows_long_comparison_stats": _file_sha256(
                here / "windows_long_comparison_stats.py"
            ),
        },
        "tesseract": ocr_version,
        "python": platform.python_version(),
        "profile": PROFILE,
    }
    canonical = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest(), components


# {
#   責務: [
#     write_json: JSONを一時ファイル経由で原子的に書く(途中でprocessが落ちても部分結果が残る)
#   ]
#   処理: [
#     1: 同dirの一時ファイルへ書く
#     2: os.replaceで置換する
#   ]
#   引数: [
#     path: 書込先
#     payload: JSON化できる値
#   ]
#   戻り値: []
# }
def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


# {
#   責務: [
#     _failure_result: run_episodeが例外を投げた時のepisode結果を失敗として作る
#   ]
#   処理: [
#     1: 例外型と先頭2000文字のmessageをfailureに入れる
#   ]
#   引数: [
#     error: 捕捉した例外
#   ]
#   戻り値: [
#     dict: success=Falseの結果
#   ]
# }
def _failure_result(error: BaseException) -> dict[str, Any]:
    return {
        "success": False,
        "failure_stage": "run_episode_exception",
        "failure": {"type": type(error).__name__, "message": str(error)[:2000]},
    }


# {
#   責務: [
#     finalize_report: episode記録から指標・run_status・stop_reasonを再計算してreportへ反映する
#   ]
#   処理: [
#     1: classify_runで状態値からstatusを決める
#     2: run_metricsで指標を集計する
#   ]
#   引数: [
#     report: run-report(episodesを含む)
#     stop_reason: 停止理由
#   ]
#   戻り値: [
#     dict: 更新したreport
#   ]
# }
def finalize_report(report: dict[str, Any], stop_reason: str) -> dict[str, Any]:
    status, reason = classify_run(report["episodes"], stop_reason)
    report["run_status"] = status
    report["stop_reason"] = reason
    report["metrics"] = run_metrics(report["provider"], report["episodes"])
    return report


# {
#   責務: [
#     run_provider: 1 providerをwall予算内でepisodeを繰り返して実行し、run-reportを逐次書く
#   ]
#   処理: [
#     1: deadlineを決め、残りが最小未満なら新episodeを始めない
#     2: run_episodeを呼び、episode記録を追加して途中経過を書く
#     3: 失敗はrecoveryを消費し、上限超過・致命失敗で停止する
#     4: 終了時にfinalize_reportで状態値から成否を決める
#   ]
#   引数: [
#     identity: track/ref_class/git_commit/provider/base_seed/order/fingerprint/hardware/budget
#     output: provider出力dir
#     cache: ゲームcache dir
#     run_episode_fn: episode実行関数(テストで差替え)
#     clock: 単調時計(テストで差替え)
#   ]
#   戻り値: [
#     dict: 最終run-report
#   ]
# }
def run_provider(
    identity: dict[str, Any],
    output: Path,
    cache: Path,
    run_episode_fn: Callable[..., dict[str, Any]],
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    provider, base_seed = identity["provider"], identity["base_seed"]
    budget = identity["budget"]
    started = clock()
    deadline = started + budget["play_seconds"]
    report: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        **identity,
        "run_status": "running",
        "stop_reason": None,
        "episodes": [],
    }
    report_path = output / "run-report.json"
    failures, index = 0, 0
    stop_reason = "wall_budget_reached"
    while True:
        remaining = deadline - clock()
        if remaining <= 0 or (
            index > 0 and remaining < budget["min_episode_remaining_seconds"]
        ):
            stop_reason = "wall_budget_reached"
            break
        index += 1
        seed = episode_seed(base_seed, index)
        try:
            result = run_episode_fn(
                provider,
                seed,
                budget["attempt_cap"],
                cache,
                output / f"episode-{index:03d}",
                save_failure_evidence=True,
                deadline_monotonic=deadline,
            )
        except Exception as error:  # 1 episodeの例外でrun全体の会計を失わない
            result = _failure_result(error)
        record = episode_record(index, seed, result)
        report["episodes"].append(record)
        write_json(report_path, report)
        if (
            record["failure_stage"] in ("stop_probe", "stale_generation_probe")
            or record["cleanup_failure"]
        ):
            stop_reason = "fatal:" + str(record["failure_stage"] or "cleanup")
            break
        if record["episode_status"] != "completed":
            failures += 1
            if failures > budget["recovery_limit"] and clock() < deadline:
                stop_reason = "recovery_exhausted"
                break
    finished = clock()
    report["wall_seconds"] = finished - started
    report["unused_budget_seconds"] = max(0.0, deadline - finished)
    decision_seconds = sum(sum(e["latency_ms"]) for e in report["episodes"]) / 1000
    episode_wall = sum(e["wall_seconds"] or 0 for e in report["episodes"])
    report["non_decision_seconds"] = max(0.0, episode_wall - decision_seconds)
    finalize_report(report, stop_reason)
    write_json(report_path, report)
    return report


# {
#   責務: [
#     build_identity: run-reportの識別・条件部分を作る(worker・parent共通)
#   ]
#   処理: [
#     1: hardware・condition fingerprintを取得する
#   ]
#   引数: [
#     provider: provider名
#     base_seed: block base seed
#     block_index: block番号
#     ref_class: main/pr/manual
#     play_seconds: wall予算
#   ]
#   戻り値: [
#     dict: identity
#   ]
# }
def build_identity(
    provider: str, base_seed: int, block_index: int, ref_class: str, play_seconds: int
) -> dict[str, Any]:
    fingerprint, components = condition_fingerprint()
    order = provider_order(block_index)
    return {
        "track": TRACK,
        "ref_class": ref_class,
        "git_commit": components["git_commit"],
        "provider": provider,
        "base_seed": base_seed,
        "block_index": block_index,
        "provider_order_index": order.index(provider),
        "condition_fingerprint": fingerprint,
        "condition_components": components,
        "hardware": hardware_fingerprint(),
        "budget": budget_record(play_seconds),
    }


# {
#   責務: [
#     _kill_tree: hard timeoutしたworkerのprocess木を回収する
#   ]
#   処理: [
#     1: 子孫とworkerをkillする
#     2: 10秒待ち、まだ生きているpidを集める
#   ]
#   引数: [
#     process: worker process
#   ]
#   戻り値: [
#     list: 回収できなかったpid
#   ]
# }
def _kill_tree(process: subprocess.Popen[Any]) -> list[int]:
    import psutil

    survivors: list[int] = []
    try:
        parent = psutil.Process(process.pid)
        family = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return survivors
    for item in family:
        try:
            item.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(family, timeout=10)
    return [item.pid for item in alive]


# {
#   責務: [
#     run_provider_subprocess: providerを子processで実行し、hard timeoutで回収して状態値からrunを確定する
#   ]
#   処理: [
#     1: workerを起動し、hard timeout(780秒)まで待つ
#     2: 超過時はprocess木を回収しhard_timeoutにする
#     3: report欠落・running残りは致命(worker_no_report/worker_exit)にする
#   ]
#   引数: [
#     args: base_seed/block_index/ref_class/play_seconds/cache_dir/output等
#     provider: provider名
#     identity: 親が計算した識別(欠落時の合成用)
#     spawn: Popen互換の起動関数(テストで差替え)
#   ]
#   戻り値: [
#     dict: 確定したrun-report(leftover_pidsを含む)
#   ]
# }
def run_provider_subprocess(
    args: argparse.Namespace,
    provider: str,
    identity: dict[str, Any],
    spawn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    output = args.output / provider
    report_path = output / "run-report.json"
    if report_path.exists():
        report_path.unlink()
    command = [
        sys.executable,
        "-u",
        "-X",
        "faulthandler",
        "-m",
        "tools.compare_2048_windows_long",
        "--worker",
        "--provider",
        provider,
        "--base-seed",
        str(args.base_seed),
        "--block-index",
        str(args.block_index),
        "--ref-class",
        args.ref_class,
        "--duration-seconds",
        str(args.duration_seconds),
        "--cache-dir",
        str(args.cache_dir),
        "--output",
        str(args.output),
        "--allow-live-input",
    ]
    launcher = spawn or subprocess.Popen
    process = launcher(
        command
    )  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    timed_out, leftovers = False, []
    try:
        process.wait(timeout=identity["budget"]["hard_timeout_seconds"])
    except subprocess.TimeoutExpired:
        timed_out = True
        leftovers = _kill_tree(process)
    report: dict[str, Any] | None = None
    try:
        loaded = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("episodes"), list):
            report = loaded
    except (OSError, ValueError):
        report = None
    if report is None:
        report = {"schema": RUN_SCHEMA, **identity, "episodes": []}
        stop_reason = "hard_timeout" if timed_out else "fatal:worker_no_report"
    elif timed_out:
        stop_reason = "hard_timeout"
    elif report.get("run_status") == "running":
        stop_reason = "fatal:worker_exit"
    else:
        stop_reason = report.get("stop_reason") or "fatal:worker_exit"
    if (
        timed_out
        or report.get("run_status") in (None, "running")
        or report.get("metrics") is None
    ):
        finalize_report(report, stop_reason)
    report["leftover_pids"] = leftovers
    write_json(report_path, report)
    return report


# {
#   責務: [
#     block_consistency: 3 provider間の条件一致(fingerprint・hardware・budget・seed fixture)を状態値で検査する
#   ]
#   処理: [
#     1: 各項目がproviderすべてで等しいかを確認する
#     2: 同じepisode番号のseed_fixtureが一致するか確認する
#     3: 回収できなかった残プロセスを違反に加える
#   ]
#   引数: [
#     runs: provider→run-report
#   ]
#   戻り値: [
#     dict: 各一致フラグとviolations
#   ]
# }
def block_consistency(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def same(key: str) -> bool:
        values = {json.dumps(r.get(key), sort_keys=True) for r in runs.values()}
        return len(values) == 1 and set(runs) == set(PROVIDERS)

    fixtures: dict[int, set[str]] = {}
    for run in runs.values():
        for episode in run.get("episodes", []):
            if episode.get("seed_fixture") is not None:
                fixtures.setdefault(episode["episode"], set()).add(
                    json.dumps(episode["seed_fixture"], sort_keys=True)
                )
    flags = {
        "condition_fingerprint_equal": same("condition_fingerprint"),
        "hardware_equal": same("hardware"),
        "budget_equal": same("budget"),
        "seed_fixture_equal": all(len(v) == 1 for v in fixtures.values()),
        "no_leftover_processes": not any(r.get("leftover_pids") for r in runs.values()),
    }
    violations = [name for name, ok in flags.items() if not ok]
    return {**flags, "violations": violations}


# {
#   責務: [
#     run_block: 1 base seedで3 providerを決まった順に逐次実行し、block-reportを書く
#   ]
#   処理: [
#     1: 親のidentityを計算する
#     2: provider_order順にworkerを実行し、各runを確定する
#     3: 条件一致を検査してblock-reportを書く
#   ]
#   引数: [
#     args: CLI引数
#     spawn: worker起動関数(テストで差替え)
#   ]
#   戻り値: [
#     dict: block-report
#   ]
# }
def run_block(
    args: argparse.Namespace, spawn: Callable[..., Any] | None = None
) -> dict[str, Any]:
    order = provider_order(args.block_index)
    parent = build_identity(
        order[0],
        args.base_seed,
        args.block_index,
        args.ref_class,
        args.duration_seconds,
    )
    runs: dict[str, dict[str, Any]] = {}
    for provider in order:
        identity = {
            **parent,
            "provider": provider,
            "provider_order_index": order.index(provider),
        }
        runs[provider] = run_provider_subprocess(args, provider, identity, spawn)
        # provider間でgame processが残っていないことは leftover_pids で検査する。
    consistency = block_consistency(runs)
    block = {
        "schema": BLOCK_SCHEMA,
        "track": TRACK,
        "ref_class": args.ref_class,
        "git_commit": parent["git_commit"],
        "base_seed": args.base_seed,
        "block_index": args.block_index,
        "provider_order": list(order),
        "condition_fingerprint": parent["condition_fingerprint"],
        "hardware": parent["hardware"],
        "budget": parent["budget"],
        "consistency": consistency,
        "block_status": "valid" if not consistency["violations"] else "invalid",
        "runs": runs,
    }
    write_json(args.output / "block-report.json", block)
    return block


# {
#   責務: [
#     block_exit_code: block-reportの状態値から終了コードを決める
#   ]
#   処理: [
#     1: 無効blockは1、全runがcompletedなら0、それ以外は2を返す
#   ]
#   引数: [
#     block: block-report
#   ]
#   戻り値: [
#     int: 0/1/2
#   ]
# }
def block_exit_code(block: dict[str, Any]) -> int:
    if block["block_status"] != "valid":
        return 1
    return (
        0 if all(r["run_status"] == "completed" for r in block["runs"].values()) else 2
    )


# {
#   責務: [
#     _parser: Runner CLIの引数定義を作る
#   ]
#   処理: [
#     1: block/worker用の引数を登録する(統計閾値は引数にしない)
#   ]
#   引数: [
#   ]
#   戻り値: [
#     ArgumentParser: 引数定義
#   ]
# }
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-seed", type=int, default=479)
    parser.add_argument("--block-index", type=int, default=0)
    parser.add_argument("--ref-class", choices=REF_CLASSES, default="manual")
    parser.add_argument("--duration-seconds", type=int, default=PLAY_SECONDS)
    parser.add_argument("--allow-live-input", action="store_true")
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("build/reference_games/2048")
    )
    parser.add_argument("--output", type=Path, default=Path("build/windows-long"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--provider", choices=PROVIDERS, help=argparse.SUPPRESS)
    return parser


# {
#   責務: [
#     _preconditions: 入力前に実行条件を検査する
#   ]
#   処理: [
#     1: 明示許可・値域・Windows・OCR・psutilを順に確認する
#   ]
#   引数: [
#     args: CLI引数
#   ]
#   戻り値: [
#     str|None: 不足の理由。問題なければNone
#   ]
# }
def _preconditions(args: argparse.Namespace) -> str | None:
    if not args.allow_live_input:
        return "owned Windows input comparison requires explicit --allow-live-input"
    if not 0 <= args.base_seed <= 0xFFFFFFFF or not 0 <= args.block_index <= 1000:
        return "base seed uint32 and block index 0..1000 required"
    if not 60 <= args.duration_seconds <= PLAY_SECONDS:
        return "duration must be 60..600 seconds"
    if os.name != "nt":
        return "this comparison requires actual Windows WGC/input"
    from ai_game_player.ocr_recognizer import TesseractOcrRecognizer

    if TesseractOcrRecognizer.optional() is None:
        return "score evaluation requires the comparison OCR dependency"
    try:
        import psutil  # noqa: F401
    except ImportError:
        return "resource sampling requires psutil"
    return None


# {
#   責務: [
#     main: block実行(親)またはprovider実行(worker)のCLI入口
#   ]
#   処理: [
#     1: 入力前に条件を検査し、不足なら非0で終了する
#     2: workerなら1 providerを実行、親なら3 providerのblockを実行する
#   ]
#   引数: [
#     argv: コマンド引数
#   ]
#   戻り値: [
#     int: 終了コード(0=全completed,2=打切り/失敗を含む,1=契約違反)
#   ]
# }
def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    problem = _preconditions(args)
    if problem is not None:
        print(f"comparison precondition failed: {problem}", file=sys.stderr)
        return 1
    try:
        if args.worker:
            if args.provider is None:
                print("--worker requires --provider", file=sys.stderr)
                return 1
            from tools.compare_2048_windows_planning import run_episode

            identity = build_identity(
                args.provider,
                args.base_seed,
                args.block_index,
                args.ref_class,
                args.duration_seconds,
            )
            report = run_provider(
                identity, args.output / args.provider, args.cache_dir, run_episode
            )
            print(
                json.dumps(
                    {"provider": args.provider, "run_status": report["run_status"]}
                ),
                flush=True,
            )
            return 0 if report["run_status"] == "completed" else 2
        block = run_block(args)
    except ConditionUnavailable as error:
        print(f"comparison condition unavailable: {error}", file=sys.stderr)
        return 1
    print(args.output / "block-report.json")
    return block_exit_code(block)


if __name__ == "__main__":
    raise SystemExit(main())
