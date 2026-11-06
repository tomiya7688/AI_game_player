# Windows長枠3 Provider比較(Issue #795)

- 目的: Windows実ゲーム(固定版2048、所有window、WGC+OS入力)で、Cycling・Baseline・Belief Plannerを同seed・同予算・同VMで各600秒走らせ、失敗・打切り・欠測を母数に含めて比較する。
- 状態: 実装済み(別workflow、測定のみ。必須gateではない)。実測結果は実行後に#567へリンクする。
- 最終確認日: 2026-11-07
- 関連: [設計書](../design/issue-795-windows-long-comparison.md)、[Planner runtime](planner_runtime.md)、親 #567、安定性 #459/#794、Campaign #247、一般ゲーム実機 #250(別)

## 使い方

リポジトリ直下から実行し、先に `PYTHONPATH=src;.`(PowerShellでは `$env:PYTHONPATH = "src;."`)を設定します。

```text
# Windows(所有window、OCR、psutil、native DLLが必要)。1 base seedで3 providerを逐次実行
python -u tools/compare_2048_windows_long.py --base-seed 479 --block-index 0 --ref-class manual --allow-live-input --output build/windows-long/block-479
# 期待manifest作成(--outputにmanifestを書き、stdoutにmatrix JSON)
python tools/aggregate_2048_windows_long.py plan --seed-count 8 --git-commit <40桁commit> --output build/plan/manifest.json
# 全blockの集計(report.json / report.md)
python tools/aggregate_2048_windows_long.py aggregate --manifest build/plan/manifest.json --artifacts <artifact dir> --output build/report
```

CIは `.github/workflows/windows-long-comparison.yml`(`workflow_dispatch`と週1回のschedule。pushとPRでは動かさず、required checksとrulesetは変更しない)。`plan`(manifest)、`block`(seedごとのWindows job、90分上限、3 providerを逐次)、`aggregate`(期待manifestを母数に集計)の3段です。

## 構成

| 部品 | 責務 |
|---|---|
| `tools/compare_2048_windows_planning.py` `run_episode(deadline_monotonic=)` | 既存の短比較のepisode実行。任意の期限後は新しい判断を始めず`stop_reason=wall_budget_reached`を返す。`None`なら従来動作 |
| `tools/compare_2048_windows_long.py` | provider順(6順列を`block_index % 6`で巡回)、episode seed(`base + 1000*(k-1)`)、600秒内のepisode反復、recovery 2回、provider単位のhard timeout(780秒、子processで実行し木ごと回収)、hardware・条件fingerprintの照合、`run-report.json`と`block-report.json` |
| `tools/windows_long_comparison_stats.py` | 標準ライブラリだけの純関数。指標集計、nearest-rank percentile、Wilson区間、対応ありbootstrap、符号反転検定、`classify_run` |
| `tools/aggregate_2048_windows_long.py` | `plan`(manifestとmatrix)と`aggregate`(比較表、provider要約、対比、感度分析、終了コード) |

## 状態と終了コード

`run_status`はepisode記録とstop_reasonの状態値から決めます(出力文字列の照合はしない)。

| status | 条件 |
|---|---|
| completed | 600秒の予算まで走り(残り60秒未満で新episodeを始めず終えた場合も含み、未使用時間は`unused_budget_seconds`に記録)、入力あり、episode失敗なし |
| partial | 入力ありでepisode失敗を含む(回復して継続) |
| failed | 入力0、recovery使い切り(期限後の3回目の失敗も含む)、hard timeout、Stop/旧世代probe失敗、cleanup失敗、SafetyGuard拒否、workerのreport欠落・終了コードとreportの食い違い |
| missing | 期待manifestにあるのにartifactが無い(集計側で母数に入れる) |
| invalid | schema・必須指標・件数の型・track/ref_class/commit・budget・条件fingerprint・hardware・seed fixtureの不一致、または親が計算した条件とworkerの条件の不一致(集計側もblockの自己申告だけでなく、run/blockの一致とviolations空を再検証する) |

- Runner終了コード: `0`=3 provider全てcompleted、`2`=blockは有効だがpartial/failedを含む、`1`=条件不一致や前提不足(入力前に終了)。
- 集計終了コード: `0`=全期待runがcompleted、`2`=会計は完全だがpartial/failed/missingを含む、`1`=契約違反(無効block、manifest外block、重複、有効run0件など。統計とverdictは出さない)。workflowは2でも赤にし、reportは先にuploadする。

## 指標

score(画面OCRの確認値のみ)・max tile・survival・invalid/no-op・abstain・reobserve・p50/p95・CPU/RAM・nodesを記録します。値は`{value, status}`で、`status`は`measured`/`unknown`/`not_applicable`。`measured`は有限数必須、`unknown`と`not_applicable`は`value=null`で、未計測を0にしません。

- survivalはterminalに到達したepisodeの実操作数の中央値。時間切れのepisodeは右打切り(`censored_episodes`)で、生存数へ混ぜません。terminalが0件なら`unknown`。
- Cycling/Baselineのnodes・reobserveは`not_applicable`。Plannerでcounters未受領なら`unknown`。
- CPU/RAMは`PlanningResourceMonitor`の標本(下限)で、OSのhard budget証拠ではありません。標本の欠測が1件でもあれば`unknown`。
- copy/IOは本スライスでは`unknown`(範囲外)。
- 期限到達で止まったepisodeは、実入力1回以上で最小入力の検査を満たします(短比較の3回以上は従来どおり)。Stop/旧世代拒否と盤面変化の検査は維持します。

## 統計

一次指標は`episode_mean_score`(事前固定)、解析単位はblock(base seed)内の3 provider対応差です。区間はpercentile bootstrap(10000回、乱数seed 795、3対比のBonferroni補正で信頼水準約98.3%)、検定は符号反転(n<=16は全列挙)。`better`/`worse`は、有効な対がN_MIN_VERDICT(8)以上で、区間が0を含まず、p値が補正後水準(0.05/3)以下の時だけです。それ以外は`inconclusive`(n<8は`insufficient_samples`で区間なし)。これらの値はコード定数で、CLI・環境変数・関数引数から下げられません。

runner imageが混在する場合は層別にできないため、`statistics.mixed_runner_images`を立ててverdictをすべて`inconclusive`にします。入力ありのrunは打切りや一部失敗でも一次解析に含めます。感度分析として「completedのみ」と「score欠測を0点に置換」を併記します(後者は置換値であり一次解析・verdictには使いません)。maxTile等の他指標は探索的で、verdictを付けません。3 seed程度の結果を有意性として主張せず、「Cyclingに勝った」ことを合否にしません。Safety hard ruleと実入力検証、欠測会計が合否です。

## 制約と未実施

- 実測(Windows runner上の8 block実行)は本PRのCIでは行いません。手動実行後にrun URL・artifact名・report SHA256を#567へコメントします。
- headless長枠との統合表示、copy/IO、行動数を揃えた比較は範囲外です(設計書の残件節)。
