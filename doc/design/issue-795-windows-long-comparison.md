Spec-Status: READY
Depends-On: #820（Plannerのworker準備完了待ち。実装着手前にmainへ取り込む。未mergeの間は設計のみ確定）
Decision-Gaps: none
Parent: #567（長枠比較の残件）。安定性の前提は #459/#794（merge済み）、Campaign基盤は #247

# Issue #795 Windows長枠でCycling・Baseline・Belief Plannerを同条件比較する

署名: Tomiya

## 目的

Windows実ゲーム（固定版2048、所有window、WGC＋OS入力）で、Cycling・Baseline・Belief Plannerを同seed・同予算・同環境で各600秒走らせ、Score/Max tile/Survival/invalid/no-op/reobserve/abstain/p50/p95/CPU/RAM/nodesを、失敗・打切り・欠測を母数に含めて比較する再現可能なRunnerとReportを作る。本設計書は設計のみ。実装は後続PR。

「Cyclingに勝った」だけで高性能AIとは呼ばない。Safety hard ruleと実入力検証は緩めない。#250（一般ゲーム実機30分）・#735（最低能力Gate）とは別で、残件を押し付けない。

## 現状調査

| 既存 | 内容 | 本Issueでの扱い |
|---|---|---|
| `tools/compare_2048_windows_planning.py` | 3 provider×1 seedの短枠（`--max-steps 12`、判断12回上限）。`run_episode`が起動→前処理parity→判断loop→保留時の新規frame→全判断後のscore OCR→Stop/旧世代probeまで持つ。`PlanningResourceMonitor`でCPU/RSS、`attempt_records`でnodes/p50/p95、`source_fingerprint`とnative binary hashで同一条件を検査 | 再利用。`run_episode`へ任意の`deadline_monotonic`を足し、長枠のepisode単位driverとして使う |
| `tools/run_2048_campaign.py` / `CampaignRunner` | 600秒PR profile（Cyclingのみ必須gate）。`--decision-provider`はあるが、**`act()`が`DecisionDisposition`（ABSTAIN保留）を`decision_failure`として全campaignを失敗**にする | 流用不可。BaselineやPlannerは保留が正常動作であり、最初の保留で600秒が終わる。campaign本体（必須600秒gate）は変更しない |
| `.github/workflows/language-ci.yml` | `windows-planning-comparison`はseed 479/480/481を12判断で並列。`windows-e2e`はCycling 600秒 | 変更しない。長枠は別workflowに置く |
| `tools/pr_workflow_evidence.py` `REQUIRED_WORKFLOW_PATHS` | mergeゲートの必須8 workflow | 新workflowを加えない（required checks・rulesetは変更しない） |
| `.github/workflows/python-security-ci.yml` | `schedule`＋`workflow_dispatch`の先例 | 同じ型の起動を使う |
| `doc/architecture/planner_runtime.md` | 短枠比較の条件・制約の正本。n=3の統計的有意性を主張しない方針 | 長枠の統計方針を同文書から参照する |

PR #802（`gameplay_capability_benchmark.md`）は未merge（mainに無い）。依存しない。#802が先にmainへ入れば、同文書のbenchmark契約と指標名を揃える（未決事項参照）。

## 成果物

- `tools/compare_2048_windows_long.py`（新）: 1つのbase seedに対し、3 providerを**順番に**1台の同一Windows上で各`--duration-seconds 600`走らせるRunner。provider順は全6順列をblock番号で巡回（`index % 6`。時間経過・温度・キャッシュの順序効果と直前providerの持ち越しを均す。8 blockでは完全には均衡しないため、順序を`provider_order_index`に記録し、aggregatorが順序別の件数を表示する）。provider単位に`run-report.json`、ブロック単位に`block-report.json`を出す。
- `tools/compare_2048_windows_planning.py`（修正）: `run_episode(..., deadline_monotonic=None)`を追加。`None`なら従来動作（短枠の既存テストは無変更）。判断loopは`deadline`到達後に新しい判断を始めず、`stop_reason="wall_budget_reached"`を記録する。
- `tools/windows_long_comparison_stats.py`（新）: 標準ライブラリだけの純関数（集計、percentile、Wilson区間、対応ありbootstrap区間、符号反転検定、欠測規則）。Windows依存なしでLinuxのunit testで検証する。
- `tools/aggregate_2048_windows_long.py`（新）: 全ブロックのreportと**期待manifest**から`report.json`と`report.md`（比較表）を作る。
- `.github/workflows/windows-long-comparison.yml`（新）: `plan`→`run`（seed matrix、windows-latest）→`aggregate`（ubuntu）。
- `doc/architecture/windows_long_comparison.md`（新）: 手順・指標定義・統計・制約・読み方。`doc/architecture/planner_runtime.md`へ1段落リンク。
- テスト（後述）、`doc/versions.md`、`generated/repo_map.json`、本設計書。

