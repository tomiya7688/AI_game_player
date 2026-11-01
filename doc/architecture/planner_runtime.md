# Planner → Decision と receding-horizon 実行

Issue #567（親 #480）。ゲーム非依存の `PlannerDecisionProvider` は #565 の純粋探索と #566 のrisk/情報価値policyを組み合わせます。実行権限は既存の Candidate Evaluator → Decision Verifier → Action Safety → SafetyGuard / FailSafe / RunController に残ります。

`prepare_planning_context` は実観測、固定候補実体、Run世代を同じDecision Contextへ束縛します。Sensor Adapterが構成したBeliefのsource/frame/action epochとsnapshotを照合し、policyのSELECTだけを既存ActionDecisionへ変換します。OBSERVE_MORE / ABSTAIN / ESCALATEは `DecisionDisposition` と独立したtraceであり、偽ActionIDや実行receiptを作りません。Baselineの保留も同契約でSessionを正常停止できます。ESCALATEは要求の記録だけで、別Model/APIへの通信を自動開始しません。

`DecisionOrchestrator` はProviderの外で実Sourceの `read_fresh()` を最大2回・総3秒まで要求します。同じcachedFrame、別Source/世代、逆行時刻を再観測として認めません。有限I/Oの後にも総期限を検査し、期限後は入力しません。純粋なpolicyのOBSERVE_MOREは#566の校正channelと実行可能な情報価値を必要とします。入力前に実状態が変化した際の安全のための再取得は、VOIが正だった証明とは区別します。

SELECTの実入力には追加captureが必要です。元planに対して新Frameを証明し、source/turn/semantic state digest、選択候補の全フィールド、screen IDとraw signatureを再照合します。同じaction epochでもゲームが動いた場合は再計画します。VerifierとSafetyを新画面で再評価し、TRUSTでも未解決verification_requestsは入力を止めます。backend直前とWindowsの実OS押下/カーソルAPI直前に期限とRun世代を再確認します。解放APIは期限後もowned held入力を解放できます。Adapterの`current_belief(context)`を探索中・結果受領後・fresh照合・送信前に照会し、画素が同じでも外部writerの仮説版/unknown質量/校正版変更を拒否します。trusted登録値は不変に固定し、公開mappingへの登録差替も旧planを失効させます。hot reload機構は追加しません。同期captureと入力の間に任意のゲームが動かないという保証ではなく、取得できた最新観測と有限leaseに基づく保守的な境界です。

正常な実行receiptだけをepisodeへ一回ackし、次の一手には入力前captureより新しいpost-action実観測を要求します。dispatch_unknownは成功とせず、再観測境界だけを進めます。dry-run、未送信、判断だけではaction epochを進めません。明示post-action completionにも鮮度条件を適用し、cached入力前Frameで実Outcomeを完成させません。最終操作は次判断が無くても既存#628の明示完了APIで観測できます。

予測の値・terminal・riskはJournalの独立した `planning.prediction` event typeに保存し、traceの `planning_prediction` は同snapshotのevent ID・版・期限への小さな参照にします（Journal無しのlegacyでは本文を保持）。これにより履歴のcopy/patchへ予測本文を重複させません。`actual_outcome: false` を付けます。Providerは `assess_outcome` を公開・委譲しません。実Outcomeは #628 のreceiptとpost-action観測だけから生成します。仮想勝利を実際の成功へ投影しない負例があります。

OFF / 未登録Sensor能力の場合だけBaselineへ戻ります。失効、古いsource、risk保留、未校正ABSTAINをfallbackで無視しません。既定GUIのProviderは変更していません。trusted hostから任意ProviderとSensorを構成できます。

## 任意2048 profile

`reference_2048_planner_provider(allow_conditional=False)` は参照用factoryです。既存の画面Recognizerが生成した `features.game_2048` を `Reference2048PlanningObservation` がtyped stateへ検証します。DOM、内部GameState、実ゲームの乱数を判断へ渡しません。単一認識盤面はRELATIVE_SCOREであり、認識confidenceをworld確率に変えません。global unknown massは1のままです。未校正状態の既定policyはABSTAINです。

