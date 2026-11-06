"""Windows長枠比較(#795)の純粋な集計・統計関数。標準ライブラリだけでLinuxでも検証できる。"""

from __future__ import annotations

from itertools import product
import math
import random
import statistics
from typing import Any

# 統計の事前固定値。CLI・環境変数・関数引数から変更できない(変更はコードと設計書のレビューを要する)。
N_MIN_VERDICT = 8
FAMILY_ALPHA = 0.05
CONTRAST_COUNT = 3
BOOTSTRAP_RESAMPLES = 10000
RANDOM_SEED = 795
EXACT_SIGN_FLIP_MAX_N = 16
MONTE_CARLO_SIGN_FLIPS = 20000
# 3対比のBonferroni補正後の有意水準(信頼水準は1-この値)。
CORRECTED_ALPHA = FAMILY_ALPHA / CONTRAST_COUNT
WILSON_Z = 1.959964
SCORE_MIN_CONFIDENCE = 0.65

MEASURED = "measured"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
METRIC_STATUSES = (MEASURED, UNKNOWN, NOT_APPLICABLE)


# {
#   責務: [
#     measured: 数値を取得済みの指標値を作る
#   ]
#   処理: [
#     1: bool・非有限数を拒否する
#     2: statusをmeasuredにして返す
#   ]
#   引数: [
#     value: 実測した有限数
#   ]
#   戻り値: [
#     dict: value/statusを持つ指標値
#   ]
#   エラー: [
#     数値でない場合ValueError
#   ]
# }
def measured(value: float) -> dict[str, Any]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("measured metric requires a finite number")
    return {"value": value, "status": MEASURED}


# {
#   責務: [
#     unknown: 未取得の指標値を作る(0へ置換しない)
#   ]
#   処理: [
#     1: valueをnull、statusをunknownにして返す
#   ]
#   引数: []
#   戻り値: [
#     dict: value=null,status=unknown
#   ]
# }
def unknown() -> dict[str, Any]:
    return {"value": None, "status": UNKNOWN}


# {
#   責務: [
#     not_applicable: その方式に存在しない指標値を作る
#   ]
#   処理: [
#     1: valueをnull、statusをnot_applicableにして返す
#   ]
#   引数: []
#   戻り値: [
#     dict: value=null,status=not_applicable
#   ]
# }
def not_applicable() -> dict[str, Any]:
    return {"value": None, "status": NOT_APPLICABLE}


# {
#   責務: [
#     validate_metric: 指標値の「nullならunknown/not_applicable、measuredなら数値必須」を検査する
#   ]
#   処理: [
#     1: 形とstatusの値を確認する
#     2: valueとstatusの整合を確認する
#   ]
#   引数: [
#     metric: 検査対象
#   ]
#   戻り値: [
#     list: 違反理由の文字列(空なら合格)
#   ]
# }
def validate_metric(metric: Any) -> list[str]:
    if not isinstance(metric, dict) or set(metric) != {"value", "status"}:
        return ["metric must be an object with value and status only"]
    status, value = metric["status"], metric["value"]
    if status not in METRIC_STATUSES:
        return [f"invalid metric status: {status!r}"]
    if status == MEASURED:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return ["measured metric requires a finite number"]
        return []
    if value is not None:
        return [f"{status} metric must have null value"]
    return []


# {
#   責務: [
#     percentile_nearest_rank: nearest-rank方式のpercentileを返す
#   ]
#   処理: [
#     1: 空なら未計測としてNoneを返す
#     2: ceil(q*n)番目の標本を返す
#   ]
#   引数: [
#     values: 標本
#     q: 0〜1の分位
#   ]
#   戻り値: [
#     float|None: 分位値。標本0ならNone
#   ]
# }
def percentile_nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# {
#   責務: [
#     wilson_interval: 成功率のWilson区間を返す
#   ]
#   処理: [
#     1: n=0なら区間なしとして返す
#     2: Wilson score区間(z=1.96)を計算する
#   ]
#   引数: [
#     successes: 成功数
#     n: 母数
#   ]
#   戻り値: [
#     dict: rate/lower/upper(n=0はすべてnull)
#   ]
# }
def wilson_interval(successes: int, n: int) -> dict[str, Any]:
    if n > 0 and not 0 <= successes <= n:
        raise ValueError("successes must be within 0..n")
    if n <= 0:
        return {
            "n": 0,
            "successes": successes,
            "rate": None,
            "lower": None,
            "upper": None,
        }
    p = successes / n
    z2 = WILSON_Z**2
    denominator = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denominator
    margin = WILSON_Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return {
        "n": n,
        "successes": successes,
        "rate": p,
        "lower": max(0.0, center - margin),
        "upper": min(1.0, center + margin),
    }