## 比較条件（同一性の担保）

| 項目 | 固定する値 | 検査 |
|---|---|---|
| seed | blockごとのbase seed。episode k のseedは`(base + 1000*(k-1)) mod 2^32`で3 provider共通 | runごとに`seed_fixture`（既存の`test_seed_metadata`: index/init script/応答のSHA）を保存し、3 providerでepisode番号が同じなら一致を検査 |
| 予算 | wall 600秒（play deadline）、判断attempt上限5000/episode、episode再起動は失敗recovery 2回まで（`DEFAULT_RECOVERY_LIMIT`と同じ）、min episode残り60秒未満なら新episodeを始めない | 設定値をreportの`budget`に保存。3 providerで不一致ならblock無効 |
| 環境 | 同一job＝同一VM上で3 providerを逐次実行。hardware fingerprint（CPU型番、論理core数、RAM、OS build、runner image version）をblock内で照合 | 不一致または取得不能はblock無効（unknownを許容しない） |
| 観測・入力・score OCR | 既存短比較と同じ: WGC待ち250ms＋同一対象GDI fallback、settle 1000ms、native FAST_CV前処理、Tesseract OCR（全判断後の別captureのみ。Source/Plannerへ戻さない）、Baseline `min_margin=0, min_recognition=0.65`、Planner depth2/nodes10000/wall400ms/global risk unknown | `condition_fingerprint`（git commit、`source_fingerprint`、game source lock、native DLL SHA256、harness SHA256、OCR版、Python版、上記profile）を全runに記録。全block・全providerで一致が必須 |
| 経路 | push/PR/main、Windows/headless、短枠12判断/長枠600秒は**別track**。reportの`track`は`windows_long_600s`、`ref_class`は`main`/`pr`/`manual` | aggregatorは異なる`track`・`git_commit`・`condition_fingerprint`の混在を拒否 |

wall予算の意味: play deadline（開始から600秒）以後は新しい判断を始めない。実行中の判断は完了させ、その後のscore OCR・Stop/旧世代probe・closeはplay予算の外だが、run全体のhard timeout（780秒）内。`wall_seconds`・起動/再起動/OCR/probeの`overhead_seconds`・`unused_budget_seconds`を分けて残し、3 providerの有効play時間の差を隠さない。

## 入出力

### `run-report.json`（`windows-long-run/v1`、1 provider×1 base seed）

- 識別: `track`, `ref_class`, `git_commit`, `provider`, `base_seed`, `provider_order_index`, `condition_fingerprint`, `hardware`, `budget`。
- 状態: `run_status` ∈ `completed` / `partial`（打切り・episode失敗を含むが入力あり）/ `failed`（入力0、起動不能、hard timeout等）。`stop_reason` ∈ `wall_budget_reached` / `recovery_exhausted` / `hard_timeout` / `fatal:<category>`。
- `episodes[]`: episode番号、seed、`episode_status`、`terminal`（右打切りか）、`actions`、`attempts`、`holds`、`invalid_no_op_actions`、`board_changes`、`policy_reobservations`、`nodes`、`score`（OCR確認値または`null`）、`max_tile`、`latency_ms`（生標本）、`resources`、失敗時は既存`failure_stage`/`failure`。
- 集計: 下表の指標。各値は`{"value": number|null, "status": "measured"|"not_applicable"|"unknown"}`。

