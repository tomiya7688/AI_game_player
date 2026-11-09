---
name: ai-game-player-issue-loop
description: AI_GAME_Player（tomiya7688/AI_game_player）でGitHub Issueを順番に「選択→設計書（実装Packet形式）→設計レビュー→実装→評価→文書更新→レビュー→PR→CI→マージ」まで反復処理する手順。複数Issueをサブエージェントで並列に進める時の依頼文テンプレートと、主担当のマージ前照合を含む。
---

> **クラウド環境での注意**: 本手順はローカルPC(Windows)向けの `E:/` パスやレビューCLI・共通キューを含みます。クラウドセッションでは、それらのローカル専用手順は使わず、このリポジトリの `AGENTS.md` と `.github/workflows` を正とします。Issueの選択順・設計→実装→検証→PRの流れ・落とし穴の部分のみ参照してください。

# AI_GAME_Player Issue反復ループ

> **探す前に読む**: コードは `generated/module_index.md`（目次・責務表・依存図）、文書は `generated/doc_index.md`（題名と先頭の1文）。全探索をしない。手書き図の前は `python3 tools/diagram_hints.py <doc.md>`。 PRの状況は `python3 tools/pr_digest.py <番号>`（CI件数・失敗チェック・botの指摘・未返信。`--rerun-failed`で失敗ジョブを全run完了後に1回だけ再実行）。

正本は各worktreeの `AGENTS.md` / `AI_CONTEXT.md` / `doc/コーディングルール.md` と `E:/projects/REVIEW_OPERATIONS.md`。本手順は順序と落とし穴の要約で、矛盾したら正本が優先。

## 0. 開始時（毎回）

```powershell
git -C E:/projects/AI_GAME_Player fetch -q origin
git -C E:/projects/AI_GAME_Player worktree list   # C: のパスがあれば報告
```

- `E:/projects/AI_GAME_Player` 本体は別ブランチ＋他者のdirty変更があるため**触らない**。作業・調査は必ずworktreeで。
- 選択: 一時worktree（`--detach origin/main`）で `python tools/issue_context.py` を実行するか、`issue_context.key` で並べる。順位は P0→P5→ラベルなし、#19と親Issue（title/label に Parent / 親Issue）は除外。
- 既に他者のブランチ（`git branch -r | Select-String <番号>`）や直近の進捗コメントがあるIssue、リポジトリ設定変更が必要なIssue（例 #92 ruleset、ユーザー許可が必要）は飛ばして理由を報告。
- 完了済みでownerの判断待ちのIssue（例 #513）は開いたまま次へ。

## 1. worktree

```powershell
git -C E:/projects/AI_GAME_Player worktree add -b claude/issue-<N>-<topic> E:/projects/worktrees/issue-<N>-<topic>/AI_GAME_Player origin/main
cd E:/projects/worktrees/issue-<N>-<topic>/AI_GAME_Player
python tools/issue_context.py --issue <N>
gh issue view <N> --comments
```

scratchは `E:/program_files/soft/claude/tmp`。C: に作らない。

## 2. 設計書を先に書く（実装前・必須）

コードを書く前に、Issueから設計書を作り、設計レビューを通してから実装する。設計が実装後の後付けにならないこと。

1. Issue本文・コメント・親/兄弟Issue・既存コードを調べ、`doc/design/issue-<N>-<topic>.md` を作る。書式はリポジトリの実装Packet（#680/#681）に合わせる:
   - 冒頭metadata: `Spec-Status`（READY/BLOCKED/DESIGN_NEEDED/RESEARCH）、`Depends-On`、`Decision-Gaps`、`Parent`
   - `## 成果物` / `## 入出力`（型・schema・エラーコード・上限） / `## 手順`（責務境界・状態遷移・失敗時の挙動・fail-closed方針） / `## テスト`（受入条件1つずつに対応する試験） / `## 完了条件`
   - 加えて `## 現状調査`（既存の関連実装と再利用するもの）、`## 代替案と不採用理由`、`## 範囲外・残件`（後続Issue番号）
   - 大きいIssueは「今回のスライス」と「残り」をここで決める。
2. 設計だけを1コミットにし、設計書だけの資料でレビューを1回行う（multi-ai-reviewの必須者。設計の誤りは実装前に直す）。
3. `Decision-Gaps` が残る（ownerの判断が必要）なら実装せず、Issueへ判断事項をコメントして報告。
4. Issueへ設計書のリンクと要約をコメントする（Issue本文は書き換えない）。

## 3. 実装→評価（反復）

1. 設計書のとおり実装。既存の命名・粒度に合わせる。テストはネットワーク不要・決定論的に。
2. 評価: 関連テスト → 設計書の `## テスト` と `## 完了条件` を1つずつ照合 → 不足なら実装へ、設計の誤りなら設計書を直してから実装へ戻る。
3. 設計から変わった点は設計書へ反映する（設計書と実装を一致させる）。
4. 文書: 該当の `doc/*.md`（機能説明書・運用資料）と `doc/versions.md` に追記。文書は実装と同じPRで更新する。

## 4. 完了ゲート

