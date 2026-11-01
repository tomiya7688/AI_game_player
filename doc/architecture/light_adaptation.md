# Inference + Light Adaptation runner (#422)

`RuntimeComposition.create_session_runtime` は実 `DecisionPipeline` に
`SessionLightAdaptation` を接続する。既定Modeは #421 の
`INFERENCE_WITH_LIGHT_ADAPTATION`。通常のOutcome/Safety/VerifierはModeで無効化しない。
旧custom factoryの互換runtimeはそのfactoryが宣言した機能を維持する。

## 操作と更新の境界

1. Sessionはpending Modeを安全境界で適用し、検証済み候補があれば短いmemory CASで公開する。
2. `begin_step(artifact_version)` がimmutable版を固定する。Engineは#531 Adapterを一度適用する。
3. receiptは#628の実送信・同世代の観測だけ。判断のみ/dry-run/dispatch不明は因果学習しない。
4. 操作終了後はbounded `put_nowait`。workerは新deltaと数値Utility実測だけを処理する。
5. workerは既存#530 BuilderのBeta推定・EWMA・重複排除で次候補を作り、#532 Gateの
   **light-validation/v1** で実検証する。paired replayを実施したと記録しない。
6. inactive候補とreportを保存した後、次Step境界で `EVALUATE → PROMOTE`、epoch CAS、
   cached pointer交換、authorization receiptの順に処理する。disk/replayはMode lock外。
7. 成功receiptだけをworkerがdurable pointer/history/auditへackする。ack未完了中は次公開禁止。
   ack失敗でも現在のvalid immutable版で推論を継続し、学習admissionを閉じる。
   `retry_checkpoint()` は明示recoveryであり、成功前は更新を再開しない。

Mode CAS失敗の候補はauthorizationを持たず、再起動時の `capture()` から採用されない。
成功CAS後のStopとdurable ackの順序が逆でも、ackは既に受理した公開のcheckpointに限定する。
保存失敗前のdurable版・Gateのrollback履歴を維持する。既存のoffline対照評価昇格APIも残る。
起動時のKnowledge破損/保存失敗は学習admissionだけを閉じ、静的推論を継続する。破損pointerを
暗黙に上書きせず、修復後の新Sessionで再検証する。

## 最小証拠と限定された公開方針

light Gateは外部の `passed=True` を受け取らない。最大64のcanonical eventから既存Builderで
再構成し、新しいverified success/no_effect/failureが最低4件、uncertainty ≤ .9、scope/期限/
schema/time/byteを検査する。unknownは最低件数に含めず、混在batchの既知証拠を保持する。
数値priorは既存値中心の±.25へ射影するtrust regionを使い、統計件数・unknown比率は改変しない。
更新規則版には `.light1` を付ける。weight/threshold/ROI/transition/reliabilityの変更はこの軽量
公開方針では拒否する。候補除去・Safety・input権限・Model重みの更新は行わない。
同じreport artifactに最大64件/512 KiBの数値canonical event本体とbatch SHAを保持する。
scope/source/receipt世代/action連番を含み、元Snapshotと同じBuilder/policyへ再投入すると
source-range hashを含むSnapshot SHAが一致する。raw artifact、自由文、actor/model metadataは
受理境界・checkpoint読書き・Gateで共通validatorにより拒否する。artifact全体は1 MiB上限。

これは「検証済みの限定的統計更新」の検査であり、汎用プレイ成果改善や全回帰群の合格証明ではない。
大きい変更には#532の実Providerによるpaired replay、または後続の評価体系を使う。

## ゲームに依存しない識別と因果証拠

`LearningIdentityPolicy` の既定は `DISABLED`。未知providerのframe内IDを恒久IDとみなさない。
信頼済みprofileが保証する `profile_stable_id_v1` または `profile_unique_label_hash_v1` だけを
使う。後者はkind+正規化labelのhash。候補数256・label512文字を上限とし、同frameの重複
label/kind・重複IDは学習/適用とも保留する。raw labelはSnapshotへ保存しない。
同じ表示の意味がframe間で変わらないというprofile保証は必要で、#402の意味的追跡を代替しない。

Scopeを構成が明示した場合のみ同game/scene/profile/player/lineageで再開する。省略時はSession
固有opaque scopeで隔離する。OCRからゲーム/Player/sceneを推測して共有しない。#433未実装。

`VerifiedActionEffect` は信頼済み統合境界が生成し、snapshot/action/停止世代/後画面signatureを
receiptへ完全照合する。初回 `ManagedSessionRuntime.complete_execution_observation(...,
verified_effect=...)` で渡す。既にunknownとして消費した観測を後から再学習へupgradeしない。
一般のpixel変化、terminal/score表示、Modelの成功主張、変化なしという観測はそれだけで因果的な
no_effect/successへ再ラベルしない。普通の観測はunknownである。

**得点欄clickも探索候補として許可する。無意味だったというverified効果が得られればsoft priorが
下がり、候補を残したまま選択が変わる。現在の一般Runtimeには全ゲーム対応の因果検証providerは
未接続であり、常時unknownの環境でno-effect学習が改善したとは主張しない。** 既存の実操作履歴と
Outcomeのunchanged/state-deltaは `DecisionContextBuilder` のrepetition/観測utilityへ継続して渡る。
これは観測された反復へのsoft cueであり、意味的失敗の正解ラベルではない。動的IDでの一般的な
意味再対応、遅延/打切りfeedbackの校正、汎用因果検証は残件である。

receiptの `action_generation` はStop/rearm世代、`correlation_sequence` はSession内の単調action
連番で別概念。#530 v1の同値制約だけを受入拡張として除去し、旧eventも有効とする。
Session固定source/correlation IDと連番により同run160操作でもwatermarkを毎操作増やさない。

