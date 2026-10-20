# Claude Code 向け指示

このリポジトリの作業規則の正本は `AGENTS.md` です。作業を始める前に必ず `AGENTS.md` を読んでください。

必ず守る要点（詳細は `AGENTS.md` と `doc/ワークフロー/ワークフロー.md`）:

1. セッション開始のたびに `git fetch origin main` を実行する。失敗したら古い状態へ変更を重ねず報告する。
2. 着手前にIssueを占有する（他の作業者のPR・ブランチがないことを確認し、Issueへ「着手」コメントを書く）。
3. 1 Issue = 1 worktree = 1 branch（`claude/issue-番号-要約`）= 1 PR。
4. `doc/コーディングルール.md` に必ず従う。
5. PR作成前に再度pullして `origin/main` を取り込み、`finish_task.bat` を通し、差分を自己レビューしてPR本文の「Self-review」欄へ記録する。
6. 必須CIがすべて成功し、未解決のreview threadがなく、自己レビューに未解決の指摘がない場合だけマージする。それ以外はDraftのままにして理由を記録する。
