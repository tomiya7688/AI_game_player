"""PRの状態を短い一覧にする（CI件数・失敗チェック名・bot指摘の見出し・未返信）。

botの概要コメントや長いCIログを丸ごと読まずに、次に何をすべきかを判断するためのツール。既定は読み取りだけで、`--rerun-failed`を付けた時だけ失敗ジョブの再実行(書込み)を行う。
GraphQLは使えない環境（クラウドセッション）があるため、REST（ページ番号を明示）だけを使う。

使い方: python3 tools/pr_digest.py 857 858 [--repo owner/name] [--bots] [--rerun-failed]

--rerun-failed: head SHAの全workflow runが完了していて、失敗したrunがあり、まだ再実行していない(run_attempt=1)場合だけ、
失敗ジョブを1回再実行する。実行中のrunがある間、既に再実行したrun、失敗が無い場合は何もしない(再実行は最大1回の規則)。

再実行の対象は conclusion が failure または timed_out の run(action_required は人の承認待ちで再実行できないので表示だけ)。
限界: 再実行の直後にGitHubがrunの状態を更新するまでの短い遅れの間は「1回だけ」を保証しきれない。ただし再実行すると
runは待機状態になるため、実行中のrunがある間は何もしない条件で実害は小さい。

限界: レビュースレッドの解決状態(is_resolved)はGraphQLでしか取れないため、この要約には出さない。
スレッドの確認と解決は、GitHub MCPのpull_request_read(get_review_comments)とresolve_review_thread、またはWebで行う。
"""

import argparse
import json
import re
import subprocess
import sys

DEFAULT_REPO = "tomiya7688/AI_game_player"
MAX_PAGES = 10
BOT_SUFFIX = "[bot]"
SEVERITY = re.compile(r"\b(Critical|Major|Minor|Trivial|Nitpick)\b")
HEADLINE_LIMIT = 90


def gh_json(args):
    """gh apiの結果をJSONで返す。失敗はCalledProcessErrorのまま呼出側へ伝える。"""
    output = subprocess.check_output(
        ["gh", *args], text=True, encoding="utf-8", timeout=120
    )
    return json.loads(output)


def fetch_pages(repo, path, key=None):
    """RESTの一覧を全ページ取得する（--paginateはLinkヘッダ経由でプロキシに拒否されるため明示する）。"""
    joiner = "&" if "?" in path else "?"
    items = []
    for number in range(1, MAX_PAGES + 1):
        page = gh_json(
            ["api", f"repos/{repo}/{path}{joiner}per_page=100&page={number}"]
        )
        page = page[key] if key else page
        items.extend(page)
        if len(page) < 100:
            return items
    raise ValueError(f"pagination is incomplete: {path}")


def count_checks(check_runs):
    """check-runsを結論ごとに数え、失敗したチェック名を返す。"""
    counts = {}
    failed = []
    for run in check_runs:
        state = run.get("conclusion") or run.get("status") or "unknown"
        counts[state] = counts.get(state, 0) + 1
        if state in ("failure", "timed_out", "action_required"):
            failed.append(run["name"])
    return counts, failed


def headline(body):
    """botのインライン指摘から、解析ログを除いた結論の見出しを1行で取り出す。"""
    text = re.sub(r"<[^>]*>", "", body or "")
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # 解析ログ（Script executed / Length of output）の後ろが結論になる。
    text = re.split(r"Length of output: \d+", text)[-1]
    # 結論は太字だけの行（**…**）になっている。解析ログに太字は出ないので、あればそれを優先する。
    bold = re.findall(r"^\*\*([^*\n]{3,})\*\*\s*$", text, flags=re.MULTILINE)
    if bold:
        return bold[0][:HEADLINE_LIMIT]
    for line in text.splitlines():
        line = line.strip(" *-#>\t")
        if (
            not line
            or line == "---"
            or line.startswith(("Script executed", "Repository:"))
        ):
            continue
        return line[:HEADLINE_LIMIT]
    return "(no headline)"


def severity(body):
    found = SEVERITY.search(re.sub(r"<[^>]*>", "", body or ""))
    return found.group(1) if found else "-"


def inline_findings(comments):
    """botのルートコメントを返す。人の返信（in_reply_to）があるものは answered=True。"""
    answered = {
        c["in_reply_to_id"]
        for c in comments
        if c.get("in_reply_to_id") and not c["user"]["login"].endswith(BOT_SUFFIX)
    }
    findings = []
    for comment in comments:
        if comment.get("in_reply_to_id") or not comment["user"]["login"].endswith(
            BOT_SUFFIX
        ):
            continue
        findings.append(
            {
                "id": comment["id"],
                "where": f"{comment.get('path')}:{comment.get('line') or comment.get('original_line')}",
                "severity": severity(comment["body"]),
                "headline": headline(comment["body"]),
                "answered": comment["id"] in answered,
            }
        )
    return findings