| 指標 | 定義 | 欠測の扱い |
|---|---|---|
| score | episode別の画面OCR確認値（確信度≥0.65）。run値は`episode_mean_score`と`best_episode_score` | OCR不可episodeは`unknown`。平均は確認済みepisodeだけ、その個数を`score_episodes_measured`に併記。全episode不可なら`unknown` |
| max tile | run内最大 | 観測不能は`unknown` |
| survival | terminal到達episodeの実操作数の中央値。`terminal=false`の最終episodeは右打切りとして`censored_episodes`に数え、生存数へ混ぜない | terminal episodeが0なら`unknown`（打切りのみ） |
| invalid/no-op | 実行した操作で盤面signatureが変わらなかった数。率は実操作あたり | Safety/Verifierによる拒否・非実行は現行どおりepisode失敗になるため、失敗表で別に数える（invalidと混ぜない） |
| abstain | `ABSTAIN`保留のattempt数。率はattemptあたり | Cycling/Baselineも0と決め打ちせず実測値を書く |
| reobserve | Plannerの`policy_observation_requests`合計。率は実操作あたり | Cycling/Baselineは`not_applicable` |
| p50/p95 | 判断1回のlatency。run内の全標本で算出（nearest-rank）。blockをまたぐ集約は「run別p50/p95の中央値」と「全標本pooled」を別名で出す | 標本0は`unknown` |
| CPU/RAM | `PlanningResourceMonitor`のprocess tree標本。`cpu_seconds`は全episode合計（子プロセスは標本下限）、`peak_rss_bytes`は最大。`semantics`文言をreportに保持 | 取得失敗・`sampling_error`は`unknown`。OS hard budget証拠とは扱わない |
| nodes | 各attemptで受領済みの最新完成checkpointの`nodes_visited`合計（既存の定義） | Cycling/Baselineは`not_applicable`。Plannerで未受領のみなら`unknown` |
| copy/IO | 既存probeのbyte計数が使える範囲のみ記録 | 本スライスでは`unknown`（範囲外参照） |

未計測を`0`にしない規則を型で固定する: 値が`null`のとき`status`は必ず`unknown`か`not_applicable`、`measured`なら数値必須。aggregatorが検査する。

### `report.json`（`windows-long-aggregate/v1`）

- `manifest`（期待するbase seed集合・provider・budget・track）と`blocks[]`（seed×provider の状態: `completed`/`partial`/`failed`/`missing`/`invalid`）。manifestは`plan` jobが作り、**存在するartifactではなく期待集合を母数**にする。
- `provider_summary`: provider別に、run数、`completed`/`partial`/`failed`/`missing`件数、成功率とWilson 95%区間、各指標の中央値・四分位・min/max・個数。
- `contrasts`: `baseline-cycling`、`planner-cycling`、`planner-baseline`の3対比。block（base seed）を単位とした対応あり差。指標ごとに`n_pairs`、`n_excluded_unknown`、差の平均・中央値、区間、符号反転p値、勝/分/負件数、`verdict`。
- `comparison_table`: 全blockを1行ずつ（打切り・失敗・欠測を含む）。`report.md`はこれを読める表にする。
- 終了コード: `0`=全期待runが`completed`で会計が完全、`2`=会計は完全だが`partial`/`failed`/`missing`を含む（reportは出す）、`1`=契約違反（条件不一致、schema違反、期待manifestとblock集合の不整合、有効run0件）。

## 統計の扱い

