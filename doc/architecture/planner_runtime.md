# Planner → Decision と receding-horizon 実行

Issue #567（親 #480）。本実装・比較は旧Python互換Runtimeの継続検証です。P0 #661の製品方針はQt6 Widgets native C++ GUI / Rust resident Core / Python optional AI workerであり、このPRはその新系を実装済みとは扱いません。isolated Chromiumは実験用game backendで、製品GUIのWebViewではありません。versioned coarse command/event/frameRef境界と #664–#667 の移行計画へ引き継ぎ、常駐Python必須へ固定しません。

ゲーム非依存の `PlannerDecisionProvider` は #565 の純粋探索と #566 のrisk/情報価値policyを組み合わせます。実行権限は既存の Candidate Evaluator → Decision Verifier → Action Safety → SafetyGuard / FailSafe / RunController に残ります。

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

Windows比較CLIは各providerの終了直後、成果物保存前に有界JSON要約をflushしてjobログへ残します。初期化・実行・観測・score評価・Stop/旧世代検証の失敗段階、固定理由と元Core理由、返却済み実行結果に基づく入力件数、score OCRの可否、経過時間、Stop/旧世代検証の実施結果を区別します。未実施はunverifiedで、成功へ置き換えません。raw frame・profile・環境設定は要約に含めません。旧Windows比較失敗は成果物の取得制限により詳細原因が未確定で、親Coreの凸平均修正だけで解消したとは扱いません。最新headのWindows実測で再確認します。

診断追加後のWindowsではCycling/Baselineが12入力した後、全文OCRが得点を読めずscore評価で失敗しました。別にPlannerのcompute budget後の保留と、Store照合失敗も記録されました。ログには元/最後に照合したSnapshotの版・期限・clock、capture age、plan/fresh capture/verification/Safety/dispatchの到達時間を有界に追加し、TTL失効・登録差替・clock不一致・Snapshot更新を分けます。診断はStoreを更新せず、期限や照合基準を緩めません。

得点評価は同じ画面の確信度0.65以上のSCOREラベルから数字行を切り出し、3倍拡大・PSM7の数字語彙で読み直せます。2種類の幅で同じ数字を読み、文字矩形がcrop端に接しない場合だけ、倍率・offsetを矩形の両cornerで変換して元frame座標へ戻し、既存strict parserでラベルとの位置と確信度を照合します。BESTや盤面数字を得点の代わりにしません。全文PSMで欠落していた[保存実画像の1824/1808を約0.96の確信度で回復](../validation/planning_score_line_ocr.json)しました。これはLinux OCR再読の証拠で、Windows実入力の合格証拠ではありません。評価専用の値はSource/Planner/学習へ戻さず、判読不可はunknownのままです。短Windows比較も少なくとも1回の認識盤面の変化を必須にし、入力メッセージ送信だけで全noopの試行を合格させません。この比較指標は行動成功の因果ラベルではありません。

Windows workerの間欠的なABSTAIN/完成depth0は、成功率5/6等で解決と見なしません。期待値・本番/test予算・TTLを維持し、失敗assertへ上限2048字のreason/counters/OS制限方式/受領時刻と期限の差を表示します。seed479の比較jobだけは、既存suite通過後にfinal-marker・実盤面checkpointの2回帰を最大20回fail-fast反復し、失敗attemptを保存します。final-markerは固定clockとscripted Popen/IPCで旧checkpoint→finished→最終depth3再読を同じdeadline内に決定的に配置し、実workerの速度には依存させません。実盤面checkpointは実processの2秒予算のままです。他2seedのsuite、32required名、600秒受入れgateは保持します。

c974のWindows stress attempt9では、final-marker fixtureの旧depth1に書き換えられたcheckpointが期限前に受領され、SELECT/compute_budgetでした。nodes48/chance24/leaves24/cache8888は同一pure oracleの実depth2と一致し、最終depth3は期限前に採用されていません。子の407,905,100nsは探索engine開始からの壁時間（先行checkpoint公開待ちも含む）で、spawn前から測る親の1秒予算とは開始点が異なります。受領時の親残予算111,888,400nsだけで、finished未受領、再読時のcutoff跨ぎ、子の予算終了を分離できません。finished_seenは元ログに無く、原因内訳を断定しません。決定的回帰ではworkerの純粋protocol部が同期実行で生成したdepth1/depth3のwire reportを使い、finished受領・最終再読・深さ3を全てassertします。最終再読を除くmutationで1≠3を再現します。実workerの1秒/2秒予算、標準比較400ms、観測TTL、late拒否、浅い完成深さ保持、held-reader20回、native30ケースprobeは変更しません。


