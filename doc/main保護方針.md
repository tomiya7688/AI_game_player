# mainブランチ保護方針（Repository Ruleset）

この文書は、AI Game Playerの`main`ブランチへ、CIを通過していない変更が通常のマージ経路で入らないようにする設定と運用を説明します。読者は、PRをマージする作業者（人、Claude Code、Codex）と、CIのworkflowやjob名を変更する開発者です。対象はGitHub Issue #92です。

## 設定の正本と適用状態

- 設定の正本は`.github/rulesets/main-protection.json`です。GitHubのRepository Ruleset `Mainguard`（ID 24724858）へ、この内容をそのまま適用します。
- このファイルをリポジトリへ追加しただけでは、GitHub上の設定は変わりません。GitHub上の`Mainguard`は、Issue #92の作業開始時点で`enforcement: disabled`（無効）でした。リポジトリ所有者の許可を得て適用した時点で有効になります。
- 適用するコマンドは次のとおりです。実行にはリポジトリ管理者の権限が必要です。

```powershell
gh api --method PUT repos/tomiya7688/AI_game_player/rulesets/24724858 --input .github/rulesets/main-protection.json
```

## 有効にする規則

| 規則 | 設定 | 理由 |
|---|---|---|
| 対象 | 既定ブランチ（`main`） | 通常の変更はすべて`main`へ入るため。 |
| 削除禁止 | `deletion` | `main`の誤削除を防ぐ。 |
| force push禁止 | `non_fast_forward` | 履歴の書換えでCI済みの変更が消えることを防ぐ。 |
| PR必須 | `pull_request` | 直接pushを禁止し、CIとレビューを通す。 |
| 必要な承認数 | 0 | 現在は人の保守者が1人で、作業者のAIも同じアカウントでPRを作るため、自分のPRを承認できない。承認数を1以上にすると通常のマージができなくなる。承認数が0の間は、古い承認の破棄（`dismiss_stale_reviews_on_push`）と最終pushの承認（`require_last_push_approval`）は効果がないため無効にしている。領域別の承認規則はIssue #79で決め、承認数を上げるときにこの2つも見直す。 |
| 許可するマージ方式 | merge・squash・rebase（`allowed_merge_methods`） | 既存の`Mainguard`の設定を維持する。`tools/finish_pr_merge.py`はmerge commit（`gh pr merge --merge`）でマージする。方式を1つに絞るかはIssue #79のマージ方針で決める。 |
| 未解決のreview threadの禁止 | `required_review_thread_resolution: true` | `tools/finish_pr_merge.py`のマージ条件（未解決threadがないこと）と一致させる。 |
| 必須status check | 必須7 workflowの全job（27件） | 後述。 |
| 最新化の要求 | `strict_required_status_checks_policy: false` | 後述。 |
| Copilotの自動レビュー | `copilot_code_review`（push時、Draftは除く） | ワークフローのAIレビュー依頼と同じ目的。マージ条件にはしない。 |
| bypass | なし（`bypass_actors: []`） | 後述。 |

## 必須status check

- 必須とするのは、`tools/pr_workflow_evidence.py`の`REQUIRED_WORKFLOW_PATHS`に列挙した7つのworkflow（`tests`、`language-ci`、`safety-ci`、`fail-safe-ci`、`bug-ci`、`performance-ci`、`upd-architecture-ci`）が報告する全jobです。マージゲート`tools/finish_pr_merge.py`も同じ7 workflowの成功を要求するため、GitHub側の保護とマージゲートの条件が一致します。
- 各checkは`integration_id: 15368`（GitHub Actions）からの報告だけを受け付けます。別のGitHub Appが同じ名前のcheckを成功として報告しても、条件を満たしたことになりません。
- 7 workflowはどれもpath filterを持たず、すべてのPRで全jobが実行されます。したがって、文書だけのPRで必須checkが報告されずに待ち続けることはありません。文書だけのPRに重いjobを実行しない方針（Issue #92の非対象）は、Issue #91でPR Fast Gateを定義した時点で、必須checkの範囲と合わせて見直します。
- CodeRabbit、Sourcery、Copilotのレビュー結果は必須checkにしません。無料枠の都合で応答しない場合があるためです。マージゲート`tools/finish_pr_merge.py`も、GitHub Appの`CodeRabbit`・`Sourcery review`のcheck（`gh pr checks`のworkflow欄が空文字のもの）が`skipping`（無料枠切れなど）で終わった場合だけは、記録して停止しません。名前は完全一致で判定します。これらのcheckが失敗・保留・取消の場合、workflow欄が欠落している場合、同じ名前のGitHub Actions jobが`skipping`の場合、報告されたcheckがこの2つだけでCIが確認できない場合は、従来どおりマージを止めます。

## 最新化（up-to-date）を要求しない理由