- 解析単位はrun（provider×base seed）。3 providerは同じbase seedで対応付く。**一次解析は入力ありのrunをすべて含める**（`partial`の途中までの値も含める）。`failed`でも入力ありなら含め、入力0のrunは「スコアなし」ではなくunknownとして対応から除外し除外数を表示する。感度分析として「`completed`のみ」も併記する。
- 一次指標は`episode_mean_score`（事前固定）。他の指標（max tile、survival、率、p50/p95、CPU/RAM、nodes）は記述統計と区間を出すが`verdict`は付けない（探索的）。
- 区間: 対応あり差のpercentile bootstrap（10000回、`random.Random(795)`で決定的、3対比のBonferroni補正で信頼水準 `1-0.05/3`≈98.3%）。p値: 符号反転検定（n≤16は全列挙、超過は決定的乱数のMonte Carlo）。成功率はWilson区間。
- `verdict`が`better`/`worse`になる条件は、`n_pairs ≥ 8`（`N_MIN_VERDICT`定数）かつbootstrap区間が0を含まず、かつ符号反転p値が補正後の閾値以下。それ以外はすべて`inconclusive`。`N_MIN_VERDICT`・水準・resample数・乱数seedはコード定数で、CLIや環境変数から下げられない（下げるにはコードと設計書の変更とレビューが要る）。n<8は区間を出さず`insufficient_samples`。
- 3 seed×1回等のn小の結果を有意性として主張しない。reportと文書の見出しは「測定条件と標本数つきの比較」とし、「高性能」「改善」の語を結論欄に使わない。Cyclingに勝つこと自体を合否にしない（Safety・入力検証・欠測会計が合否）。
- 1つのblockはVM性能差の影響を受けるが、3 providerが同一VMで逐次走るためblock内の対応差で相殺する。provider順の6順列巡回で時間経過の偏りも均す。runner imageが変わった場合は`hardware.image_version`で層別表示する（混合したままp値の根拠にしない）。

## 失敗時の挙動（fail-closed）

1. 起動/前処理parity/初期観測の失敗: 既存どおり入力0でepisode失敗。同runのrecovery（最大2回）で新episodeを試し、使い切ればrun `failed`、`stop_reason=recovery_exhausted`。
2. 入力0（`actions_total == 0`）のrunは`completed`にしない。ABSTAIN連続で600秒保留し続けた場合も`failed`（入力0、保留率1.0を記録）。
3. hard timeout（780秒）は子processを回収して`failed`/`hard_timeout`。回収できない場合は残プロセスを記録して終了コード非0。
4. Stop/旧世代probe失敗、Safety違反、Source/Plannerへのscore逆流の疑いは致命扱いでrun `failed`。比較結果を残すが成功に数えない。
5. aggregatorは、期待manifestにあるのにartifactが無いblockを`missing`（母数に含める）、schema違反や条件不一致のblockを`invalid`として表に残し、`invalid`が1件でもあれば統計・`verdict`を出さず終了コード1。
6. workflowのjobは`if: always()`でartifactを保存し、aggregate jobもrun jobの成否にかかわらず実行する。

## CI上の置き場

- 新workflow `windows-long-comparison.yml`: `on: workflow_dispatch`（入力: `seed_start`既定479、`seed_count`既定8（1〜32に検証）、`ref_class`）。`seed_count<8`の手動実行（動作確認）ではrun/block reportと表は通常どおり出るが、統計は`insufficient_samples`・`verdict`なしになる。この挙動を`report.md`の先頭に明示する＋ `schedule`（週1回、日曜夜UTC。実行時間と課金の根拠は下記）。`push`/`pull_request`は付けない。top-level `permissions: contents: read`、既存のpython-trust-floor actionと同じpin規則に従い、`tools/workflow_security_check.py`を通す。
- `plan`（ubuntu）: manifestとmatrix JSONを出力。`run`（windows-latest）: base seedごとのmatrix。1 jobで3 providerを逐次実行、`timeout-minutes: 90`（最悪3×780秒=39分＋依存install/native build/アップロードの余裕。短枠の20分とは別。runner遅延で打ち切られても、期待manifestにより`missing`として母数に残る）。`aggregate`（ubuntu）: artifactを集めて`report.json`/`report.md`を生成・upload・job summaryへ要約。
- 判断: 毎晩ではなく**週1回のmain track＋手動のPR/branch track**。8 block×約40分≈5.3 windowsランナー時間/回で、毎晩は過剰。cron頻度はworkflow内の1行で変えられ、設計の正しさに影響しない。
- `REQUIRED_WORKFLOW_PATHS`・branch protection・ruleset・必須32 checksには**触れない**。長枠は必須gateではなく測定。PRの`language-ci`の短比較・Cycling 600秒gateは無変更。
- 手動実行は対象refのcommitを`git_commit`に残し、`ref_class=pr`/`manual`と`main`の結果を同じ表に混ぜない（aggregatorが`ref_class`と`git_commit`の混在を拒否）。

## 手順（Runnerの流れ）