追加診断でWindows worker_failedは観測期限やcompute cutoffより前に発生したことを確認しました。子の最初のPython例外だけを固定phase/category/errno/winerrorとして有界に保存し、親は終了codeと最高受領depthを別途記録します。自由な例外文・state・stderrは保存せず、結果型/IPC/OS制限/予算を変更しません。原因が確定するまで間欠失敗を解決済みには扱いません。

Windowsのcheckpoint_publishでerrno13/winerror5、最高受領depth1と十分な残予算を確認したため、raw/JSONをFILE_SHARE_DELETE付きの同じ有界readerへ統一しました。しかし4bcの実Windowsではopen reader中のos.replaceが20回すべて失敗し、共有許可だけでは従来renameのopen-target制約を解消できないことが分かりました。

[MicrosoftのFILE_RENAME_INFORMATION契約](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)に基づき、planning通信だけ、既存の同directory staging/flush/fsync後にFileRenameInfoEx=22、REPLACE_IF_EXISTS|POSIX_SEMANTICS=3で原子的に公開する候補を追加します。既存handleは旧snapshot、以後のopenは新messageを読みます。一般atomic_json、byte/nonce/depth/TTL/Stop/Job契約は変更しません。未対応OS/API失敗はclosedで終了し、先行delete、非atomic fallback、retryはしません。HANDLE所有権/UTF-16 byte長/ABI/旧message保持を回帰し、Linux成功はWindows成功と扱いません。実Windowsのheld-open旧新snapshot20回、既存worker20回、native HANDLE→CRT→streamの30ケースprobeを最新headで検証するまで未確認です。probeが予期されるOSエラーを記録しても、回帰失敗のCI結論は保持し、後段の画像診断を常時試行します。

全文OCRがSCOREラベル自体を見落とすWindows trialには、同frameで認識済み盤面に対応する上段ROIを3倍/PSM6で再読し、literal SCOREだけを回復する評価専用profileを追加します。その後の数字行は2幅一致・端切れ拒否・0.65を維持します。[元2画像と倍率/offsetを含む独立4ケース](../validation/planning_score_header_ocr.json)で1824/1808を回復しました。入力の認識や学習には戻しません。

frame poolの最新lease IDは入力後の生産を保証しません。入力Sourceと評価専用取得は元のNative SystemRelativeTime、host受領時刻、host QPC clock実装を分けて保持し、timestampを現在時刻へ書換えません。settleより前のframeはlease更新/candidate公開より前に捨てます。評価取得もnot_beforeと最後の受理Source時刻の両方を満たすことを要求し、clock不明・future・旧frame・取得失敗はscore=None、Source cache/lease/版を更新しません。OS送信境界の時刻はWMのキュー投入を表す監査値で、ゲームの処理完了ではありません。

新しいWindows短比較だけ、capabilityを確認した既存C ABIのFAST_CV前処理を明示注入し、Sourceが所有するNative contextを終了時に閉じます。既定CampaignはPython前処理のままです。開始時に同じ実画素のmean/hash/明領域成分、候補、意味state、Baseline判断、Planner入力の同値を検査し、不一致なら新profileを合格させません。検証に時間を使った後は新しい実frameで再観測し、TTLを延ばしません。DLLのSHA256/ABI/capabilityと各取得のWGC/GDI・fallback理由を記録します。[Linux C++の同画素fixture検証](../validation/planning_native_frame_parity.json)は全比較が一致しましたが、Windowsの実証とは別です。実DLLのhash一致も3provider比較の条件にします。WGCの既定2000ms待機は変更しておらず、TIMEOUT→GDIかの実証を確認してから待機profileを検討します。e6c7の全9試行はPython前処理の別headであり、この新Windowsprofileの測定として流用しません。

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