明示的な `allow_conditional=True` はSearchBudgetとRiskPolicyの両方で、単一認識worldを仮定した実験選択を有効にします。global world riskは未知と記録し、校正済み安全性や汎用ゲーム精度を主張しません。既定budgetはdepth2/node10000/400ms/worker256MiB、観測TTL4秒です。TTLとCSS settlingの実験profileは、既存物理Safety lease5秒やWindows600秒合格条件とは別です。

Windows Campaignの任意比較は `python tools/run_2048_campaign.py --decision-provider planner --planner-conditional --planning-settle-ms 900 --profile pr`。Cyclingが既定であり、既存Windows gateを置換しません。Baseline比較は `--decision-provider baseline`（min_margin0 / min_recognition.65の明示比較profile）。固定版のtile appearanceに加えscore additionのanimationがあるため、比較Sourceは実操作後の有限settleを使用します。raw signatureがなお変われば拒否し、盤面一致を理由に#628の照合を緩めません。Windows実測は別の証拠が必要です。

## Method Fidelity Matrix

| 方法・元要件 | 実装との対応 | 境界・残件 |
|---|---|---|
| Belief / 複数world | 型付きsnapshot、scoreと校正確率を分離、source/frame/turn/unknownを保持（#563/#564） | 一般ゲームの認識校正は未達。単一画像の確実性を仮定しない |
| Bounded Expectimax / iterative deepening | #565登録model・legal actions・qualityを制限workerで評価 | 任意hidden worldの未来状態を完全観測と扱わない。#565の観測条件契約を継承 |
| Anytime / iterative deepening / bounded cache・pruning | #565の完全深さだけを採用しtime/node/branch/RAMを強制 | #409 marginal gain / #425全体budgetへの自動接続は未達、受領済み見積だけを#566で評価 |
| Lower-tail risk / disagreement / VOI | #566のCVaR、unknown区間、EVSIと観測後mappingの実行可能性 | channel無しでEVPI上界だけから再観測を正当化しない。Unknownness全体は#206 |
| Receding-horizon / MPC | 実入力は最初1手のみ、receipt後の新実観測で再構成 | 事前の仮想Action列をbackendへ送らない。本Issue |
| 6軸qualityとconfidence | #491の版付きNumericQualityを探索契約で共有 | 実測actualとcandidate仮説を分離。欠落軸を確率へ使わない |
| Safety / causal experience | 既存Verifier/Safety/Run世代と#628 receiptを維持 | generic因果providerが無い画面差はunknown。#422/#433の残件を維持 |
| POMCP / DESPOT / POMCPOW / PETS / MuZero、latent world model一括学習 | 後続交換方式・非対象 | #480に残し、正式採用時のみ実装単位を追跡。全方式を実装済みと呼ばない |
| Headless / Live / resource比較 | 同seed・同modelの計測と実画面を分ける | Chromium実画面とWindows WGC/OS Campaign600秒は別判定。#247/#250 |

