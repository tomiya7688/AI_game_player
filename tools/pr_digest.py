"""PRの状態を短い一覧にする（CI件数・失敗チェック名・bot指摘の見出し・未返信）。

botの概要コメントや長いCIログを丸ごと読まずに、次に何をすべきかを判断するための読み取り専用ツール。
GraphQLは使えない環境（クラウドセッション）があるため、REST（ページ番号を明示）だけを使う。

使い方: python3 tools/pr_digest.py 857 858 [--repo owner/name] [--bots]
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
    output = subprocess.check_output(["gh", *args], text=True, encoding="utf-8", timeout=120)
    return json.loads(output)


def fetch_pages(repo, path, key=None):
    """RESTの一覧を全ページ取得する（--paginateはLinkヘッダ経由でプロキシに拒否されるため明示する）。"""
    joiner = "&" if "?" in path else "?"
    items = []
    for number in range(1, MAX_PAGES + 1):
        page = gh_json(["api", f"repos/{repo}/{path}{joiner}per_page=100&page={number}"])
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
        if not line or line == "---" or line.startswith(("Script executed", "Repository:")):
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
        if comment.get("in_reply_to_id") or not comment["user"]["login"].endswith(BOT_SUFFIX):
            continue
        findings.append({
            "id": comment["id"],
            "where": f"{comment.get('path')}:{comment.get('line') or comment.get('original_line')}",
            "severity": severity(comment["body"]),
            "headline": headline(comment["body"]),
            "answered": comment["id"] in answered,
        })
    return findings


def digest(repo, number):
    """1件のPRの要約行のリストを返す。"""
    pull = gh_json(["api", f"repos/{repo}/pulls/{number}"])
    sha = pull["head"]["sha"]
    runs = fetch_pages(repo, f"commits/{sha}/check-runs", key="check_runs")
    counts, failed = count_checks(runs)
    reviews = fetch_pages(repo, f"pulls/{number}/reviews")
    humans = sorted({f"{r['user']['login']}:{r['state']}" for r in reviews if not r["user"]["login"].endswith(BOT_SUFFIX)})
    lines = [
        f"#{number} {pull['title'][:70]}",
        f"  state={pull['state']} draft={pull['draft']} mergeable={pull.get('mergeable_state')} head={sha[:7]}",
        "  ci=" + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())),
    ]
    if failed:
        lines.append("  failed=" + ", ".join(failed[:10]))
    if humans:
        lines.append("  human_reviews=" + ", ".join(humans))
    return lines, inline_findings(fetch_pages(repo, f"pulls/{number}/comments"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("numbers", type=int, nargs="+", help="PR番号")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--bots", action="store_true", help="botのインライン指摘も一覧する（既定は未返信の件数だけ）")
    args = parser.parse_args(argv)
    for number in args.numbers:
        try:
            lines, findings = digest(args.repo, number)
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            print(f"#{number} ERROR: {error}")
            continue
        print("\n".join(lines))
        open_findings = [f for f in findings if not f["answered"]]  # 返信が無いもの（修正済みかは別）
        print(f"  bot_findings={len(findings)} no_reply={len(open_findings)}")
        for finding in (findings if args.bots else []):
            mark = " " if finding["answered"] else "!"
            print(f"   {mark} {finding['severity']:8} {finding['where']} (id {finding['id']}) {finding['headline']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