# {
#   責務: [
#     describe: 値列の記述統計(個数・中央値・四分位・最小/最大)を返す
#   ]
#   処理: [
#     1: 空ならn=0のunknownを返す
#     2: statistics.quantilesで四分位を求める
#   ]
#   引数: [
#     values: 数値列
#   ]
#   戻り値: [
#     dict: n/median/q1/q3/min/max
#   ]
# }
def describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "n": 0,
            "median": None,
            "q1": None,
            "q3": None,
            "min": None,
            "max": None,
        }
    ordered = sorted(values)
    if len(ordered) >= 2:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    else:
        q1 = q3 = ordered[0]
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "q1": q1,
        "q3": q3,
        "min": ordered[0],
        "max": ordered[-1],
    }


# {
#   責務: [
#     bootstrap_mean_interval: 対応あり差の平均のpercentile bootstrap区間を決定的に返す
#   ]
#   処理: [
#     1: random.Random(RANDOM_SEED)で10000回復元抽出する
#     2: 補正後水準の両側percentileを取る
#   ]
#   引数: [
#     differences: 対応あり差
#   ]
#   戻り値: [
#     tuple: (lower, upper)
#   ]
# }
def bootstrap_mean_interval(differences: list[float]) -> tuple[float, float]:
    rng = random.Random(RANDOM_SEED)
    n = len(differences)
    means = sorted(
        sum(rng.choices(differences, k=n)) / n for _ in range(BOOTSTRAP_RESAMPLES)
    )
    low = means[int(math.floor((CORRECTED_ALPHA / 2) * (BOOTSTRAP_RESAMPLES - 1)))]
    high = means[int(math.ceil((1 - CORRECTED_ALPHA / 2) * (BOOTSTRAP_RESAMPLES - 1)))]
    return low, high


# {
#   責務: [
#     sign_flip_p_value: 平均差の両側符号反転検定のp値を返す
#   ]
#   処理: [
#     1: n<=16は全符号列を列挙する
#     2: 超過時は決定的乱数のMonte Carloで反転する
#     3: 観測平均の絶対値以上となる割合を返す
#   ]
#   引数: [
#     differences: 対応あり差
#   ]
#   戻り値: [
#     float: p値(0〜1)
#   ]
# }
def sign_flip_p_value(differences: list[float]) -> float:
    n = len(differences)
    observed = abs(sum(differences))
    tolerance = 1e-12 * max(1.0, observed)
    if n <= EXACT_SIGN_FLIP_MAX_N:
        total = hits = 0
        for signs in product((1, -1), repeat=n):
            total += 1
            if (
                abs(sum(s * d for s, d in zip(signs, differences)))
                >= observed - tolerance
            ):
                hits += 1
        return hits / total
    rng = random.Random(RANDOM_SEED)
    hits = 0
    for _ in range(MONTE_CARLO_SIGN_FLIPS):
        value = abs(sum(d if rng.random() < 0.5 else -d for d in differences))
        if value >= observed - tolerance:
            hits += 1
    # 観測自身を1件含めて0にならないようにする(Phipson-Smyth)。
    return (hits + 1) / (MONTE_CARLO_SIGN_FLIPS + 1)