## 有限予算・終了・故障

| 対象 | 既定/上限 | 行動 |
| --- | --- | --- |
| queue | 128件、各4 KiB、構成上限4096件 | 満杯なら待たずdrop reasonを記録 |
| batch | 16仕事/回、因果証拠buffer最大64件 | 最小4 verifiedで検証、古いwindow超過は明示drop |
| worker | 20 ms/更新、thread CPU 5%/1秒 | 時間超過候補は非公開、CPU超過はdefer |
| memory | 8 MiBのserialized workspace予約 | queue+Snapshotコピーの保守的12倍でadmission、host余力も照合 |
| 公開 | 最低5秒間隔 | immutable次版だけ、Step中は交換しない |
| Knowledge公開書込 | 毎分12 logical atomic writes | 公開1回につき6を先行予約し超過はdefer |
| resource/deadline | versioned `LightResourceSample` | 圧迫・CPU/RAM余力不足・deadline接近でdefer |
| Stop事実checkpoint | 一回、最大128件/1 MiB | 学習/公開せず受理済みdeltaだけをatomic退避 |

`LightResourceSample` は#425向けの数値admission port。現在はown-worker CPU/保持量制約に加え
hostが渡す余力を照合する。既定の宣言余力を実OS/GPU資源分類の実測と称さない。#425/#426の
自動classifier/aggressiveness policyや#409の完全なUtility Estimatorは完成扱いにしない。
`UtilityMeasurement(v1)` → immutable手法別EWMAは実測latencyと任意verified qualityだけを保持し、
quality未知を0や成功へ補完しない。Routerへの完全Utility統合は#409に残る。

StopはMode/RunControllerを先にlatchし、workerへ終了通知する。workerはbounded事実checkpointを
最後に一回だけ書き、通常は2秒以内にjoinする。最後のterminal効果も次Light Sessionでscope/
Modeを再照合してqueueへ戻せる。INFERENCE_ONLYではcheckpointを変更せず再学習もしない。
重複は既存watermarkで排除する。queue/byteのretention超過はdrop telemetryで示す。
同scopeの並行Sessionはfile lock内でpending事実をID/hash照合してmergeし、durable Championの
watermark以下だけを消費済みとして除く。遅いwriterが未消費の他Session事実を上書きせず、
既にackされた事実を永久にretentionへ戻さない。
通常の判断・Safety・Mode metadataのSession Journal記録はこの学習書込予算と別で継続する。
OSの遅いfsync/ファイルlock/非応答diskを強制中断するhard realtime保証はなく、期限内終了を
確認できない場合はcleanup failureを報告する。通常の推論はworkerのIOを待たない。

## 検証と実測

`tests/test_light_adaptation.py` は実Session/Pipeline/Verifier/Executorとfixture backendで、
Mode readonly、Stop/Mode競合後restart、disk故障/recovery、混在unknown、160操作dedup、
profile collision、terminal事実退避、queue/resource pressureを検証する。
毎frame異なるOCR IDへ4回のverified no_effectを与えた統合試験では、次判断がscoreからmoveへ
変わり、allowedは両候補のまま。Windows入力/実ゲーム精度の証明ではない。

`PYTHONPATH=src:. python tools/benchmark_light_adaptation.py --iterations 200 --output
build/performance/light-adaptation.json` は同じ実Session経路を3条件で測る。backendはOS入力を
送らないfixture、Safetyの設定は維持しclockだけ2秒刻みのfixture時間を使う。2026-11-01 Linux
実測は以下。JSON/Journal保存を含み、capture/Model/Windows時間を含まない。

| Mode/資源圧 | input p50/p95 ms | enqueue p50/p95 ms | dispatch |
| --- | --- | --- | --- |
| INFERENCE_ONLY | 26.052 / 52.906 | .0052 / .0058 | 200/200 |
| LIGHT / 通常 | 27.070 / 53.445 | .0095 / .0106 | 200/200 |
| LIGHT / 圧迫 | 28.303 / 54.953 | .0091 / .0101 | 200/200 |

圧迫条件はworker更新0、queue drop272でも全dispatchが完了した。これは有限fixtureの実測で
あって一般ゲームの固定deadlineや効果改善の保証ではない。Minecraft/FPS/カード等のWindows
実プレイ評価は別途#30/#173の受入れで行う。

## 手法と配置

Beta-Bernoulliの縮小推定、EWMA、有界incremental batch、immutable/MVCC型の版固定、trust-region
射影、Champ/Challenger+rollbackを組み合わせる。統計更新/JSON/Mode連携は低頻度・小容量で
Pythonとし、OS入力・高頻度安全境界をここへ移さない。gradient/GPU/model reload/history全scanなし。

- Sutton & Barto, *Reinforcement Learning*, 2nd ed., 2018: incremental estimates/step-size
  ([公開書籍](http://incompleteideas.net/book/the-book-2nd.html))。ゲーム固有の正解ルールを注入しない。
- Schulman et al., 2015, [Trust Region Policy Optimization](https://proceedings.mlr.press/v37/schulman15.html):
  更新幅を制約する考え方を参照。ここはgradient/TRPO実装ではなくscalar priorの有界射影。
- Joulani et al., 2013, [Online Learning under Delayed Feedback](https://proceedings.mlr.press/v28/joulani13.html):
  遅延feedbackの意味を区別する背景。現在は対応receiptが無いfeedbackをunknownに保留し、
  論文の一般遅延feedbackアルゴリズムを実装済みとはしない。
