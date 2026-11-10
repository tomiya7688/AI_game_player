---
name: babysit
description: 自分が作ったPRのCI・レビュー・botコメントのイベントに、少ない使用量で対応する手順。PR活動の通知（CI失敗、レビュー、botコメント）を受けたときに使う。
---

# PRイベント対応（使用量を抑える手順）

通知の本文（botの概要・ウォークスルー・長いログ）は読み込まない。まず状態を1コマンドで把握し、必要な所だけを深掘りする。

1. **状態の把握**: `python3 tools/pr_digest.py <PR番号...> [--bots]`（#898の`tools/pr_digest.py`）。CI件数・失敗したチェック名・人のレビュー・botのインライン指摘（重大度・場所・見出し・返信の有無）を数行で返す。**出すのはインラインの指摘だけ**で、見出しは目印にすぎない。指摘に対応する前にそのコメントを開き、レビューを完了とする前に`pull_request_read`の`get_reviews`（レビュー本文）と`get_comments`（通常コメントのbot要約）も読む。スレッドの解決状態はGraphQLでしか取れないので出ない（`get_review_comments`で確認し、`resolve_review_thread`で解決する）。
2. **行動が要らないイベントには返答しない**: 概要コメントの更新、botの「Review triggered」「Review finished」、Sourceryのガイド、スキップ・取消(cancelled)・実行中のCI通知は、`pr_digest.py`で状態が変わっていなければ何も書かない（同じ通知への個別応答を繰り返さない）。
3. **CI失敗**: PRの変更と無関係な不安定な失敗は、全workflow runの完了後に`python3 tools/pr_digest.py <PR番号> --rerun-failed`で失敗ジョブを1回だけ再実行して確かめる（実行中のrunがある間と、再実行済みのrunには何もしない。要求の反映が遅れる間は重複を防ぎきれないので、実行直後に続けて実行しない）。それ以外の失敗は、 `gh api repos/<repo>/actions/jobs/<job_id> --jq '.steps[]|select(.conclusion=="failure")|.name'`で失敗ステップ名を取り、ログは失敗箇所の周辺だけを読む（`get_job_logs`の`tail_lines`は40前後）。原因は最小の再現（失敗したテスト1本）で確かめてから直す。
4. **指摘は全件を読んでから1回でまとめて直す**: `pr_digest.py --bots`で一覧し、妥当なものを同じ作業で全部直して**1回だけpush**する。push中はCIとbotレビューが再実行・中断されるので、1件ずつpushしない。直す前に`generate_docs --check`・ruff・mypy・関連テストを通す（生成物の更新漏れでCIを無駄に回さない）。
5. **返信は理由つきで見送る指摘にだけ**: 直した指摘にはコミットで足りる。見送る指摘は、スレッドに理由を1コメントで返す。bot相手の長い議論はしない。
6. **botの無料枠切れ・無応答は1回だけ記録**: PR本文のReview欄に「not run: 理由」と書き、再依頼を繰り返さない（枠が戻る時刻が分かるときだけ1回再依頼）。
7. **完了の判断**: `pr_digest.py`のCIが全件success、失敗0、`no_reply`の指摘がコミットで解消済みまたは理由返信済み、で完了。条件を満たしたらAGENTS.mdのマージ条件に従う。環境が操作を拒否したら回避せず報告する。

## 使用量の目安

- 概要・ログ・差分を丸ごと貼らない。`--jq`で必要な列だけ、`head`/`tail`で行数を絞る。
- 文脈が約150kに近づいたら、`CLAUDE.md`の「コンパクト指示」に沿って要約を残す（自動コンパクトの基準は`.claude/settings.json`の`autoCompactWindow`）。
- 長い処理は`run_in_background`で実行して終了通知を待つ。5秒を超える前景sleepと、確認だけの繰り返しをしない。
