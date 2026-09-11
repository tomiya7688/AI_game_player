import json, re, subprocess
from pathlib import Path

REPO = "tomiya7688/AI_game_player"
OUT = Path(".codex/next_issue.md")
RANK = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
DOCS = {
    "spec:recognition": ["doc/画像解析機能説明書.md", "doc/OCR候補検出機能説明書.md", "doc/候補統合機能説明書.md"],
    "spec:capture": ["doc/画面キャプチャ機能説明書.md", "doc/観測入力機能説明書.md"],
    "spec:decision": ["doc/判断パイプライン機能説明書.md", "doc/評価指標機能説明書.md"],
    "spec:execution": ["doc/操作実行機能説明書.md"],
    "spec:persistence": ["doc/設定永続化機能説明書.md", "doc/知識ベース機能説明書.md"],
}

def run():
    data = subprocess.check_output(["gh","issue","list","--repo",REPO,"--state","open","--limit","100","--json","number,title,body,labels,url"], text=True, encoding="utf-8")
    return json.loads(data)

def key(issue):
    labels = {x["name"].lower() for x in issue.get("labels", [])}
    return min([RANK[x] for x in labels if x in RANK] or [50]), issue["number"]

def main():
    issues = [x for x in run() if x["number"] != 19]
    if not issues:
        print("No open work issues.")
        return
    issue = min(issues, key=key)
    labels = [x["name"] for x in issue.get("labels", [])]
    low = {x.lower() for x in labels}
    docs = []
    for label, paths in DOCS.items():
        if label in low:
            docs += paths
    summary = re.sub(r"\s+", " ", issue.get("body") or "").strip()[:900] or "No description provided."
    rank = key(issue)[0]
    priority = f"P{rank}" if rank < 4 else "unlabeled"
    lines = ["# Next Issue", "", f"Issue: #{issue['number']} — {issue['title']}", f"Priority: {priority}", f"Labels: {', '.join(labels) or '(none)'}", f"URL: {issue['url']}", "", "## Compact summary", summary, "", "## Read only if needed"]
    lines += [f"- `{x}`" for x in dict.fromkeys(docs)] or ["- Use the AGENTS.md task router; read only directly relevant files."]
    lines += ["", "## Codex instruction", "Treat this as the current task. Do not scan all issues or documents.", ""]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[{priority}] #{issue['number']} {issue['title']}")
    print(f"Context: {OUT}")

if __name__ == "__main__":
    main()