Browser比較CLIの`--planner-depth`（既定1）/`--planner-wall-ms`（既定400）は共通SearchBudgetの有限validationを再利用し、要求depth/予算と各手のcompleted_depthをreportへ残します。参照factoryの既定depth2は維持します。この旧cohortのBrowser/headless比較はdepth1の実験profileであり、後段のe6c7既定depth2実測とは区別します。

trace参照はstable event ID・内容SHA256・status/`actual_outcome:false`・版/期限を保持します。予測eventを同じRun guard内で先に保存し、成功してからhistory/traceを公開します。保存失敗時は入力0・dangling参照0、後続trace保存失敗時の孤立予測もactualFalseの別eventのままです。close後の再読・digest一致・typed result復元、full/refの同じ5入力でdecision/receipt/event数一致を回帰で確認しています。

長枠では256tileのglowがROIを広げて1セルをunknownにした認識失敗を発見しました（#659）。停止したtrialは成功比較へ混ぜません。比較Sourceはcapture開始時に前state/observation/cache/PNGを消し、失敗後も旧boardを現在と扱いません。各実行結果受領時にaction・dispatch参照・認識済みbefore boardを「観測仮説/真値でない」として記録し、post-observation pending/完了を更新します。欠落completionを明示し、error時も判断終了後のbounded score評価を試み、不可はnullです。閉じた失敗Journalは既存ArtifactStoreへ最大32MiBで保存し、参照hashを記録します。保存失敗が元の失敗を置き換えません。

## 実画面の失敗を含む固定cohort

[bb864886 / Journal・1000ms settle・3provider×3seed上限180手の全結果](../validation/planning_browser_journal_bb864.json)はsource/hash/dirtyが両端一致しました。Cycling score1332/1140/1256（全終局、無効7）、Baseline664/1344/1448（全終局、無効15）。Plannerは479で135手/134遷移照合、480で131手/130照合の後に256tile glowによるunknown cellで停止し、481だけ180手上限/score2312/max256/180照合/無効0/Stop後送信0でした。**Planner失敗2/3を落とした平均を作りません。** 修正は#659へ追跡し、修正版は別SHA/別cohortで測定します。

この旧helperの失敗trialではcapture例外後の前boardがlast_stateへ残り、terminal未検証flagと途中scoreが不十分でした。旧値を成功の根拠に使わず、元primaryerror・入力数・完了数・最終画像を保持します。新helperの前state消去/未完action保存/evaluation-only score/null/Journal参照化による記録改善を、旧cohortへ遡って実測したとは扱いません。

fresh callback前と評価取得の双方で、clock整合に加えて既存のCAMPAIGN_OBSERVATION_TIMEOUT_SECONDS（5秒）未満の実capture ageを要求します。settle以後でも18秒古いframeは受理せず、lease/候補/学習を更新しません。解析後のcache公開直前にも同じ5秒を再検査し、解析中に失効した候補・Source連番・lastAcceptedを公開しません。新しいage TTLを増やしたり、元の生産時刻をhost受領時刻に置き換えたりはしません。

Windows実ログではQPC実装が一致していてもWGC生産時刻がhost受領より742500ns未来のframeを確認しました。時刻許容幅を広げず、WindowsScreenCaptureの既存契約違反経路でそのWGC frame/sessionを破棄し、同じ対象windowからGDIで新規取得します。元WGC時刻は書換えず、GDI frameにも通常のclock/barrier/age検査を適用します。reportのbackend/fallback_reasonで区別し、GDI取得をWGC成功の証拠にしません。

画像artifactを取得できない場合の失敗診断は、`--save-failure-evidence`を明示した専用Reference episodeに限ります。既に評価した同frameの上段headerを最大640×240/PNG128KiB・1episode1枚・4096文字chunkでstdoutへ出し、frame/PNG SHA・size・index/countで再構成を検証できます。launcherの生存PID、Source HWND、現在の実TargetStateを再照合し、不明なら画像を出しません。診断ROIは元OCR ROI中心の上段全体で、元OCR ROIを別metadataに保持します。OCR本体のcrop、再capture、Source cache/lease、入力やfocus操作は変更しません。