```powershell
# 変更ファイルだけ整形（E:/projects/FORMATTER_OPERATIONS.md）
python tools/generate_docs.py          # generated/repo_map.json が古いとCI失敗
Remove-Item Env:NoDefaultCurrentDirectoryInExePath -ErrorAction SilentlyContinue
& "$PWD\finish_task.bat"               # 失敗したらcommitしない
git checkout -- .codex/next_issue.md   # 生成物はcommitしない
git add -N .; git diff origin/main     # 自己レビュー（コーディングルール7観点）
```

- 個別テストは finish_task と同じ環境で: `PYTHONPATH="src;." PYTHONUTF8=1 python -m unittest discover -s tests -p "test_x*.py"`（PYTHONPATH無しだとImportErrorで誤判定）。
- #718（PR #720, 2026-11-04）以降、finish_task はこのPCで全段passする前提。失敗したら新規の回帰として扱う。実行時は `TEMP`/`TMP` を `E:/program_files/soft/claude/tmp` へ向ける。
- `generated/repo_map.json` の `M` がCRLF差だけのことがある（`git commit` で nothing to commit）。

- ツール経由の編集で `\uXXXX` がリテラル文字化することがある。正規表現・テスト文字列は差分で確認。

## 5. レビュー

`multi-ai-review` スキルに従う（必須6者＋追加1者Ori、PR時CodeRabbit。Haiku・Copilotは停止中）。証跡は `E:/projects/.review-work/AI_GAME_Player/issue-<N>-<topic>/`。指摘は原典と照合し、有効なものだけ修正→再整形→再テスト→修正部分のみ再確認。

## 6. PR→CI→マージ

- commit末尾 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`、PR本文末尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。本文は日本語、自己レビュー欄とレビュー証跡表を含める。
- 直前に `git fetch; git merge origin/main` で最新化（並列PRが先にマージされたら再度）。
- `gh pr create` → `@coderabbitai review` → CIと10分待ち → 指摘はスレッドごとに修正か理由付き返信でresolve。
- Sourcery（GitHub App）はCodeRabbitと別に自動で指摘を投稿し、`Sourcery review` checkが **fail だとマージゲートが止まる**（skippingのみ許容）。CodeRabbitの「Review rate limited」は省略として記録してよい。
- スレッドは**1件ずつ評価してから**resolveする。未解決IDを一括でresolveしない（後から投稿された指摘を取りこぼす）。
- opengrepの誤検知を抑止する `# nosemgrep: <rule>` は一致行（呼出しの開始行）に置く。ruff formatで閉じ括弧側へ移らないよう確認。
- 主担当のマージ定型（main取込→生成物更新→finish_task→push→ゲート）は同梱スクリプトで1コマンド: `& C:/Users/tomiy/.claude/skills/ai-game-player-issue-loop/merge-pr.ps1 -Worktree <path> -Pr <N>`（衝突・finish_task失敗で停止。バックグラウンド実行推奨）。負荷時にplanner timingテストだけ落ちた場合は単体再実行で確認し、#729の修正状況を確認する。
- マージは主担当のみ: `python tools/finish_pr_merge.py <PR URL>`（CI・未解決スレッドを検査して合格時のみマージ）。`--admin` や強制pushはしない。
- マージ後: Issueへ進捗コメント（完了か残件）、worktreeとローカルブランチを削除。

## 7. 並列実行（サブエージェント）

- 1 Issue = 1 サブエージェント = 1 worktree。ファイル衝突の少ない組を選ぶ（同じworkflowを触る場合は「最小編集」を依頼）。
- サブエージェントはshellのcwdが本体checkout（`E:\projects\AI_GAME_Player`）へ戻るため、相対pathのリダイレクトやWrite/Editで本体に空ファイルを作る事故が繰り返し起きた。依頼文で「全コマンドは `cd <worktree> &&` で始め、Write/Editは絶対pathのみ」と明記し、主担当はマージ毎に本体の `git status --short` を開始時と比べる。
- 依頼文に必ず含める: Issue番号とブランチ/worktreeパス、本体checkout禁止、§1〜§6の手順、**マージ禁止**、Agent起動禁止、署名はTomiya、リポジトリ設定変更禁止、最終報告は40行以内（PR URL・HEAD・CI・レビュー者ごとの状態・残件）。
- 主担当は報告を鵜呑みにせず、`gh pr view <N> --json headRefOid,mergeable,statusCheckRollup`、差分の要点、証跡ファイルの有無を確認してからマージする。マージは1件ずつ、後続PRは最新mainを取り込ませてから。取り込み後は先行PRが追加した検査（例: #514の「全workflowのpip installはtrust-floor action後」テスト）を含む関連テストを必ず再実行する。テキスト衝突が無くても意味的衝突で落ちる。

## 8. 付属スクリプトと運用上の既知事項（2026-11-05）

