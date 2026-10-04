"""Metadata-first task routing; generated packs are pointers, not specifications."""

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO = "tomiya7688/AI_game_player"
OUT = Path(".codex/next_issue.md")
RANK = {f"p{i}": i for i in range(6)}
QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $endCursor) {
      nodes { number title url labels(first: 100) { nodes { name } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
DOCS = {
    "spec:recognition": ["doc/画像解析機能説明書.md", "doc/OCR候補検出機能説明書.md", "doc/候補統合機能説明書.md"],
    "spec:capture": ["doc/画面キャプチャ機能説明書.md", "doc/観測入力機能説明書.md"],
    "spec:decision": ["doc/判断パイプライン機能説明書.md", "doc/評価指標機能説明書.md"],
    "spec:execution": ["doc/操作実行機能説明書.md"],
    "spec:persistence": ["doc/設定永続化機能説明書.md", "doc/知識ベース機能説明書.md"],
}

def gh_json(args):
    result = subprocess.check_output(
        ["gh", *args], text=True, encoding="utf-8", timeout=120
    )
    return json.loads(result)


def fetch_issues():
    """Paginate metadata only, including older Issues beyond the first 100."""
    owner, name = REPO.split("/")
    pages = gh_json([
        "api", "graphql", "--paginate", "--slurp", "-f", f"query={QUERY}",
        "-f", f"owner={owner}", "-f", f"name={name}",
    ])
    issues = []
    for page in pages:
        if page.get("errors"):
            raise ValueError("GitHub returned GraphQL errors; metadata is incomplete.")
        connection = page["data"]["repository"]["issues"]
        for node in connection["nodes"]:
            issues.append({**node, "labels": node["labels"]["nodes"]})
    if pages and connection["pageInfo"]["hasNextPage"]:
        raise ValueError("GitHub pagination is incomplete; refusing partial selection.")
    return issues


def fetch_issue(number):
    """Fetch requirements for exactly one Issue, without loading discussion."""
    issue = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if issue.get("pull_request") is not None or issue["state"] != "open":
        raise ValueError("The selected item must be an open Issue, not a PR.")
    return {**issue, "url": issue["html_url"]}

def key(issue):
    labels = {x["name"].lower() for x in issue.get("labels", [])}
    return min([RANK[x] for x in labels if x in RANK] or [50]), issue["number"]

def select_issue(issues):
    work = [
        issue for issue in issues
        if issue["number"] != 19
        and not re.search(r"\bparent\b|親Issue", issue["title"], re.IGNORECASE)
        and not any(x["name"].lower() == "parent" for x in issue.get("labels", []))
    ]
    return min(work, key=key) if work else None


def render_context(issue, max_chars=8000):
    labels = [x["name"] for x in issue.get("labels", [])]
    low = {x.lower() for x in labels}
    docs = []
    for label, paths in DOCS.items():
        if label in low:
            docs += paths
    rank = key(issue)[0]
    priority = f"P{rank}" if rank < 6 else "unlabeled"
    prefix = "\n".join([
        "# Next Issue",
        "",
        f"Issue: #{issue['number']} — {issue['title']}",
        f"Priority: {priority}",
        f"Labels: {', '.join(labels) or '(none)'}",
        f"Source of Truth: {issue['url']}",
        "",
        "## Requirements excerpt (not a replacement for the Issue)",
        "",
    ])
    suffix = "\n".join([
        "", "", "## Read only if needed",
        *([f"- `{x}`" for x in dict.fromkeys(docs)] or ["- Use the AGENTS.md task router for source/tests."]),
        "",
        "## Evidence / stop conditions",
        "- Discussion is not loaded. Read relevant comments at the Issue URL when requirements are unclear.",
        "- Determine Goal / Required / Acceptance, then stop broad exploration.",
        "- Missing or truncated requirements must be checked at the original Issue before implementation.",
        "- Search first; read target source/tests, then direct dependencies only as needed.",
        "- Use targeted tests while implementing; finish_task.bat remains mandatory before commit.",
        "- Report unexecuted runtime/GUI checks as Unverified.",
        "",
    ])
    marker = "\n\n[TRUNCATED: read the original Issue for omitted requirements.]"
    body = (issue.get("body") or "No description provided; clarify requirements before implementation.").strip()
    budget = max_chars - len(prefix) - len(suffix)
    if budget < len(marker):
        raise ValueError("Context limit cannot fit the required pointers and instructions.")
    if len(body) > budget:
        body = body[:budget - len(marker)] + marker
    return prefix + body + suffix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, help="Explicit Issue; bypass metadata listing (including parent Issues).")
    parser.add_argument("--max-chars", type=int, default=8000, help="Pack character limit (2000-16000, not a token count).")
    args = parser.parse_args(argv)
    if args.issue is not None and args.issue <= 0:
        parser.error("--issue must be positive")
    if not 2000 <= args.max_chars <= 16000:
        parser.error("--max-chars must be between 2000 and 16000")
    try:
        selected = {"number": args.issue} if args.issue else select_issue(fetch_issues())
        if selected is None:
            raise ValueError("No open work Issues. Previous context, if any, must not be reused.")
        issue = fetch_issue(selected["number"])
        content = render_context(issue, args.max_chars)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(content, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"[ERROR] Task context was not refreshed: {exc}")
        print("Do not use a previous .codex/next_issue.md after this failure.")
        return 1
    rank = key(issue)[0]
    priority = f"P{rank}" if rank < 6 else "unlabeled"
    print(f"[{priority}] #{issue['number']} {issue['title']}")
    print(f"Context: {OUT} ({len(content)} characters; one Issue body)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