# {
#   責務: [
#     paired_contrast: 対応あり差の平均・区間・p値・勝敗・verdictを作る
#   ]
#   処理: [
#     1: 差の個数と勝/分/負を数える
#     2: n未満なら区間なし(insufficient_samples)にする
#     3: 区間と符号反転p値を計算する
#     4: verdict対象なら区間が0を含まずp<=補正水準の時だけbetter/worseにする
#   ]
#   引数: [
#     differences: 対応あり差(前者-後者)。unknownは呼出側で除外済み
#     n_excluded_unknown: unknownのため除外した対の数
#     with_verdict: Falseなら探索的指標としてverdictを付けない
#   ]
#   戻り値: [
#     dict: 対比の統計
#   ]
# }
def paired_contrast(
    differences: list[float], n_excluded_unknown: int = 0, with_verdict: bool = True
) -> dict[str, Any]:
    n = len(differences)
    result: dict[str, Any] = {
        "n_pairs": n,
        "n_excluded_unknown": n_excluded_unknown,
        "wins": sum(1 for d in differences if d > 0),
        "ties": sum(1 for d in differences if d == 0),
        "losses": sum(1 for d in differences if d < 0),
        "mean_difference": statistics.fmean(differences) if n else None,
        "median_difference": statistics.median(differences) if n else None,
        "confidence_level": 1 - CORRECTED_ALPHA,
        "interval": None,
        "p_value": None,
        "status": "insufficient_samples",
    }
    if with_verdict:
        result["verdict"] = "inconclusive"
    if n < N_MIN_VERDICT:
        return result
    low, high = bootstrap_mean_interval(differences)
    p_value = sign_flip_p_value(differences)
    result.update({"interval": [low, high], "p_value": p_value, "status": "evaluated"})
    if with_verdict and (low > 0 or high < 0) and p_value <= CORRECTED_ALPHA:
        result["verdict"] = "better" if low > 0 else "worse"
    return result


# {
#   責務: [
#     episode_score: OCR確認済みscoreだけを返し、未確認はNoneにする
#   ]
#   処理: [
#     1: score_evaluationのvalue(int)とconfidence(0.65〜1)を検査する
#   ]
#   引数: [
#     result: run_episodeの結果
#   ]
#   戻り値: [
#     int|None: 確認済みscore
#   ]
# }
def episode_score(result: dict[str, Any]) -> int | None:
    evaluation = result.get("score_evaluation") or {}
    value, confidence = evaluation.get("value"), evaluation.get("confidence")
    if (
        type(value) is int
        and type(confidence) in (int, float)
        and SCORE_MIN_CONFIDENCE <= confidence <= 1
    ):
        return value
    return None


# {
#   責務: [
#     episode_record: run_episodeの結果を長枠reportのepisode記録へ畳む
#   ]
#   処理: [
#     1: 数値指標を抽出し、未取得はNoneのまま残す
#     2: nodesはcounters受領がある時だけ数値にする
#   ]
#   引数: [
#     index: episode番号(1始まり)
#     seed: このepisodeのseed
#     result: run_episodeの結果
#   ]
#   戻り値: [
#     dict: episode記録
#   ]
# }
def episode_record(index: int, seed: int, result: dict[str, Any]) -> dict[str, Any]:
    records = result.get("attempt_records") or []
    counters_received = any(r.get("counters") is not None for r in records)
    failure = result.get("failure")
    return {
        "episode": index,
        "seed": seed,
        "episode_status": "completed" if result.get("success") else "failed",
        "terminal": result.get("terminal")
        if isinstance(result.get("terminal"), bool)
        else None,
        "stop_reason": result.get("stop_reason"),
        "actions": result.get("actions", 0),
        "attempts": result.get("attempts", 0),
        "holds": result.get("holds", 0),
        "invalid_no_op_actions": result.get("invalid_actions", 0),
        "board_changes": result.get("board_changes", 0),
        "policy_reobservations": result.get("policy_reobservations", 0),
        "nodes": result.get("nodes") if counters_received else None,
        "score": episode_score(result),
        "max_tile": result.get("max_tile")
        if type(result.get("max_tile")) is int
        else None,
        "latency_ms": list((result.get("latency_ms") or {}).get("samples") or []),
        "resources": result.get("resources"),
        "wall_seconds": result.get("wall_seconds"),
        "seed_fixture": result.get("seed_fixture"),
        "planner_readiness": result.get("planner_readiness"),
        "failure_stage": result.get("failure_stage"),
        "failure": failure,
        "cleanup_failure": result.get("cleanup_failure")
        or result.get("journal_cleanup_failure"),
        "resource_failure": result.get("resource_failure"),
        "stopped_input_rejected": bool(result.get("stopped_input_rejected")),
        "stale_generation_rejected": bool(result.get("stale_generation_rejected")),
    }