1. 条件確認: `os.name=="nt"`、`--allow-live-input`、OCR/psutil/native DLLの存在を確認。いずれか欠ければ入力前に非0で終了（短比較と同じ）。
2. `condition_fingerprint`と`hardware`を算出し保存。
3. provider順を`itertools.permutations(["cycling","baseline","planner"])`の`base_seed_index % 6`番目の順列で決め、各providerで：
   a. `deadline = start + 600`。
   b. episode k=1,2,…：`run_episode(provider, seed(base,k), attempt_cap=5000, deadline_monotonic=deadline)`。terminalなら次episode、deadline到達で終了、失敗ならrecoveryを消費して次episode。残り60秒未満なら新episodeを始めず`unused_budget_seconds`に記録。
   c. episodeごとに`run-report.json`を逐次書き換え（途中でprocessが落ちても部分結果が残る）。
4. provider間でgame processと一時directoryを完全に閉じ、残プロセスが無いことを確認してから次へ。
5. block終了時に`block-report.json`（3 providerの要約＋fingerprint一致結果）を書く。成否は`run_status`の集合から判定（出力文字列の照合ではなく状態値）。

## 変更ファイル一覧

新規: `tools/compare_2048_windows_long.py`、`tools/windows_long_comparison_stats.py`、`tools/aggregate_2048_windows_long.py`、`.github/workflows/windows-long-comparison.yml`、`doc/architecture/windows_long_comparison.md`、`tests/test_windows_long_comparison_stats.py`、`tests/test_windows_long_comparison_runner.py`、`tests/test_windows_long_comparison_aggregate.py`、`tests/test_windows_long_comparison_workflow.py`。
修正: `tools/compare_2048_windows_planning.py`（`deadline_monotonic`）、`tests/test_windows_planning_comparison.py`（deadline追加分）、`doc/architecture/planner_runtime.md`（リンク1段落）、`doc/versions.md`、`generated/repo_map.json`。
触れない: `run_2048_campaign.py`/`CampaignRunner`（必須600秒gate）、`language-ci.yml`、`pr_workflow_evidence.py`、Safety/Verifier/Planner本体、fail-safe lease。

## テスト計画（受入条件と1対1）

| 受入条件 | 試験 |
|---|---|
| Windows 3 Provider共通長枠reportと再現可能なArtifact | runnerテスト: 偽`run_episode`で3 providerが同一`budget`・同一episode seed列・6順列巡回のprovider順で実行され、`run-report`/`block-report`が書かれる。`condition_fingerprint`が全runで一致、1つ変えるとaggregatorが拒否。workflowテスト: 起動は`workflow_dispatch`/`schedule`のみ、top-level permissions、artifact upload、`REQUIRED_WORKFLOW_PATHS`に未追加、`workflow_security_check`通過 |
| 打切り・失敗・欠測を含む比較表 | aggregateテスト: completed/partial/failed/missing/invalidを含む入力で、全blockが表に1行ずつ出る。manifestにあるartifact欠落が`missing`として母数に入る。partialは一次解析に含まれ、感度分析（completedのみ）が併記される |
| Score/Max tile/Survival/p50/p95/CPU/RAM/nodesを報告、未計測はunknown | statsテスト: 値`null`で`status=measured`は拒否、`0`への置換不可。Cycling/Baselineのnodes/reobserveは`not_applicable`。全episode OCR不可でscoreが`unknown`。terminal 0件のsurvivalが`unknown`（打切りを生存数に混ぜない）。標本0のp50/p95が`unknown` |
| 統計（paired seed・標本数・信頼区間、単純緩和禁止） | statsテスト: 既知差分データでbootstrap区間と符号反転p値が決定的に再現。n<8は区間なし`insufficient_samples`・`inconclusive`。n=8でも区間が0を含めば`inconclusive`。`N_MIN_VERDICT`等が定数でCLI/環境から変えられない（引数を渡す経路が無いことを検査）。3対比のBonferroni水準を検査 |
| 全失敗・途中打切りも母数 | runnerテスト: recovery使い切り→`failed`/`recovery_exhausted`、入力0→`completed`にならない、全保留600秒→`failed`、hard timeout→`failed`。aggregateテスト: 終了コード0/2/1の分岐（状態値で判定） |
| 同条件（seed・予算・hardware） | runnerテスト: hardware fingerprint不一致のblockは`invalid`で統計から除外・終了コード1。aggregateテスト: 異なる`track`/`ref_class`/`git_commit`の混在を拒否 |
| deadline追加が既存短比較を変えない | `test_windows_planning_comparison.py`: `deadline_monotonic=None`で既存全テスト無変更で通る。deadline到達後は新判断を始めず`wall_budget_reached`、実行中判断は完了する（偽時計） |
| Safety hard rule・実入力検証の維持 | runnerテスト: Stop/旧世代probe失敗・score逆流の疑いでrun `failed`。Safety/Verifier/lease/Plannerのコードに差分が無いことを`git diff --stat`で確認（完了条件） |
| #567の残件を満たす証拠を元Issueへリンク | 実測後に#567へrun URL・artifact名・report SHA256をコメント（実測は実装PRのCI/手動実行。本設計PRでは行わない） |
| 未実施はOpen Issueで追跡 | 範囲外の各項目に後続Issueを起票（次節） |