直近4captureだけ、寸法・WGC/GDIとBitBlt/WM_PRINT・生産/受領時刻・画素hash・board bbox/意味signatureを保持します。明示比較profileではread-onlyな現在window/client geometry、DPI、選択PIDとforeground HWND/PIDの一致を添えます。これはその時点の診断sampleで、入力許可や原子的な所有証明へ転用しません。画像比較は依存install/native build成功時にunit失敗後も独立して走らせ、unitの失敗をcontinue-on-errorで成功へ変えません。既存600秒gateと32required名を維持します。

## 修正後の既定depth2実画面cohort

[e6c7b66の固定sourceで3provider×3seedを実行した全結果](../validation/planning_browser_depth2_e6c7.json)は、初期/終了source hash・clean状態が全試行で一致しました。各seedを独立processで並列実行し、4CPUのcloud quotaを共有しました。同じ1000ms settle・LCG seed479/480/481・180手上限で、Cycling score1332/1140/1256（平均1242.67）、Baseline664/1344/1448（平均1152）、Planner2268/2244/2268（平均2260）でした。最大tileはPlannerの3試行すべて256です。

全1337入力はunique receipt・明示完了観測・盤面遷移照合の件数と一致し、全9試行で画面からのmerge reward合計と判断後の独立visible scoreが一致しました。Stop後送信・mismatch・unverified・cleanup errorは0。noopはCycling7/Baseline15/Planner0です。Planner540手は519手でdepth2、残り21手で予算内に完成したdepth1を使いました。要求depth2/400msを固定し、既定を下げて失敗を隠していません。

Planner全試行が180手上限で生存したためsurvivalは打切りで、勝利ではありません。3seedの記述的な結果であり、統計的有意性、他ゲーム、未校正world riskの解決、Windowsでの優位性を主張しません。decision p50はPlanner1475～1525ms、p95は1595～1618msで、1000ms settleと共有CPU負荷を含み、探索単体やisolated性能ではありません。CPU/RSSもhost processだけでbrowser/workerを除く測定です。旧depth1の失敗cohortを保存したまま、#659 ROI修正とCore数値修正を含む別headの結果として扱います。以後のWindows専用score再読・診断追補はこの固定source実測へ遡って適用したとは扱いません。

## 参照化の限定cost検証

[ee2dbe4の同一入力copy fixture](../validation/planning_trace_reference_cost_ee2.json)はsource hash両端一致・dirty0、2world/2action/32chance atom×180recordで、予測本文を含むcache deepcopy p50 102.93ms / p95 105.88ms / peak allocation14,986,776bytes、参照形式 p50 7.17ms / p95 9.22ms / peak1,086,400bytesでした。serialized cacheは6,359,760→368,280bytes。これは固定データのcopy costで、実プレイ全体・予測計算・Journal書込・hash生成の総latency改善ではありません。長枠全9episodeの最適化後latencyを実測したとは主張しません。

非SELECTで停止した試行も、その時点のlast Core reasonとboundedな完全planning auditを保存します。探索未実施のstepでは前stepのCore resultを現在と扱いません。正常な保留でも診断Journalを既存ArtifactStoreへ残し、policyのplanning_unavailableだけでbudget/model/quality等の元理由を失わないようにします。

## e6c7の追加Headless depth2 fixture比較

[原本hash付き全9試行記録](../validation/planning_headless_depth2_e6c7.json)。

原本hash `4e785fb88a159cc8e10a1d8b99d421c3e3bb1f01bd51e9d3287de600b7641a1c`、source `e6c7b66a886fe35f1dba9b0d2bec6e04514df02d` の両端clean/hash・EventJournal receipt・Stop拒否と実プロセスexit0を確認した。画像認識やOS実入力ではないmodel-state fixtureである。Python Random(seed)はBrowserのLCGと異なり、同seedでもtrajectoryやscoreをbackend間で同一視しない。

| provider | seed479/480/481得点 | 平均 | fixture操作合計 |
|---|---|---|---|
| cycling | 1472 / 728 / 1672 | 1290.67 | 430 |
| baseline | 1696 / 2036 / 1876 | 1869.33 | 540 |
| planner | 2004 / 2264 / 2280 | 2182.67 | 540 |

