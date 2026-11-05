Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: none

# Issue #807 merge gateのDraft事前検出・merge失敗本文・読取専用APIの有界再試行

## 成果物
- `tools/finish_pr_merge.py`
  - PR状態取得（`gh pr view`）で`isDraft`も取得し、Draftなら**CI待機前**に「PR is a draft」と明示して終了値3で停止する。自動でreadyにはしない。
  - 最終merge失敗時、GitHub CLIのエラー本文（例: `Pull Request is still a draft`）を停止理由の先頭に含める。
  - 読取専用のGitHub API呼出し（PR状態・checks一覧・checks待機・review thread・Workflow Run・merge後確認）に、一時的な通信失敗だけを対象とする間隔付きの有界再試行を追加する。`gh pr merge`は再試行しない。
- テスト: `tests/test_finish_pr_merge_draft_retry.py`（新規）、既存`tests/test_finish_pr_merge.py`・`tests/test_finish_pr_workflow_runs.py`の`gh pr view`模擬応答へ`isDraft: false`を追加。
- 文書: `doc/ワークフロー/ワークフロー.md`（merge gateの節）、`doc/versions.md`。

## 入出力
- `gh pr view URL --json state,isDraft,mergeable,headRefOid,headRefName`。`isDraft`がboolでなければ停止（fail-closed）。`PullRequestSnapshot`に`is_draft: bool = False`を追加（既存の位置引数呼出しと互換）。
- 判定順: `state == OPEN` → `isDraft is False` → `mergeable == MERGEABLE` → head SHA形式。Draftの停止文:
  `Pull request is a draft; mark it ready for review on GitHub before running the merge gate (this tool does not change draft status). PR merge was stopped.`
- 一時的通信失敗の判定（`_is_transient_github_failure(text) -> bool`、大小無視の部分一致）:
  `tls handshake timeout`, `i/o timeout`, `timeout awaiting response headers`, `connection reset`, `connection refused`, `unexpected eof`, `temporary failure in name resolution`, `no such host`, `http 502`, `http 503`, `http 504`, `bad gateway`, `service unavailable`, `gateway timeout`。
  認証失敗（401/403）・404・rate limit（403/`HTTP 429`/Too Many Requests）・GraphQLエラー・JSON不正は再試行しない。
- 再試行定数: `READ_ONLY_RETRY_DELAYS_SECONDS = (10, 30)`（最大3回試行、待機合計40秒）。待機は`_sleep`（`time.sleep`）経由でテストから差替え可能。
- API呼出し（60秒）のtimeoutは一時的失敗として再試行する。`gh pr checks --watch`（60分）はtimeoutを再試行せず、通信失敗による再試行も最初の試行開始から60分の共通予算内で行う（各試行のtimeoutは残り予算。残りが無ければ再試行しない）。最悪の待機は60分＋待機40秒に収まる。
- 終了値は従来どおり成功0・安全停止3。

## 手順
1. `_run_gh`はtimeoutを`GitHubCommandTimeout(MergeGateError)`（新規subclass）で送出する。既存の捕捉箇所は親クラスで受けるため挙動は変わらない。
2. 新関数`_run_read_only_gh_command(arguments, timeout_seconds, action_description, retry_timeouts=True, share_timeout_budget=False)`:
   - `_run_gh`を実行し、終了code 0なら返す。
   - 非0終了で診断文が一時的失敗に一致、または（`retry_timeouts`のとき）`GitHubCommandTimeout`なら、残り回数がある限り`[RETRY] <action> hit a transient GitHub error (<要約>); retrying in Ns (attempt k/3).`を表示して待ち、再実行する。
   - `share_timeout_budget`のときは`time.monotonic()`で最初の試行開始から`timeout_seconds`の期限を固定し、各試行のtimeoutを残り時間にする。試行前・待機前に残りが1秒未満なら再試行せず停止する（予算超過の試行を始めない）。
   - 一致しない失敗、または回数を使い切った場合は、従来と同じ`<action> failed with GitHub CLI exit code N: <診断文>`で停止する（使い切り時は`after 3 attempts`を付記）。