## Issueの完了条件との対応

| Issueの項目 | 対応する設計箇所 | 状態 |
|---|---|---|
| Windows 3 Provider共通長枠reportと再現可能なArtifact | 成果物・比較条件・入出力・CI上の置き場 | 設計済み |
| 打切り・失敗・欠測を含む比較表 | `comparison_table`・失敗時の挙動・manifest母数 | 設計済み |
| Score/Max tile/Survival/p50/p95/CPU/RAM/nodesを報告、未計測はunknown | 指標表・未計測規則 | 設計済み（copy/IOは範囲外） |
| #567の残件を満たす証拠を元Issueへリンク | テスト計画の最終行 | 実装PR後の作業 |
| 未実施はOpen Issueで追跡 | 範囲外・残件 | 設計済み |
| 手法: 固定版・同seed集合・同予算・同hardware/観測/入力/OCR | 比較条件 | 設計済み |
| 手法: paired seed・実験条件・サンプル数・信頼区間 | 統計の扱い・入出力 | 設計済み |
| 手法: push/PR/main、headless/Windows、短枠/長枠を別track | 比較条件の「経路」・CI | 設計済み（headless長枠は範囲外） |
| 手法: Cyclingに勝つだけで高性能と呼ばない | 統計の扱い | 設計済み |

## 代替案と不採用理由

- **`CampaignRunner`で`--decision-provider baseline/planner`を600秒実行**: 最初のABSTAIN保留で`decision_failure`になり600秒に到達できない。保留を正常扱いにすると必須Cycling gateの挙動が変わるため不採用。
- **provider×seedを別jobに並列**: VMが変わりhardware差が対応差に混ざる。同一VM逐次を採用。
- **毎晩実行**: 約5.3 windowsランナー時間/回で過剰。週1回＋手動。
- **n=3（既存seed）で「改善」と報告**: 区間・検定が成立しない。最小8 blockを事前固定。
- **途中失敗runを除外**: 生存者バイアスになる。入力ありのrunはすべて一次解析に含める。
- **JSON Schemaファイルを`config/`に追加**: contract-assetsのschema一覧との結合が増える。検証はstats/aggregatorのPython関数とテストで行う。

## 範囲外・残件（実装PR後に起票し番号をここへ追記）

1. headless長枠（Linux）と短枠12判断との結果を同表に載せる統合表示。
2. copy/IOのbyte計数のrun集計（既存probeの長枠化）。
3. 一般ゲーム実機30分は#250、最低能力Gateは#735。
4. Belief Plannerの性能改善そのもの（本Issueは測定のみ）。
5. 行動数を揃えた比較（同action数時点のscore。traceにstep別scoreが必要）。

## 未決事項

なし。技術的に決まる事項は決定済み: seed数8（verdict最小n、既存479–481を含む479–486）、週1回cron、episode seed間隔1000、hard timeout 780秒、Bonferroni 3対比、一次指標`episode_mean_score`。#802が先にmainへ入った場合のみ、指標名を同文書へ揃える軽微な追従が必要。