`strict_required_status_checks_policy`を有効にすると、`main`が進むたびに全PRで`origin/main`の取込みとCIの再実行が必要になります。複数のAIと人が並行してPRを出す運用では、Windows E2Eを含むCI（1回約10分）の再実行が連鎖します。代わりに、[ワークフロー](ワークフロー/ワークフロー.md)の「必ず守る順序」の手順5（PR作成の直前にもう一度pullする）でPR作成直前に`origin/main`を取り込み、`tools/finish_pr_merge.py`がマージ直前にhead commitを固定して検証します。古いbaseでCIが成功したPRがマージされる余地は残るため、競合や回帰が増えた場合は有効化またはmerge queueを再検討します。

## bypassを設定しない理由と緊急時の手順

AI作業者はリポジトリ所有者のGitHub認証でPRを操作します。所有者や管理者にbypassを許可すると、通常の`gh pr merge`でも規則を迂回できるおそれがあり、AIが誤って保護を外す経路が残ります。このため`bypass_actors`は空にします。

ただし、bypassを空にしても、所有者の認証を持つ作業者はRulesetの設定APIで`Mainguard`自体を無効化できます。AI作業者がRulesetの設定を変更しないことは、この文書とワークフローの規則で禁止しているだけで、GitHubの権限では防げていません。技術的に防ぐには、AI作業者に管理権限を持たないトークンを使わせる必要があり、現時点では未対応です。

CIの障害などで、必須checkを満たせない緊急修正を`main`へ入れる必要がある場合は、リポジトリ所有者（人）だけが次の手順で行います。AI作業者はこの手順を自分の判断で実行しません。

1. 緊急対応のIssueを作り、理由、対象PR、満たせない必須check、影響範囲を記録する。
2. GitHubの設定画面、または`enforcement`を`disabled`にしたJSONで`Mainguard`を一時的に無効化する。
3. 対象PRだけをマージする。
4. 直後に`.github/rulesets/main-protection.json`を再適用して保護を戻す。
5. 再適用できたことを、`gh api repos/tomiya7688/AI_game_player/rulesets/24724858 --jq '.enforcement, ([.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks[]] | length)'`で確認する。出力が`active`と必須checkの件数（現在27）であれば保護が戻っている。
6. 緊急修正の回帰テストと、満たせなかったcheckの修復を、Issueの後続作業として記録する。

Ruleset全体を無効化している間は、必須checkだけでなく、直接push・force push・削除の禁止も外れます。無効化している時間は、対象PRのマージに必要な最短時間にしてください。

## job名やworkflowを変更するとき

GitHubは必須checkをjobの表示名（`name:`、matrixの値を展開した名前）で照合します。必須checkの名前と実際のjob名がずれると、存在しないcheckを待ち続けて全PRがマージできなくなるか、新しいjobが必須にならずに失敗したまま通ります。

1. `tools/ruleset_check.py`は、必須7 workflowのjob名から期待するcheck名を求め、`.github/rulesets/main-protection.json`の一覧と比べます。差があればCI（`tests.yml`の`Python quality (Ruff + syntax)`）と`finish_task.bat`が失敗します。
2. job名、matrixの値、必須workflowを変更したPRでは、`python tools/ruleset_check.py --print-expected`の出力（各要素に`"integration_id": 15368`が付いたJSON配列）で、`.github/rulesets/main-protection.json`の`required_status_checks`の値を同じPR内で置き換えます。
3. 有効なGitHub上のrulesetは古い名前を要求し続けるため、名前を変えたPRは次の順序でマージします。
   1. 名前を変えたPRのCIが成功し、マージできる状態になるまで待つ。
   2. リポジトリ所有者が更新後のJSONを適用する。この時点から、他の未マージPRは新しい名前のcheckを報告していないためマージできなくなる。
   3. 直後に名前を変えたPRをマージする。
   4. 他の作業者へ、`origin/main`を取り込んでCIを再実行するよう、各PRまたはIssueで知らせる。
4. `tools/ruleset_check.py`がjob名を予測できるのは、`name:`の`${{ matrix.KEY }}`がインラインリスト（`os: [a, b]`）または`include:`だけのmatrixから決まる場合です。次の形は推測せずにエラーにします: `name:`のないmatrix job、再利用workflowを呼ぶjob（`uses:`）、job単位の`if:`、matrix以外の`${{ }}`式を含む名前、引用符のない数値・真偽値（YAMLでは`3.10`が`3.1`として表示されるため）、リストと`include:`の混在、`exclude:`、ブロック形式のリスト、複数のworkflowで同じ名前になるjob。こうした形を使う場合は、先にツールとテストを拡張します。
5. 同じツールは、rulesetファイルが`enforcement: active`、既定ブランチ対象、bypassなし、全checkの`integration_id`が15368であること、削除・force push・PRの規則を含むことも検査します。GitHub上に適用済みの設定は読みません。

## merge queueの評価

merge queueは現時点では導入しません。理由は次のとおりです。

- PRをマージする保守者は1人で、同時にマージ待ちになるPRは少数です。
- マージ直前のhead固定検証は`tools/finish_pr_merge.py`が行っています。
- 個人所有のリポジトリでmerge queueを有効にできるかは未確認です。

`main`の取込み遅れによる回帰や、マージの競合が増えた場合は再評価します。
