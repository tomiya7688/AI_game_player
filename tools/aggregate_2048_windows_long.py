"""Windows長枠比較(#795)のmanifest作成と、期待集合を母数にした集計・比較表の生成。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from tools.compare_2048_windows_long import (
    BLOCK_SCHEMA,
    PROVIDERS,
    REF_CLASSES,
    RUN_SCHEMA,
    TRACK,
    budget_record,
)
from tools.windows_long_comparison_stats import (
    CORRECTED_ALPHA,
    N_MIN_VERDICT,
    describe,
    measured,
    paired_contrast,
    percentile_nearest_rank,
    validate_metric,
    wilson_interval,
)

MANIFEST_SCHEMA = "windows-long-manifest/v1"
AGGREGATE_SCHEMA = "windows-long-aggregate/v1"
MAX_SEED_COUNT = 32
RUN_STATUSES = ("completed", "partial", "failed")
ENTRY_STATUSES = (*RUN_STATUSES, "missing", "invalid")
CONTRASTS = (
    ("baseline", "cycling"),
    ("planner", "cycling"),
    ("planner", "baseline"),
)
PRIMARY_METRIC = "episode_mean_score"
# verdictを付けない探索的指標(値はmetrics内のキー)。
SECONDARY_METRICS = (
    "max_tile",
    "survival_median_actions",
    "invalid_no_op_rate",
    "abstain_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "cpu_seconds",
    "peak_rss_bytes",
)
SUMMARY_METRICS = (
    PRIMARY_METRIC,
    "best_episode_score",
    *SECONDARY_METRICS,
    "reobserve_rate",
    "nodes",
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BLOCK_REPORT_NAME = "block-report.json"


# {
#   責務: [
#     build_manifest: 期待するblock集合(母数)を作る
#   ]
#   処理: [
#     1: 入力値域を検査する
#     2: base seed列とprovider・budgetを固定して返す
#   ]
#   引数: [
#     seed_start: 最初のbase seed
#     seed_count: block数(1〜32)
#     ref_class: main/pr/manual
#     git_commit: 40桁のcommit
#   ]
#   戻り値: [
#     dict: manifest
#   ]
#   エラー: [
#     値域外の場合ValueError
#   ]
# }
def build_manifest(
    seed_start: int, seed_count: int, ref_class: str, git_commit: str
) -> dict[str, Any]:
    if not 1 <= seed_count <= MAX_SEED_COUNT:
        raise ValueError(f"seed_count must be 1..{MAX_SEED_COUNT}")
    if not 0 <= seed_start <= 0xFFFFFFFF - seed_count:
        raise ValueError("seed_start must leave all seeds within uint32")
    if ref_class not in REF_CLASSES:
        raise ValueError("ref_class must be main, pr or manual")
    if not COMMIT_PATTERN.match(git_commit):
        raise ValueError("git_commit must be a 40 hex commit")
    return {
        "schema": MANIFEST_SCHEMA,
        "track": TRACK,
        "ref_class": ref_class,
        "git_commit": git_commit,
        "base_seeds": [seed_start + i for i in range(seed_count)],
        "providers": list(PROVIDERS),
        "budget": budget_record(),
    }


# {
#   責務: [
#     manifest_problems: manifest自体の不正を検査する
#   ]
#   処理: [
#     1: schema・track・ref_class・commit・seed集合・provider・budgetを確認する
#   ]
#   引数: [
#     manifest: 読み込んだmanifest
#   ]
#   戻り値: [
#     list: 違反理由
#   ]
# }
def manifest_problems(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest is not an object"]
    problems = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append("manifest schema mismatch")
    if manifest.get("track") != TRACK:
        problems.append("manifest track mismatch")
    if manifest.get("ref_class") not in REF_CLASSES:
        problems.append("manifest ref_class invalid")
    if not isinstance(manifest.get("git_commit"), str) or not COMMIT_PATTERN.match(
        manifest["git_commit"]
    ):
        problems.append("manifest git_commit invalid")
    seeds = manifest.get("base_seeds")
    if (
        not isinstance(seeds, list)
        or not 1 <= len(seeds) <= MAX_SEED_COUNT
        or any(type(s) is not int or not 0 <= s <= 0xFFFFFFFF for s in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        problems.append("manifest base_seeds invalid")
    if manifest.get("providers") != list(PROVIDERS):
        problems.append("manifest providers mismatch")
    if manifest.get("budget") != budget_record():
        problems.append("manifest budget mismatch")
    return problems


# {
#   責務: [
#     block_problems: 1つのblock-reportがmanifestと契約に合うか検査する
#   ]
#   処理: [
#     1: schema・track/ref_class/commit・base seed・block_statusを確認する
#     2: 3 providerのrun schema・budget・指標の型規則を確認する
#   ]
#   引数: [
#     block: 読み込んだblock-report
#     manifest: 期待manifest
#   ]
#   戻り値: [
#     list: 違反理由(空なら有効)
#   ]
# }
def block_problems(block: Any, manifest: dict[str, Any]) -> list[str]:
    if not isinstance(block, dict) or block.get("schema") != BLOCK_SCHEMA:
        return ["block schema mismatch"]
    problems = []
    for key in ("track", "ref_class", "git_commit"):
        if block.get(key) != manifest[key]:
            problems.append(f"{key} differs from manifest")
    if block.get("base_seed") not in manifest["base_seeds"]:
        problems.append("base_seed is not in the manifest")
    if block.get("budget") != manifest["budget"]:
        problems.append("budget differs from manifest")
    if block.get("block_status") != "valid":
        violations = (block.get("consistency") or {}).get("violations")
        problems.append(f"block condition check failed: {violations}")
    runs = block.get("runs")
    if not isinstance(runs, dict) or set(runs) != set(manifest["providers"]):
        return [*problems, "runs must contain exactly the manifest providers"]
    for provider, run in runs.items():
        if not isinstance(run, dict) or run.get("schema") != RUN_SCHEMA:
            problems.append(f"{provider}: run schema mismatch")
            continue
        if run.get("run_status") not in RUN_STATUSES:
            problems.append(f"{provider}: run_status invalid")
        if run.get("provider") != provider or run.get("base_seed") != block.get(
            "base_seed"
        ):
            problems.append(f"{provider}: run identity differs from block")
        metrics = run.get("metrics")
        if not isinstance(metrics, dict):
            problems.append(f"{provider}: metrics missing")
            continue
        for name, metric in metrics.items():
            if isinstance(metric, dict):
                problems.extend(
                    f"{provider}.{name}: {m}" for m in validate_metric(metric)
                )
    return problems


# {
#   責務: [
#     _load_json: JSONを読み、失敗は例外でなく理由文字列で返す
#   ]
#   処理: [
#     1: 読込とparseを試みる
#   ]
#   引数: [
#     path: 対象ファイル
#   ]
#   戻り値: [
#     tuple: (値, エラー文字列|None)
#   ]
# }
def _load_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        return None, f"{type(error).__name__}: {str(error)[:200]}"


# {
#   責務: [
#     _metric_value: measuredの指標だけ数値を返す
#   ]
#   処理: [
#     1: status=measuredの時だけvalueを返す
#   ]
#   引数: [
#     run: run-report|None
#     name: 指標名
#   ]
#   戻り値: [
#     float|None: 値。unknown/not_applicable/欠落はNone
#   ]
# }
def _metric_value(run: dict[str, Any] | None, name: str) -> float | None:
    if run is None:
        return None
    metric = (run.get("metrics") or {}).get(name)
    if isinstance(metric, dict) and metric.get("status") == "measured":
        return metric["value"]
    return None


# {
#   責務: [
#     _usable: 入力ありのrunか判定する
#   ]
#   処理: [
#     1: actions_totalが正かを見る
#   ]
#   引数: [
#     run: run-report|None
#   ]
#   戻り値: [
#     bool: 一次解析に含められるか
#   ]
# }
def _usable(run: dict[str, Any] | None) -> bool:
    return run is not None and (run.get("metrics") or {}).get("actions_total", 0) > 0


# {
#   責務: [
#     collect_blocks: artifactを読み、manifestの期待集合に割り当てる
#   ]
#   処理: [
#     1: 全block-report.jsonを読む
#     2: 不正・重複・manifest外のblockを契約違反として記録する
#     3: 条件fingerprintの混在を検出し、少数派を無効にする
#   ]
#   引数: [
#     manifest: 期待manifest
#     artifacts: artifactを集めたdir
#   ]
#   戻り値: [
#     tuple: (seed→有効block, seed→無効理由, 違反リスト)
#   ]
# }
def collect_blocks(
    manifest: dict[str, Any], artifacts: Path
) -> tuple[dict[int, dict[str, Any]], dict[int, list[str]], list[str]]:
    valid: dict[int, dict[str, Any]] = {}
    invalid: dict[int, list[str]] = {}
    violations: list[str] = []
    for path in sorted(artifacts.rglob(BLOCK_REPORT_NAME)):
        block, error = _load_json(path)
        if error is not None:
            violations.append(f"{path}: unreadable block report ({error})")
            continue
        seed = block.get("base_seed") if isinstance(block, dict) else None
        if seed not in manifest["base_seeds"]:
            violations.append(
                f"{path}: block outside the manifest (base_seed={seed!r})"
            )
            continue
        if seed in valid or seed in invalid:
            violations.append(f"{path}: duplicate block for base_seed {seed}")
            invalid.setdefault(seed, []).append("duplicate block report")
            valid.pop(seed, None)
            continue
        problems = block_problems(block, manifest)
        if problems:
            invalid[seed] = problems
            violations.extend(f"seed {seed}: {p}" for p in problems)
        else:
            valid[seed] = block
    fingerprints: dict[str, list[int]] = {}
    for seed, block in valid.items():
        fingerprints.setdefault(block["condition_fingerprint"], []).append(seed)
    if len(fingerprints) > 1:
        # 最多数(同数なら最小seedを含む組)を基準にし、他は無効にする。
        reference = max(fingerprints.items(), key=lambda kv: (len(kv[1]), -min(kv[1])))[
            0
        ]
        for fingerprint, seeds in fingerprints.items():
            if fingerprint == reference:
                continue
            for seed in seeds:
                valid.pop(seed)
                invalid[seed] = ["condition_fingerprint differs from the other blocks"]
                violations.append(
                    f"seed {seed}: condition_fingerprint differs from the other blocks"
                )
    return valid, invalid, violations


# {
#   責務: [
#     _entry: (seed×provider)1件の会計行を作る
#   ]
#   処理: [
#     1: invalidはblockの無効理由、無いblockはmissing、それ以外はrun_statusにする
#   ]
#   引数: [
#     seed: base seed
#     provider: provider名
#     valid: 有効block
#     invalid: 無効理由
#   ]
#   戻り値: [
#     dict: 会計行
#   ]
# }
def _entry(seed: int, provider: str, valid: dict, invalid: dict) -> dict[str, Any]:
    if seed in invalid:
        return {
            "seed": seed,
            "provider": provider,
            "status": "invalid",
            "stop_reason": None,
            "reasons": invalid[seed],
            "run": None,
        }
    if seed not in valid:
        return {
            "seed": seed,
            "provider": provider,
            "status": "missing",
            "stop_reason": None,
            "reasons": ["no block report for this expected run"],
            "run": None,
        }
    run = valid[seed]["runs"][provider]
    return {
        "seed": seed,
        "provider": provider,
        "status": run["run_status"],
        "stop_reason": run.get("stop_reason"),
        "reasons": [],
        "run": run,
    }


# {
#   責務: [
#     _contrast_block: 1対比・1指標のblock単位対応あり差を統計にする
#   ]
#   処理: [
#     1: 各seedでa-bの両方が有効な時だけ差を取る
#     2: unknownは除外数に数える
#     3: completed_only/zero_imputeで感度分析にする
#   ]
#   引数: [
#     entries: 会計行の辞書
#     seeds: 期待seed
#     a,b: 対比のprovider
#     metric: 指標名
#     with_verdict: verdictを付けるか
#   ]
#   戻り値: [
#     dict: paired_contrastの結果
#   ]
# }
def _contrast_block(
    entries: dict[tuple[int, str], dict[str, Any]],
    seeds: list[int],
    a: str,
    b: str,
    metric: str,
    *,
    with_verdict: bool,
    completed_only: bool = False,
    zero_impute: bool = False,
) -> dict[str, Any]:
    differences, excluded, imputed = [], 0, 0
    for seed in seeds:
        ra, rb = entries[(seed, a)]["run"], entries[(seed, b)]["run"]
        values = []
        for run in (ra, rb):
            ok = _usable(run) and (
                not completed_only or run["run_status"] == "completed"
            )
            value = _metric_value(run, metric) if ok else None
            if value is None and zero_impute:
                value = 0.0
                imputed += 1
            values.append(value)
        if values[0] is None or values[1] is None:
            excluded += 1
        else:
            differences.append(values[0] - values[1])
    result = paired_contrast(differences, excluded, with_verdict=with_verdict)
    if zero_impute:
        # 置換した値の数を明示する(置換値は参考であり、一次解析・verdictには使わない)。
        result["n_imputed_zero"] = imputed
    return result


# {
#   責務: [
#     aggregate: 期待manifestを母数に、会計・比較表・統計・終了コードを作る
#   ]
#   処理: [
#     1: 全(seed×provider)を1行ずつ並べ、欠落をmissing、不正をinvalidにする
#     2: 契約違反があれば統計とverdictを出さない
#     3: provider別要約、対比(一次=episode_mean_score)、感度分析を作る
#   ]
#   引数: [
#     manifest: 期待manifest
#     artifacts: artifactを集めたdir
#   ]
#   戻り値: [
#     tuple: (report, exit_code)
#   ]
# }
def aggregate(manifest: dict[str, Any], artifacts: Path) -> tuple[dict[str, Any], int]:
    valid, invalid, violations = collect_blocks(manifest, artifacts)
    seeds = list(manifest["base_seeds"])
    entries = {(s, p): _entry(s, p, valid, invalid) for s in seeds for p in PROVIDERS}
    if not valid and not any(e["status"] in RUN_STATUSES for e in entries.values()):
        violations.append("no valid run is available")
    counts = {p: {status: 0 for status in ENTRY_STATUSES} for p in PROVIDERS}
    for (seed, provider), entry in entries.items():
        counts[provider][entry["status"]] += 1
    table = []
    for seed in seeds:
        for provider in PROVIDERS:
            entry = entries[(seed, provider)]
            run = entry["run"]
            metrics = (run or {}).get("metrics") or {}
            table.append(
                {
                    "seed": seed,
                    "provider": provider,
                    "status": entry["status"],
                    "stop_reason": entry["stop_reason"],
                    "reasons": entry["reasons"],
                    "provider_order_index": (run or {}).get("provider_order_index"),
                    "episodes": metrics.get("episodes"),
                    "censored_episodes": metrics.get("censored_episodes"),
                    "actions_total": metrics.get("actions_total"),
                    "metrics": {name: metrics.get(name) for name in SUMMARY_METRICS},
                }
            )
    report: dict[str, Any] = {
        "schema": AGGREGATE_SCHEMA,
        "manifest": manifest,
        "run_counts": counts,
        "contract_violations": violations,
        "comparison_table": table,
        "n_min_verdict": N_MIN_VERDICT,
        "corrected_alpha": CORRECTED_ALPHA,
        "statistics": None,
    }
    if violations:
        report["exit_code"] = 1
        return report, 1
    provider_summary: dict[str, Any] = {}
    for provider in PROVIDERS:
        runs = [
            entries[(s, provider)]["run"]
            for s in seeds
            if _usable(entries[(s, provider)]["run"])
        ]
        n_expected = len(seeds)
        summary: dict[str, Any] = {
            "counts": counts[provider],
            "completed_rate": wilson_interval(
                counts[provider]["completed"], n_expected
            ),
            "usable_runs": len(runs),
            "metrics": {},
        }
        for name in SUMMARY_METRICS:
            values = [
                v for v in (_metric_value(r, name) for r in runs) if v is not None
            ]
            summary["metrics"][name] = {
                **describe(values),
                "n_unknown_or_na": len(runs) - len(values),
            }
        samples = [
            x
            for s in seeds
            for e in ((entries[(s, provider)]["run"] or {}).get("episodes") or [])
            for x in e["latency_ms"]
        ]
        p50, p95 = (
            percentile_nearest_rank(samples, 0.5),
            percentile_nearest_rank(samples, 0.95),
        )
        summary["latency_pooled"] = {
            "scope": "every run with latency samples, including runs without input",
            "samples": len(samples),
            "p50_ms": measured(p50)
            if p50 is not None
            else {"value": None, "status": "unknown"},
            "p95_ms": measured(p95)
            if p95 is not None
            else {"value": None, "status": "unknown"},
        }
        order_counts: dict[str, int] = {}
        for s in seeds:
            run = entries[(s, provider)]["run"]
            if run is not None:
                key = str(run.get("provider_order_index"))
                order_counts[key] = order_counts.get(key, 0) + 1
        summary["provider_order_counts"] = order_counts
        provider_summary[provider] = summary
    contrasts: dict[str, Any] = {}
    for a, b in CONTRASTS:
        name = f"{a}-{b}"
        contrasts[name] = {
            "primary": {
                "metric": PRIMARY_METRIC,
                **_contrast_block(
                    entries, seeds, a, b, PRIMARY_METRIC, with_verdict=True
                ),
            },
            "sensitivity_completed_only": _contrast_block(
                entries,
                seeds,
                a,
                b,
                PRIMARY_METRIC,
                with_verdict=False,
                completed_only=True,
            ),
            "sensitivity_missing_score_as_zero": _contrast_block(
                entries,
                seeds,
                a,
                b,
                PRIMARY_METRIC,
                with_verdict=False,
                zero_impute=True,
            ),
            "exploratory": {
                metric: _contrast_block(
                    entries, seeds, a, b, metric, with_verdict=False
                )
                for metric in SECONDARY_METRICS
            },
        }
    report["statistics"] = {
        "provider_summary": provider_summary,
        "contrasts": contrasts,
    }
    complete = all(e["status"] == "completed" for e in entries.values())
    report["exit_code"] = 0 if complete else 2
    return report, report["exit_code"]


# {
#   責務: [
#     _cell: 比較表の1セルの文字列を作る
#   ]
#   処理: [
#     1: measuredは数値、他はstatus名にする
#   ]
#   引数: [
#     metric: {value,status}
#   ]
#   戻り値: [
#     str: 表示文字列
#   ]
# }
def _cell(metric: Any) -> str:
    if not isinstance(metric, dict):
        return "-"
    if metric.get("status") == "measured":
        value = metric["value"]
        return f"{value:.4g}" if isinstance(value, float) else str(value)
    return str(metric.get("status", "-"))


# {
#   責務: [
#     render_markdown: 比較表と統計を読める形のMarkdownにする
#   ]
#   処理: [
#     1: 先頭に条件と標本数の注意を書く
#     2: 全(seed×provider)行の表、対比の要約を並べる
#   ]
#   引数: [
#     report: aggregateのreport
#   ]
#   戻り値: [
#     str: Markdown
#   ]
# }
def render_markdown(report: dict[str, Any]) -> str:
    manifest = report["manifest"]
    lines = [
        "# Windows長枠比較: 測定条件と標本数つきの比較",
        "",
        f"- track: {manifest['track']} / ref_class: {manifest['ref_class']} / commit: {manifest['git_commit']}",
        f"- 期待block数: {len(manifest['base_seeds'])} (seed {manifest['base_seeds'][0]}..) / provider: {', '.join(manifest['providers'])}",
        f"- verdictの最小標本数: {report['n_min_verdict']} block。それ未満は区間・verdictを出さず insufficient_samples です。",
        "- 未計測は unknown、対象外は not_applicable と表示し、0へ置換しません。",
        "",
    ]
    if report["contract_violations"]:
        lines += ["## 契約違反(統計は出していません)", ""]
        lines += [f"- {v}" for v in report["contract_violations"]]
        lines.append("")
    lines += [
        "## 比較表(全block×provider)",
        "",
        "| seed | provider | status | stop | episodes | censored | actions | mean score | max tile | survival | p50 ms | p95 ms | CPU s | peak RSS | nodes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["comparison_table"]:
        m = row["metrics"]
        lines.append(
            "| {seed} | {provider} | {status} | {stop} | {ep} | {cens} | {act} | {s} | {t} | {sv} | {p50} | {p95} | {cpu} | {rss} | {nodes} |".format(
                seed=row["seed"],
                provider=row["provider"],
                status=row["status"],
                stop=row["stop_reason"] or "-",
                ep=row["episodes"] if row["episodes"] is not None else "-",
                cens=row["censored_episodes"]
                if row["censored_episodes"] is not None
                else "-",
                act=row["actions_total"] if row["actions_total"] is not None else "-",
                s=_cell(m.get("episode_mean_score")),
                t=_cell(m.get("max_tile")),
                sv=_cell(m.get("survival_median_actions")),
                p50=_cell(m.get("latency_p50_ms")),
                p95=_cell(m.get("latency_p95_ms")),
                cpu=_cell(m.get("cpu_seconds")),
                rss=_cell(m.get("peak_rss_bytes")),
                nodes=_cell(m.get("nodes")),
            )
        )
    statistics = report["statistics"]
    if statistics:
        lines += [
            "",
            "## 対比(一次指標 episode_mean_score、block単位の対応あり差)",
            "",
            "| contrast | n_pairs | 除外(unknown) | 平均差 | 区間 | p値 | 勝/分/負 | verdict |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, contrast in statistics["contrasts"].items():
            p = contrast["primary"]
            interval = (
                "-"
                if p["interval"] is None
                else f"[{p['interval'][0]:.4g}, {p['interval'][1]:.4g}]"
            )
            mean = (
                "-" if p["mean_difference"] is None else f"{p['mean_difference']:.4g}"
            )
            pv = "-" if p["p_value"] is None else f"{p['p_value']:.4g}"
            lines.append(
                f"| {name} | {p['n_pairs']} | {p['n_excluded_unknown']} | {mean} | {interval} | {pv} | {p['wins']}/{p['ties']}/{p['losses']} | {p['verdict']} ({p['status']}) |"
            )
    return "\n".join(lines) + "\n"


# {
#   責務: [
#     _write: 親dirを作ってUTF-8で書く
#   ]
#   処理: [
#     1: mkdirして書込む
#   ]
#   引数: [
#     path: 出力先
#     text: 内容
#   ]
#   戻り値: []
# }
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# {
#   責務: [
#     main: plan(manifest/matrix作成)とaggregate(集計)のCLI入口
#   ]
#   処理: [
#     1: planはmanifest.jsonを書きmatrix JSONを標準出力へ出す
#     2: aggregateはreport.json/report.mdを書き、状態値の終了コードを返す
#   ]
#   引数: [
#     argv: コマンド引数
#   ]
#   戻り値: [
#     int: 0=全completed,2=会計完全だが打切り/失敗/欠測あり,1=契約違反
#   ]
# }
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--seed-start", type=int, default=479)
    plan.add_argument("--seed-count", type=int, default=8)
    plan.add_argument("--ref-class", default="manual")
    plan.add_argument("--git-commit", required=True)
    plan.add_argument("--output", type=Path, required=True)
    agg = sub.add_parser("aggregate")
    agg.add_argument("--manifest", type=Path, required=True)
    agg.add_argument("--artifacts", type=Path, required=True)
    agg.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        try:
            manifest = build_manifest(
                args.seed_start, args.seed_count, args.ref_class, args.git_commit
            )
        except ValueError as error:
            print(f"invalid plan: {error}", file=sys.stderr)
            return 1
        _write(args.output, json.dumps(manifest, indent=2))
        include = [
            {"seed": s, "index": i} for i, s in enumerate(manifest["base_seeds"])
        ]
        print(json.dumps({"include": include}, separators=(",", ":")))
        return 0
    manifest, error = _load_json(args.manifest)
    problems = [error] if error else manifest_problems(manifest)
    if problems:
        print(f"invalid manifest: {problems}", file=sys.stderr)
        return 1
    report, code = aggregate(manifest, args.artifacts)
    _write(args.output / "report.json", json.dumps(report, indent=2))
    _write(args.output / "report.md", render_markdown(report))
    print(args.output / "report.json")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