- `merge-pr.ps1 -Worktree <path> -Pr <N>`: mainの取り込み（衝突は追記専用の `doc/versions.md`・`doc/architecture/README.md`・`generated/repo_map.json` だけ自動解消）→生成物再生成→`finish_task`→push→Draft解除→マージゲート。状態で成否を判定し、終了コード0=MERGED。一時的な通信失敗は読み取り系のみ再試行。**同じPRで2本同時に起動しない**（ログを取り合う）。連続処理は `foreach` で終了コードを見て止める。
- `run-reviews.ps1 -Worktree <path> -Issue <N> -Topic <slug> -Purpose "<1行>"`: 差分から資料を作り、Luna/Continue/Qwen/Antigravity/Grok/Ori/Julesを並列実行して `evidence.md` を作る。返ってきた本文は必ず読んで有効な指摘だけ直す。
- 負荷（複数の `finish_task`・CI・レビューの並行）で時間依存テストが落ちる。単独で再実行して通る場合は、Issueにして期限・待ち方を直す（予算を緩めるだけにしない）。並行度は **3〜4本まで**。
- サブエージェントが作るPRは Draft のことがある。依頼文に「Ready for reviewで作る」を入れる。
- サブエージェントは本体checkoutに空ファイルを作る事故を繰り返す。マージ毎に本体の `git status --short` を確認する。
- CodeRabbit/Sourceryの指摘が付くとゲートが止まる。全スレッドを1件ずつ評価し、返信してresolveしてから再実行する。
- 安全に関わる設計判断が必要な未解決事項（Decision-Gaps）以外は、判断待ちにせず根拠を付けて自分で決めて実行する（ユーザー指示）。
- merge-pr.ps1 を `&&` で連鎖させる時は `| tail` を挟まない（パイプが終了コードを隠し、失敗後も次PRへ進んだ。2026-11-05 #819→#820）。`set -o pipefail` を付けるか、出力は `> log` に落としてから終了コードで分岐する。
- 並行サブエージェントが検証を回している間の finish_task は、時間上限系テスト（browser diagnostics deadline、trust_inventory probe、runtime_boundary の subprocess）が負荷で落ちる。マージ前の finish_task はサブエージェントが止まってから1件ずつ走らせる。
- Jules は npm の `jules`（起動毎に %TEMP%/jules_tmp へ再展開し、並行起動で壊れて spawn ENOENT / FAILED_PRECONDITION / invalid block type）を使わず、固定binary `E:/program_files/soft/jules/jules.exe` を直接呼ぶ（run-reviews.ps1・Invoke-AllReviews.ps1・multi-ai-review 修正済み 2026-11-05）。npm 側を更新したら同じ版の exe をここへ置き直す。
- run-reviews.ps1 は 11500 字超の差分をファイル境界→見出し/hunk 境界で packet-NN に分割し、資料ごとに順番にレビューする（切り詰めない）。結果は `<reviewer>-NN.json`。一時スクリプトで分割する必要はない。

## 9. 使用量の削減（このプロジェクト向け・品質と必須工程は下げない）

目的は「無駄の排除」。設計書・テスト・レビュー6者+CodeRabbit・finish_task・CIは省略しない。90%削減は目標で実績ではない。実測できない値は「未確認」と書く。

- **モデル分担**: 主担当と実装サブエージェントは Sonnet。Opus は advisor として次の3場面だけ（Issue 1件あたり原則2回まで）。(1) 設計書で Decision-Gaps（プライバシー・Artifact Admission・保持/削除・remote送信可否など安全に関わる判断）を確定する前、(2) 同じ失敗が2回続いた時、(3) P1の安全系Issueを「完了」と報告する前。小さなドキュメントIssue・機械的な追従修正では呼ばない。advisorは毎回会話全体を非キャッシュで読むため、長いセッションでは新セッションへ引き継いでから呼ぶ。advisorはレビュアーではなく、evidence.md の代わりにならない。
- **読み取り専用の調査**は Haiku サブエージェントへ: `search`=構造・所在の棚卸し、`grep`=呼出元・使用箇所、`docs`=設計書/Issue/API仕様の要約。1件のシンボル確認は主担当が `codegraph_explore` を1回直接呼ぶ。サブエージェントから別エージェントを起動させない。
- **実装サブエージェントの依頼文**は自己完結で短く（対象Issue・設計書path・触ってよいファイル・完了条件）。同じ設計書や規則集を丸ごと貼らず pathで指す。最終報告は40行以内（§7）。
- **出力の扱い**: テスト・finish_task・gh のログは `> log` に落とし、終了コードと失敗行だけ読む（巨大出力を会話へ貼らない）。判定は文字列照合でなく終了コード/PR状態で行う。
- **重い検証は1回**: 開発中は変更に関係するテストだけを回し、`finish_task`（約15〜20分）は PR につき `merge-pr.ps1` の1回を基本にする。ただし設計・コードを直したら必要な再検証は必ず行う（省略しない）。並行度は §8 の通り3〜4本まで。
- **レビュー**: 資料は差分のみ（`run-reviews.ps1` が分割）。同一資料を再送せず、返答本文は有効な指摘だけ読む。追加無料レビュアーは通常1者。利用不可は理由1行で省略し、再試行しない。
- **文脈**: 150k超・8時間超・並行4件超は区切りで新セッションへ。引き継ぎは目的・現状・証拠・未完了・検証境界を短く。`/compact` は上限前に。