def workflow_runs(repo, sha):
    """head SHAのworkflow runを全ページ取得する。"""
    return fetch_pages(repo, f"actions/runs?head_sha={sha}", key="workflow_runs")


RERUNNABLE_CONCLUSIONS = ("failure", "timed_out")


def plan_rerun(runs):
    """runの一覧から再実行の計画を決める純粋関数。(再実行するrunのlist, 何もしない理由の説明行)を返す。"""
    if any(run["status"] != "completed" for run in runs):
        return [], "skipped (workflow runs still in progress)"
    failed = [r for r in runs if r["conclusion"] in RERUNNABLE_CONCLUSIONS]
    targets = [r for r in failed if r.get("run_attempt", 1) == 1]
    if targets:
        return targets, ""
    if failed:
        return [], "skipped (already re-run once: " + ", ".join(
            r["name"] for r in failed
        ) + ")"
    return [], "nothing to re-run"


def post_rerun(repo, run_id):
    """1つのworkflow runの失敗ジョブを再実行する(書込み)。"""
    subprocess.check_output(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
        ],
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def rerun_failed(repo, sha):
    """計画に従って失敗ジョブを再実行する。(説明行のlist, 全て成功したか)を返し、途中の失敗でも実行済みの結果を残す。"""
    targets, reason = plan_rerun(workflow_runs(repo, sha))
    if not targets:
        return [f"  rerun: {reason}"], True
    lines = []
    ok = True
    for run in targets:
        try:
            post_rerun(repo, run["id"])
            lines.append(f"  rerun: requested {run['name']} (run {run['id']})")
        except subprocess.SubprocessError as error:
            ok = False
            lines.append(f"  rerun: ERROR {run['name']} (run {run['id']}): {error}")
    return lines, ok


def digest(repo, number):
    """1件のPRの要約行のリストを返す。"""
    pull = gh_json(["api", f"repos/{repo}/pulls/{number}"])
    sha = pull["head"]["sha"]
    runs = fetch_pages(repo, f"commits/{sha}/check-runs", key="check_runs")
    counts, failed = count_checks(runs)
    reviews = fetch_pages(repo, f"pulls/{number}/reviews")
    humans = sorted(
        {
            f"{r['user']['login']}:{r['state']}"
            for r in reviews
            if not r["user"]["login"].endswith(BOT_SUFFIX)
        }
    )
    lines = [
        f"#{number} {pull['title'][:70]}",
        f"  state={pull['state']} draft={pull['draft']} mergeable={pull.get('mergeable_state')} head={sha[:7]}",
        "  ci=" + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())),
    ]
    if failed:
        lines.append("  failed=" + ", ".join(failed[:10]))
    if humans:
        lines.append("  human_reviews=" + ", ".join(humans))
    return lines, inline_findings(fetch_pages(repo, f"pulls/{number}/comments")), sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("numbers", type=int, nargs="+", help="PR番号")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="全runが完了し失敗かつ未再実行の時だけ、失敗ジョブを1回再実行する",
    )
    parser.add_argument(
        "--bots",
        action="store_true",
        help="botのインライン指摘も一覧する（既定は未返信の件数だけ）",
    )
    args = parser.parse_args(argv)
    exit_code = 0
    for number in args.numbers:
        try:
            lines, findings, sha = digest(args.repo, number)
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            print(f"#{number} ERROR: {error}")
            exit_code = 1
            continue
        print("\n".join(lines))
        if args.rerun_failed:
            try:
                rerun_lines, rerun_ok = rerun_failed(args.repo, sha)
            except (subprocess.SubprocessError, ValueError, KeyError) as error:
                rerun_lines, rerun_ok = [f"  rerun: ERROR {error}"], False
            print("\n".join(rerun_lines))
            exit_code = exit_code or (0 if rerun_ok else 1)
        no_reply = [
            f for f in findings if not f["answered"]
        ]  # 返信が無いもの（修正済みかは別）
        print(f"  bot_findings={len(findings)} no_reply={len(no_reply)}")
        shown = findings if args.bots else []
        for finding in shown:
            mark = " " if finding["answered"] else "!"
            print(
                f"   {mark} {finding['severity']:8} {finding['where']} (id {finding['id']}) {finding['headline']}"
            )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
