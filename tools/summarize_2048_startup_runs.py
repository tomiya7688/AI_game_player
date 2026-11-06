"""保存済みの起動診断から、cohortごとに起動時間のp50/p95と失敗率を集計する（Issue #791）。

network無しで、各cohort directory配下の `startup-summary.json` と
`failure-diagnostics.json` だけを読む。symlinkと巨大fileは拒否し、cohortを合算しない。
失敗episodeは失敗率の分子に数え、p50/p95には入れない。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

MAX_FILES = 4096
MAX_FILE_BYTES = 256 * 1024
MIN_SUCCESSES_FOR_COMPARISON = 10
SUMMARY_NAME = "startup-summary.json"
FAILURE_NAME = "failure-diagnostics.json"
CI_KEYS = ("event", "run_id", "run_attempt", "sha")


class CohortError(ValueError):
    """cohortの採用規則に反する入力。集計を拒否する。"""


# {
#   責務: [_percentile: 昇順の値からnearest-rank法でパーセンタイルを返す],
#   引数: [values: 昇順に並べた数値のlist, percent: 0〜100]
#   戻り値: [float | None: 空ならNone]
# }
def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    rank = max(1, -(-len(values) * percent // 100))
    return values[int(rank) - 1]


# {
#   責務: [_read_json: 上限付きで通常fileのJSONを読む],
#   処理: [symlinkと上限超過は拒否し、読めないfileはNoneにして呼出側が件数に数える],
#   引数: [path: 読むfile]
#   戻り値: [dict | None]
# }
def _read_json(path: Path) -> dict | None:
    if path.is_symlink():
        raise CohortError("symlink is not accepted")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CohortError("file exceeds the size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# {
#   責務: [_collect: cohort directory配下の起動診断をepisode directoryごとに集める],
#   引数: [root: cohort directory]
#   戻り値: [dict[Path, dict]: episode dirから summary/failure/unreadable]
#   エラー: [CohortError: rootがsymlink/directoryでない、file数が上限超過、symlinkを含む]
# }
def _collect(root: Path) -> dict[Path, dict]:
    if root.is_symlink() or not root.is_dir():
        raise CohortError("cohort directory must be a real directory")
    episodes: dict[Path, dict] = {}
    seen = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if (Path(current) / name).is_symlink():
                raise CohortError("symlink is not accepted")
        for name in files:
            if name not in (SUMMARY_NAME, FAILURE_NAME):
                continue
            seen += 1
            if seen > MAX_FILES:
                raise CohortError("too many diagnostic files")
            path = Path(current) / name
            entry = episodes.setdefault(
                Path(current), {"summary": None, "failure": None, "unreadable": 0}
            )
            value = _read_json(path)
            if value is None:
                entry["unreadable"] += 1
            else:
                entry["summary" if name == SUMMARY_NAME else "failure"] = value
    return episodes


# {
#   責務: [_context: cohort内のci_contextが全episodeで同一であることを確認する],
#   引数: [name: cohort名, contexts: 各episodeのci_context辞書のlist]
#   戻り値: [dict: 共通のci_context(欠落はNone)]
#   エラー: [CohortError: 異なるci_contextが混ざっている]
# }
def _context(name: str, contexts: list[dict]) -> dict:
    normalized = {
        json.dumps({k: c.get(k) for k in CI_KEYS}, sort_keys=True) for c in contexts
    }
    if len(normalized) > 1:
        raise CohortError(f"cohort {name} mixes different ci_context values")
    if not normalized:
        return dict.fromkeys(CI_KEYS)
    return json.loads(next(iter(normalized)))


# {
#   責務: [summarize_cohort: 1 cohortの起動時間・失敗率・category内訳・要求行なし接続の分布を集計する],
#   処理: [summaryのfirst_board_accepted_msがある試行を成功としp50/p95に入れ、無い試行とfailure-diagnosticsだけの試行を失敗率の分子に数える。evidence_incompleteは別計上する],
#   引数: [name: cohort名, root: cohort directory]
#   戻り値: [dict: cohortの集計。他cohortとは合算しない]
#   エラー: [CohortError: 採用規則に反する入力]
# }
def summarize_cohort(name: str, root: Path) -> dict:
    episodes = _collect(root)
    times: list[float] = []
    failures = 0
    categories: dict[str, int] = {}
    request_less: dict[str, int] = {}
    versions: set[str | None] = set()
    contexts: list[dict] = []
    unreadable = 0
    for entry in episodes.values():
        unreadable += entry["unreadable"]
        summary, failure = entry["summary"], entry["failure"]
        if summary is not None:
            contexts.append(summary.get("ci_context") or {})
            versions.add(summary.get("browser_version"))
            first = summary.get("first_board_accepted_ms")
            if type(first) in (int, float) and first >= 0:
                times.append(float(first))
                connections = summary.get("server_connection_summary") or {}
                count = (connections.get("connections_without_request_line") or 0) + (
                    connections.get("connections_awaiting_request_line") or 0
                )
                key = str(count)
                request_less[key] = request_less.get(key, 0) + 1
            else:
                failures += 1
        elif failure is not None:
            failures += 1
        if failure is not None:
            correlation = failure.get("correlation")
            if isinstance(correlation, dict):
                contexts.append(correlation.get("ci_context") or {})
                classification = correlation.get("classification") or {}
                category = str(classification.get("category", "no_classification"))[:64]
            else:
                category = "no_correlation"
            categories[category] = categories.get(category, 0) + 1
    times.sort()
    context = _context(name, contexts)
    attempt = context.get("run_attempt")
    attempt_unknown = all(value is None for value in context.values())
    retry_only = isinstance(attempt, str) and attempt.isdigit() and int(attempt) > 1
    total = len(episodes)
    return {
        "name": name,
        "episodes": total,
        "successful_startups": len(times),
        "startup_failures": failures,
        "failure_rate": round(failures / total, 4) if total else None,
        "first_board_accepted_ms": {
            "p50": _percentile(times, 50),
            "p95": _percentile(times, 95),
        },
        "failure_categories": dict(sorted(categories.items())),
        "request_less_connections_in_successes": dict(sorted(request_less.items())),
        "browser_versions": sorted(v for v in versions if v is not None),
        "browser_version_unknown": None in versions,
        "ci_context": context,
        "attempt_unknown": attempt_unknown,
        "retry_only": retry_only,
        "unreadable_files": unreadable,
    }


# {
#   責務: [compare_cohorts: 2 cohortを比較してよいか判定し、不可なら理由つきのreference_onlyにする],
#   引数: [first: 1つ目の集計, second: 2つ目の集計]
#   戻り値: [dict: comparison(comparable|reference_only)とreasons]
# }
def compare_cohorts(first: dict, second: dict) -> dict:
    reasons = []
    for cohort in (first, second):
        if cohort["successful_startups"] < MIN_SUCCESSES_FOR_COMPARISON:
            reasons.append(
                f"{cohort['name']}:successes_below_{MIN_SUCCESSES_FOR_COMPARISON}"
            )
        if cohort["attempt_unknown"]:
            reasons.append(f"{cohort['name']}:attempt_unknown")
        if cohort["retry_only"]:
            reasons.append(f"{cohort['name']}:retry_only")
        if len(cohort["browser_versions"]) != 1 or cohort["browser_version_unknown"]:
            reasons.append(f"{cohort['name']}:browser_version_not_single")
    if first["browser_versions"] != second["browser_versions"]:
        reasons.append("browser_version_differs")
    return {
        "between": [first["name"], second["name"]],
        "comparison": "reference_only" if reasons else "comparable",
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort",
        action="append",
        required=True,
        help="name=directory (repeatable; cohorts are never merged)",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    cohorts = []
    names: set[str] = set()
    try:
        for item in args.cohort:
            name, separator, directory = item.partition("=")
            if not separator or not name or not directory or name in names:
                raise CohortError("--cohort needs a unique name=directory")
            names.add(name)
            cohorts.append(summarize_cohort(name, Path(directory)))
        report = {
            "schema": "reference-startup-cohort-summary/v1",
            "cohorts": cohorts,
            "comparisons": [
                compare_cohorts(cohorts[i], cohorts[j])
                for i in range(len(cohorts))
                for j in range(i + 1, len(cohorts))
            ],
        }
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (CohortError, OSError) as error:
        print(f"rejected: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