# {
#   責務: [
#     _rate: 分母が正の時だけ率をmeasuredにする
#   ]
#   処理: [
#     1: 分母0はunknownにする
#   ]
#   引数: [
#     numerator: 分子
#     denominator: 分母
#   ]
#   戻り値: [
#     dict: 指標値
#   ]
# }
def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return measured(numerator / denominator) if denominator > 0 else unknown()


# {
#   責務: [
#     _resources: episodeのCPU秒合計とRSSピークを指標値にする
#   ]
#   処理: [
#     1: 全episodeの標本が揃うか確認する
#     2: 1件でも欠測・samplingエラーならunknownにする
#   ]
#   引数: [
#     episodes: episode記録の列
#   ]
#   戻り値: [
#     tuple: (cpu_seconds, peak_rss_bytes)
#   ]
# }
def _resources(episodes: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    cpu_total, peak = 0.0, 0
    for episode in episodes:
        resource = episode.get("resources")
        if (
            not isinstance(resource, dict)
            or resource.get("sampling_error")
            or not isinstance(resource.get("parent_cpu_seconds"), (int, float))
            or not isinstance(
                resource.get("child_cpu_seconds_sampled_lower_bound"), (int, float)
            )
            or not isinstance(
                resource.get("process_tree_peak_rss_bytes_sampled"), (int, float)
            )
        ):
            return unknown(), unknown()
        cpu_total += (
            resource["parent_cpu_seconds"]
            + resource["child_cpu_seconds_sampled_lower_bound"]
        )
        peak = max(peak, resource["process_tree_peak_rss_bytes_sampled"])
    if not episodes:
        return unknown(), unknown()
    return measured(cpu_total), measured(peak)


# {
#   責務: [
#     run_metrics: 1 provider×1 base seedのepisode記録から比較指標を集計する
#   ]
#   処理: [
#     1: score・max tile・survival(右打切りを分離)を集計する
#     2: 率・latency・CPU/RAM・nodesを集計し、未計測はunknownにする
#   ]
#   引数: [
#     provider: cycling/baseline/planner
#     episodes: episode_recordの列
#   ]
#   戻り値: [
#     dict: 指標名→{value,status}と補助カウント
#   ]
# }
def run_metrics(provider: str, episodes: list[dict[str, Any]]) -> dict[str, Any]:
    actions = sum(e["actions"] for e in episodes)
    attempts = sum(e["attempts"] for e in episodes)
    scores = [e["score"] for e in episodes if e["score"] is not None]
    tiles = [e["max_tile"] for e in episodes if e["max_tile"] is not None]
    terminal = [e for e in episodes if e["terminal"] is True]
    latencies = [x for e in episodes for x in e["latency_ms"]]
    cpu, rss = _resources(episodes)
    if provider == "planner":
        nodes_values = [e["nodes"] for e in episodes if e["nodes"] is not None]
        nodes = measured(sum(nodes_values)) if nodes_values else unknown()
        reobserve = measured(sum(e["policy_reobservations"] for e in episodes))
        reobserve_rate = _rate(
            sum(e["policy_reobservations"] for e in episodes), actions
        )
    else:
        nodes = reobserve = reobserve_rate = not_applicable()
    p50 = percentile_nearest_rank(latencies, 0.5)
    p95 = percentile_nearest_rank(latencies, 0.95)
    return {
        "episode_mean_score": measured(statistics.fmean(scores))
        if scores
        else unknown(),
        "score_episodes_measured": len(scores),
        "best_episode_score": measured(max(scores)) if scores else unknown(),
        "max_tile": measured(max(tiles)) if tiles else unknown(),
        "survival_median_actions": (
            measured(statistics.median(e["actions"] for e in terminal))
            if terminal
            else unknown()
        ),
        "terminal_episodes": len(terminal),
        # 右打切りは「時間切れでterminal未到達(False)」だけ。失敗等でterminalが不明(None)なものは別に数える。
        "censored_episodes": sum(1 for e in episodes if e["terminal"] is False),
        "terminal_unknown_episodes": sum(1 for e in episodes if e["terminal"] is None),
        "episodes": len(episodes),
        "actions_total": actions,
        "attempts_total": attempts,
        "invalid_no_op_count": measured(
            sum(e["invalid_no_op_actions"] for e in episodes)
        ),
        "invalid_no_op_rate": _rate(
            sum(e["invalid_no_op_actions"] for e in episodes), actions
        ),
        "abstain_count": measured(sum(e["holds"] for e in episodes)),
        "abstain_rate": _rate(sum(e["holds"] for e in episodes), attempts),
        "reobserve_count": reobserve,
        "reobserve_rate": reobserve_rate,
        "latency_p50_ms": measured(p50) if p50 is not None else unknown(),
        "latency_p95_ms": measured(p95) if p95 is not None else unknown(),
        "latency_samples": len(latencies),
        "cpu_seconds": cpu,
        "peak_rss_bytes": rss,
        "nodes": nodes,
        "copy_io": unknown(),
    }


# 致命扱いにするepisode失敗段階(Stop/旧世代probeの失敗は成功に数えない)。
FATAL_FAILURE_STAGES = frozenset({"stop_probe", "stale_generation_probe"})
# SafetyGuardが実行を拒否した時のRuntimeError文言の先頭(action_executor.execute)。
SAFETY_BLOCK_PREFIX = "SafetyGuard blocked action"


# {
#   責務: [
#     fatal_reason: episode記録が致命(run即時failed)に当たるかを判定する
#   ]
#   処理: [
#     1: Stop/旧世代probe失敗、cleanup失敗、SafetyGuard拒否を致命にする
#   ]
#   引数: [
#     record: episode記録
#   ]
#   戻り値: [
#     str|None: "fatal:<理由>"。致命でなければNone
#   ]
# }
def fatal_reason(record: dict[str, Any]) -> str | None:
    if record.get("failure_stage") in FATAL_FAILURE_STAGES:
        return "fatal:" + str(record["failure_stage"])
    if record.get("cleanup_failure"):
        return "fatal:cleanup"
    message = str((record.get("failure") or {}).get("message") or "")
    if message.startswith(SAFETY_BLOCK_PREFIX):
        return "fatal:safety_guard"
    return None


# {
#   責務: [
#     classify_run: episode記録とstop_reasonからrun_status/stop_reasonを状態値で判定する
#   ]
#   処理: [
#     1: 入力0・致命・recovery枯渇・hard timeoutをfailedにする
#     2: episode失敗を含めば partial、無ければ completed にする
#   ]
#   引数: [
#     episodes: episode記録
#     stop_reason: 実行側の停止理由
#   ]
#   戻り値: [
#     tuple: (run_status, stop_reason)
#   ]
# }
def classify_run(episodes: list[dict[str, Any]], stop_reason: str) -> tuple[str, str]:
    if stop_reason in ("recovery_exhausted", "hard_timeout") or stop_reason.startswith(
        "fatal:"
    ):
        return "failed", stop_reason
    for episode in episodes:
        fatal = fatal_reason(episode)
        if fatal is not None:
            return "failed", fatal
    if sum(e["actions"] for e in episodes) == 0:
        return "failed", stop_reason
    if any(e["episode_status"] != "completed" for e in episodes):
        return "partial", stop_reason
    return "completed", stop_reason
