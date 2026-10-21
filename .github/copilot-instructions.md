# GitHub Copilot 向け指示

このリポジトリの作業規則の正本は `AGENTS.md` です。作業を始める前に必ず `AGENTS.md` を読んでください。

必ず守る要点（詳細は `AGENTS.md` と `doc/ワークフロー/ワークフロー.md`）:

1. 作業開始のたびに最新の `origin/main` を取得する（`git fetch origin main`）。
2. 着手前に、他の作業者がそのIssueへ着手していないことを確認し、Issueへ「着手」コメントを書く。
3. 1 Issue = 1 branch（`copilot/issue-番号-要約`）= 1 PR。
4. `doc/コーディングルール.md` に必ず従う。
5. PR作成前に `origin/main` を取り込み、テストを通し、差分を自己レビューしてPR本文の「Self-review」欄へ記録する。
6. 必須CIがすべて成功し、未解決のreview threadがなく、自己レビューに未解決の指摘がない場合だけマージする。