depth2/400msと共通125ms pacingを維持した。n=3の記述的結果で、打切り・unknown world/conditional仮定・資源scopeを保つ。実画面e6c7の9試行や旧eb59のdepth1 fixtureとは別測定として扱い、Windows受入れや全ゲームの攻略精度へ一般化しない。

## Windows 5bbc診断と短比較profile

[5bbc失敗cohortの有界ログ要約](../validation/planning_windows_5bbc_diagnostic.json)を保存します。push run41338659005のseed479/480/481ではplanning publicationの旧handle維持・新規open=newを20回、native30ケースを実OSで確認しました。readerのdelete共有だけで従来os.replaceが失敗した旧runも保持します。別head c974のPR seed479ではstress attempt9でSELECT/compute_budget・completed_depth1が深さ3期待を満たさず、アクセス拒否解消を全gate成功とは扱いません。

5bbc seed481（job124943766762）のCycling/Baselineは各12実入力・12盤面変化でしたが、score評価がunknownのため試行は失敗です。検証済み同frame headerにはSCORE48等が見え、局所数字cropのpanel外縁を含むOCRが端切れ/非数値を返しました。閾値を下げず局所処理を改善する対象です。Plannerは0入力で、初回WGC892×642からtimeout後GDI904×648へ切替わり、認識盤面の意味signatureは同一でもraw signatureは異なるため正しく再観測しました。その後GDI画素は同一でも、各WGC timeout約2秒により3秒の再観測予算で保留しました。

この測定に基づき、独立の短比較だけWGC待ちを250msと明示し、timeout時は既存の同一対象GDI fallbackとsession再利用を使います。一般capture/600秒Campaignのnative既定2000msを変えず、raw署名照合・clock・5秒capture age・Planner4秒TTL・3秒再観測予算も維持します。新profileのWindows gameplay成功は次の実CIで確認し、5bbcやe6c7の成功値へ遡って適用しません。前処理parityでは両semanticが未知でもmatch=falseを維持し、availability/一致/未知cell数/確信度/終了flag/候補数を分けて診断します。初期だけ、両経路が同じ未知入力なら最大2回の実fresh captureを既存45秒ready期限内で追加し、available=trueかつparity成立を入力開始条件とします。真の前処理差、未知のまま、期限切れは入力0の失敗で、cacheを新frameとして付け替えません。

### 評価専用Score panelの前処理

Windowsの実header9件では、SCORE文字を読めても数値cropにpanel外縁・page背景が混ざり、PSM7が全幅を文字に見立てて拒否されました。旧試行の失敗結論は保全します。評価だけ、同frameのSCOREアンカーに属する局所panelをOtsuのクラス間分散最大化と4近傍連結成分で分離します。背景が矩形の過半数を支持し、ROI端に接しないpanelだけを使い、BEST側を含めません。bilinear3倍で低解像度字形の階調を保持した後Otsu二値化し、白余白を加えてPSM6/7へ渡します。これは[Otsu (1979)](https://doi.org/10.1109/TSMC.1979.4310076)の大域閾値法を小さい局所ROIへ適用したもので、新しい学習器ではありません。

literal SCOREはOCRで確認し、ラベルが不明なら値もunknownです。数値はpanel実幅を覆う異なる2幅で一致し、confidence0.65・厳密numeric parser・元support上の端切れ拒否を維持します。padding前の元source stripと、丸角を除くpanelの2px内側の支持領域で、foregroundがいずれかの境界へ触れた場合も拒否します。追加した白余白はOCR bbox検査前に座標から差し引き、切れた字形を隠しません。ラベルが偏心しても初回cropがpanel両端を覆う幅を使い、2回目は1.5倍の異なる実ROIとします。clampせず、同一ROIになれば独立の2回として数えずunknownです。新しい外部依存・Pillow runtimeはなく、局所画素は最大100,000、panel候補は最大4に制限します。Source/入力OCR/Planner/TTL/Stop/学習へ戻さない評価専用処理です。

[実画素回帰と費用](../validation/planning_score_panel_ocr.json)は、元cropのPNG SHA、904×648への(202,0)配置、9件×原座標/倍率2+offset、元1824/1808の再読を区別します。元画像のScoreとBest、0、多桁を使い、値・seed別の条件はありません。実Windows比較の最終成功やOS入力の完了をこのreplayから主張しません。