Receding-horizonの参照: Mayne et al., *Constrained model predictive control: Stability and optimality*, Automatica 36 (2000), DOI [10.1016/S0005-1098(99)00214-9](https://doi.org/10.1016/S0005-1098(99)00214-9)。ここではその安定性定理の条件を満たした証明を主張せず、最初の一手を実行して実観測で再計画する処理原則を採用します。Risk/情報価値の出典と実装条件は[共通policy資料](planning_risk_information.md)を参照してください。

## 検証とcost

接続checkpointでは全1,224 tests（45環境skip）、ruff/mypy/coverage/compile/bug/runtime-boundary/performance/workflow-security/required29構成を通過しました。OS admissionとBelief現行照合を除いた場合に新規回帰がFAILすることも確認しています。初期回帰は非SELECTの無入力・cached/別source拒否・有限再観測、state/signature/候補変更、Stop/rearm、失効、dry-run、未解決Safety要求、仮想terminalと実unknownの分離を確認しています。実探索、seeded比較、p50/p95・CPU/RAM/nodesとWindows実測の結果は最終統合時に追記します。未測定を成功扱いにしません。

Pythonの小さな接続層は1step毎で、画像bufferや木をコピーせずbounded参照だけを扱います。探索のtime/node/RAM強制は#565のworkerに置き、OS入力/停止は既存Native境界を維持します。接続層だけをC++へ移してもcapture/認識/worker IPCの支配的costを減らさないため、現在のPython compositionへ配置しています。

過去条件350msの[Baseline実画面比較記録](../validation/planner_baseline_350ms.json)は、f145 production・seed479/480/481でscore664/1344/1448、actions92/148/153、max64/128/128です。393件すべて実行receiptと明示観測があり、実Outcomeは全unknownです。平均1152（既存Cycling1242.67）で改善を示しません。新しい1000ms settleでの同条件比較とは分け、n=3の統計的有意性を主張しません。

Windows比較は既存language CIに独立jobとしてseed479/480/481を並列実行します。各jobはCycling/Baseline/Plannerの各12手上限、最低3件の実入力とStop/旧世代拒否を要求します。600秒耐久gateはそのまま残ります。test-only HTTP response wrapperで固定seed LCGを初期化し、pinned index、init script、実際の応答のSHAを別々に保存します。固定版assetは変更しません。得点は全判断後の別capture→OCR評価だけで読み、Source/Plannerへ戻しません。認識不可は比較失敗です。CPU/RAMは50msのprocess-tree sample（browser/workerを含む）を保存し、短命workerや終端CPUの欠落、共有RSSの二重計上がある測定範囲を明示します。これは#565のOS hard resource enforcementとは別です。Windows actual実測とCI合格が確認できるまで本IssueのLive条件はUnverifiedです。

再現CLI: `tools/compare_2048_planning.py` はpublic boardのheadless fixture、`tools/compare_2048_browser_planning.py --allow-live-input` は明示的なisolated Chromium実画面、`tools/compare_2048_windows_planning.py --allow-live-input --seed 479 --max-steps 12` は所有WindowのWGC/OS入力です。Chromiumには任意開発依存Pillow/Playwright、Windows比較にはOCRとpsutilが必要です。どの結果も試行ごとのreportへ固定し、live判定とfixture判定を混同しません。

5199f479の[Chromium短試走](../validation/planner_smoke_5199.json)ではPlanner12手、12unique receipt、12予測遷移一致、score52/max16、Stop後送信0、実Outcome12件すべてunknownを確認しました。sourceの開始/終了hashは同一です。これは旧direct-Pipeline JSON経路の短試走で、最終Journal比較とは別条件です。

長枠準備で、直接Pipelineを構成したfixtureが旧trace全JSONを書き換える経路だったことを確認しました。[旧経路の診断](../validation/planner_legacy_diagnostic.json)だけを保存してheadless旧測定を中断し、3provider共通で明示EventJournalへ切り替えました。旧測定の未完結果を最終比較へ混ぜません。Journal/PipelineをTempdir削除前に閉じ、constructor/cleanup失敗を元エラーから分離して保存します。開始/終了にsrc/tools全Pythonのaggregatehashとhelperhashを記録し、途中にsourceが変化した測定は成功扱いにしません。

比較laneのlanguage CIはworkflow＋event＋ref単位でobsolete runをcancelします。pushとpull_requestの現行headを別groupに保ち、既存600秒gateと追加3seedの計32required contextを保持します。

## 固定seed headless比較

[eb59ba5 / Journal・125ms共通周期の9episode記録](../validation/planning_headless_journal_eb59.json)。source/hash/dirtyは開始・終了で一致しました。Python Randomのcommon streamを合法移動ごとに使用し、ChromiumのLCG familyとは別実験です。各providerの探索/選択条件を固定し、最適化前の重複予測traceを含む条件で測りました。

| Provider | seed | 操作数 | score | 最大tile | 無効操作 |
|---|---:|---:|---:|---:|---:|
| cycling | 479 | 156 | 1472 | 128 | 2 |
| baseline | 479 | 180 | 1696 | 128 | 9 |
| planner | 479 | 180 | 2268 | 256 | 0 |
| cycling | 480 | 98 | 728 | 64 | 1 |
| baseline | 480 | 180 | 2036 | 256 | 10 |
| planner | 480 | 180 | 2008 | 128 | 0 |
| cycling | 481 | 176 | 1672 | 128 | 9 |
| baseline | 481 | 180 | 1876 | 128 | 12 |
| planner | 481 | 180 | 2264 | 256 | 0 |

平均scoreはCycling1290.67、Baseline1869.33、Planner2180でした。Plannerはseed480でBaselineを下回り、3seedで統計的有意性や一般ゲームの強さを主張しません。Baseline/Plannerは全episodeで180手上限に達したため、生存手数は打切りです。計1510件のfixture実行receiptは全件操作数と一致し、Planner540判断はconditionalのglobal risk未知を保持しました。policy再観測0と入力直前fresh captureは区別しています。

この旧trace重複条件で、各episodeのdecision p50の平均はCycling56.14ms / Baseline68.44ms / Planner203.76ms、3episode中の最大p95は127.65 / 133.50 / 369.80msです。125ms共通pacingはdecision latencyから除き、episode wallに含めます。親CPU合計は25.58 / 38.24 / 84.50秒、Plannerの待機済みworker CPU合計32.23秒。親RSSはprocess lifetime high-water（最大50,508KiB）でepisode差分ではありません。最終trace参照化の全9episode latency改善は未測定とし、同じ入力の意味一致回帰と独立copy cost fixtureで評価します。

Browser比較CLIの`--planner-depth`（既定1）/`--planner-wall-ms`（既定400）は共通SearchBudgetの有限validationを再利用し、要求depth/予算と各手のcompleted_depthをreportへ残します。参照factoryの既定depth2は維持します。現在保存済みのBrowser/headless比較はdepth1の実験profileであり、factory既定depth2の実測coverageとは区別します。

trace参照はstable event ID・内容SHA256・status/`actual_outcome:false`・版/期限を保持します。予測eventを同じRun guard内で先に保存し、成功してからhistory/traceを公開します。保存失敗時は入力0・dangling参照0、後続trace保存失敗時の孤立予測もactualFalseの別eventのままです。close後の再読・digest一致・typed result復元、full/refの同じ5入力でdecision/receipt/event数一致を回帰で確認しています。

長枠では256tileのglowがROIを広げて1セルをunknownにした認識失敗を発見しました（#659）。停止したtrialは成功比較へ混ぜません。比較Sourceはcapture開始時に前state/observation/cache/PNGを消し、失敗後も旧boardを現在と扱いません。各実行結果受領時にaction・dispatch参照・認識済みbefore boardを「観測仮説/真値でない」として記録し、post-observation pending/完了を更新します。欠落completionを明示し、error時も判断終了後のbounded score評価を試み、不可はnullです。閉じた失敗Journalは既存ArtifactStoreへ最大32MiBで保存し、参照hashを記録します。保存失敗が元の失敗を置き換えません。

## 実画面の失敗を含む固定cohort

[bb864886 / Journal・1000ms settle・3provider×3seed上限180手の全結果](../validation/planning_browser_journal_bb864.json)はsource/hash/dirtyが両端一致しました。Cycling score1332/1140/1256（全終局、無効7）、Baseline664/1344/1448（全終局、無効15）。Plannerは479で135手/134遷移照合、480で131手/130照合の後に256tile glowによるunknown cellで停止し、481だけ180手上限/score2312/max256/180照合/無効0/Stop後送信0でした。**Planner失敗2/3を落とした平均を作りません。** 修正は#659へ追跡し、修正版は別SHA/別cohortで測定します。

この旧helperの失敗trialではcapture例外後の前boardがlast_stateへ残り、terminal未検証flagと途中scoreが不十分でした。旧値を成功の根拠に使わず、元primaryerror・入力数・完了数・最終画像を保持します。新helperの前state消去/未完action保存/evaluation-only score/null/Journal参照化による記録改善を、旧cohortへ遡って実測したとは扱いません。