3. 読取専用呼出しを全て`_run_read_only_gh_command`へ置換: `_read_pull_request_snapshot`、`_wait_for_status_checks`（`retry_timeouts=False, share_timeout_budget=True`）、`_read_status_checks`、`_read_unresolved_review_threads`の各ページ、`_require_workflow_runs.read_json`、`_confirm_remote_merge`。
4. `gh pr merge`は従来どおり`_run_gh`を**1回だけ**呼ぶ。結果がtimeout・非0でも再実行しない。
5. merge後確認（`_confirm_remote_merge`）は読取専用なので再試行対象。応答が「同じPR番号・同じhead・`state == open`・`merged is False`」なら専用の`MergeNotPerformedError(MergeGateError)`を送出する。ただし「GitHubが拒否した」と表示するのは、`gh pr merge`が**timeoutや起動失敗ではなく非0終了でエラー本文を返し、その本文が一時的通信失敗の判定に一致しない**場合に限る（通信失敗はGitHubが受理済みの要求を隠し得るため）（GitHubの`mergePullRequest`は同期的にエラーを返すため、拒否本文＋open/未mergeの確認は未実行を示す）。merge呼出しがtimeout・起動失敗・0終了の場合は、open/未mergeでも遅延反映の可能性を排除できないため、従来どおり結果不明として扱う。
6. `merge_pull_request_when_safe`の停止文:
   - `except MergeNotPerformedError`を`except MergeGateError`より先に置く。非0終了＋本文ありなら: `PR was not merged: <URL> is still open and unmerged after the merge command failed with: <GitHubのエラー本文>. Fix the reported cause, then rerun the merge gate (it re-verifies the PR state before merging).`（本文がGitHub由来か断定しない表現にする。実装レビューLuna指摘） 再実行してもgateは最初にOPEN・head・CIを再検証し、`--match-head-commit`で固定headだけをmergeするため、遅れて統合されたPRを二重にmergeしない。それ以外は下の結果不明文。
   - その他の確認失敗（結果不明）: `Merge command failed: <GitHubのエラー本文>. Remote merge outcome is unconfirmed for <URL>; do not repeat the merge blindly. Inspect: ... Confirmation: <理由>`。CLIが0終了なら先頭文は付けず従来文。
   - 確認成功時の挙動・表示は従来どおり。
7. 2回目のPR状態取得（merge直前）でもDraft検査が働くため、待機中にDraftへ戻されたPRもmerge前に止まる。

## テスト（受入条件との対応）
- AC1 Draft事前停止: `isDraft: true`で`gh pr view`だけ呼ばれ、`pr checks`・`pr merge`を呼ばず終了値3、stderrに`draft`を含む。`isDraft`欠落・非boolも停止。待機中にDraftへ変わった場合も`pr merge`を呼ばない。
- AC2 merge失敗本文: `pr merge`が`Pull Request is still a draft`で非0終了し、確認APIが`open/merged:false`を返すと、stderrに`PR was not merged`・`still open and unmerged`と当該本文を含み、`pr merge`は1回。確認APIが失敗する場合も本文と`unconfirmed`を含む。`pr merge`がtimeoutまたは`TLS handshake timeout`等の通信失敗本文で、確認APIが`open/merged:false`なら`unconfirmed`（拒否と断定しない）。
- AC3 有界再試行: `TLS handshake timeout`が2回続いた後に成功→処理継続し待機が`[10, 30]`。3回連続→停止し試行3回。`HTTP 401`・`HTTP 429`等は再試行0回。API timeoutは再試行、`--watch`のtimeoutは再試行しない。`--watch`の通信失敗再試行は残り予算をtimeoutに渡し、予算切れなら再試行しない。Workflow Run照会・merge後確認の一時失敗も再試行される。
- AC4 merge非再実行: `pr merge`がtimeout/`TLS handshake timeout`で失敗しても`pr merge`呼出しは1回だけ。
- 既存: `test_finish_pr_merge*.py`・`test_finish_pr_workflow_runs.py`全件が通る。

## 完了条件
- 上記テストと`finish_task.bat`が成功。
- ワークフロー.mdにDraft停止・merge失敗本文・再試行対象/非対象を記載。

## 現状調査
- `_read_pull_request_snapshot`はstate/mergeable/head SHA/head refのみ検査し、Draftを見ない。Draft PRでもGitHubは`mergeable: MERGEABLE`を返し得るため、CI待機（最大60分）後の`gh pr merge`で初めて失敗していた（PR #727）。
- merge失敗時、本文は`merge command: ...`として末尾にあり、確認失敗の文に埋もれていた。PRが開いたままでも「結果不明」と表示していた。
- 全読取呼出しは`_run_successful_gh_command`経由で1回失敗すると停止（PR #730のWorkflow Run照会が`TLS handshake timeout`で停止）。
- `WorkflowRunGate`は`read_json`を注入されるため、`finish_pr_merge.py`側の差替えだけで再試行できる（`pr_workflow_evidence.py`は変更不要）。

## 代替案と不採用理由
- Draftを自動でreadyにする: Issueの受入条件が禁止。人の判断を奪うため不採用。
- すべての非0終了を再試行: 認証失敗やcheck失敗を遅らせるだけで、`--watch --fail-fast`の失敗も繰り返すため不採用。
- `gh pr merge`も再試行: 結果不明時の二重merge・別head merge防止の既存方針（do not repeat the merge blindly）に反するため不採用。
- 指数バックオフ・無制限再試行: 運用時間の上限が不明確になるため、固定2回の有界再試行にした。

## 範囲外・残件
- `pr_workflow_evidence.py`の判定、`finish_pr.bat`の挙動、`ruleset_check.py`は変更しない。
- rate limit（403/429）の待機は別課題（必要なら別Issue）。
