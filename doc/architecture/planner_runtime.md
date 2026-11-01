# Planner → Decision と receding-horizon 実行

Issue #567（親 #480）。ゲーム非依存の `PlannerDecisionProvider` は #565 の純粋探索と #566 のrisk/情報価値policyを組み合わせます。実行権限は既存の Candidate Evaluator → Decision Verifier → Action Safety → SafetyGuard / FailSafe / RunController に残ります。

`prepare_planning_context` は実観測、固定候補実体、Run世代を同じDecision Contextへ束縛します。Sensor Adapterが構成したBeliefのsource/frame/action epochとsnapshotを照合し、policyのSELECTだけを既存ActionDecisionへ変換します。OBSERVE_MORE / ABSTAIN / ESCALATEは `DecisionDisposition` と独立したtraceであり、偽ActionIDや実行receiptを作りません。Baselineの保留も同契約でSessionを正常停止できます。ESCALATEは要求の記録だけで、別Model/APIへの通信を自動開始しません。

`DecisionOrchestrator` はProviderの外で実Sourceの `read_fresh()` を最大2回・総3秒まで要求します。同じcachedFrame、別Source/世代、逆行時刻を再観測として認めません。有限I/Oの後にも総期限を検査し、期限後は入力しません。純粋なpolicyのOBSERVE_MOREは#566の校正channelと実行可能な情報価値を必要とします。入力前に実状態が変化した際の安全のための再取得は、VOIが正だった証明とは区別します。

SELECTの実入力には追加captureが必要です。元planに対して新Frameを証明し、source/turn/semantic state digest、選択候補の全フィールド、screen IDとraw signatureを再照合します。同じaction epochでもゲームが動いた場合は再計画します。VerifierとSafetyを新画面で再評価し、TRUSTでも未解決verification_requestsは入力を止めます。backend直前とWindowsの実OS押下/カーソルAPI直前に期限とRun世代を再確認します。解放APIは期限後もowned held入力を解放できます。Adapterの`current_belief(context)`を探索中・結果受領後・fresh照合・送信前に照会し、画素が同じでも外部writerの仮説版/unknown質量/校正版変更を拒否します。trusted登録値は不変に固定し、公開mappingへの登録差替も旧planを失効させます。hot reload機構は追加しません。同期captureと入力の間に任意のゲームが動かないという保証ではなく、取得できた最新観測と有限leaseに基づく保守的な境界です。

正常な実行receiptだけをepisodeへ一回ackし、次の一手には入力前captureより新しいpost-action実観測を要求します。dispatch_unknownは成功とせず、再観測境界だけを進めます。dry-run、未送信、判断だけではaction epochを進めません。明示post-action completionにも鮮度条件を適用し、cached入力前Frameで実Outcomeを完成させません。最終操作は次判断が無くても既存#628の明示完了APIで観測できます。

予測の値・terminal・riskはtraceの `planning_prediction` に保存し、`actual_outcome: false` を付けます。Providerは `assess_outcome` を公開・委譲しません。実Outcomeは #628 のreceiptとpost-action観測だけから生成します。仮想勝利を実際の成功へ投影しない負例があります。

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
| Lower-tail risk / disagreement / VOI | #566のCVaR、unknown区間、EVSIと観測後mappingの実行可能性 | channel無しでEVPI上界だけから再観測を正当化しない。Unknownness全体は#206 |
| Receding-horizon / MPC | 実入力は最初1手のみ、receipt後の新実観測で再構成 | 事前の仮想Action列をbackendへ送らない。本Issue |
| 6軸qualityとconfidence | #491の版付きNumericQualityを探索契約で共有 | 実測actualとcandidate仮説を分離。欠落軸を確率へ使わない |
| Safety / causal experience | 既存Verifier/Safety/Run世代と#628 receiptを維持 | generic因果providerが無い画面差はunknown。#422/#433の残件を維持 |
| POMCP / MuZero / 深層探索 / game攻略最適化 | 非対象 | #480の非対象を実装済みと呼ばない |
| Headless / Live / resource比較 | 同seed・同modelの計測と実画面を分ける | Chromium実画面とWindows WGC/OS Campaign600秒は別判定。#247/#250 |

Receding-horizonの参照: Mayne et al., *Constrained model predictive control: Stability and optimality*, Automatica 36 (2000), DOI [10.1016/S0005-1098(99)00214-9](https://doi.org/10.1016/S0005-1098(99)00214-9)。ここではその安定性定理の条件を満たした証明を主張せず、最初の一手を実行して実観測で再計画する処理原則を採用します。Risk/情報価値の出典と実装条件は[共通policy資料](planning_risk_information.md)を参照してください。

## 検証とcost

接続checkpointでは全1,224 tests（45環境skip）、ruff/mypy/coverage/compile/bug/runtime-boundary/performance/workflow-security/required29構成を通過しました。OS admissionとBelief現行照合を除いた場合に新規回帰がFAILすることも確認しています。初期回帰は非SELECTの無入力・cached/別source拒否・有限再観測、state/signature/候補変更、Stop/rearm、失効、dry-run、未解決Safety要求、仮想terminalと実unknownの分離を確認しています。実探索、seeded比較、p50/p95・CPU/RAM/nodesとWindows実測の結果は最終統合時に追記します。未測定を成功扱いにしません。

Pythonの小さな接続層は1step毎で、画像bufferや木をコピーせずbounded参照だけを扱います。探索のtime/node/RAM強制は#565のworkerに置き、OS入力/停止は既存Native境界を維持します。接続層だけをC++へ移してもcapture/認識/worker IPCの支配的costを減らさないため、現在のPython compositionへ配置しています。

過去条件350msの[Baseline実画面比較記録](../validation/planner_baseline_350ms.json)は、f145 production・seed479/480/481でscore664/1344/1448、actions92/148/153、max64/128/128です。393件すべて実行receiptと明示観測があり、実Outcomeは全unknownです。平均1152（既存Cycling1242.67）で改善を示しません。新しい1000ms settleでの同条件比較とは分け、n=3の統計的有意性を主張しません。
