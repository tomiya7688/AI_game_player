# AI Game Player 現在の機能

確認基準：2026-11-01、統合feature `77a8995e`。部品・本番接続・実ゲーム検証を区別します。[場所別の状態と根拠](doc/guide/implementation_status.md)も参照してください。

## できること

- #659参照2048認識: 発光によるROIの局所外れ値を中央値と幾何支持で除外し、未知セルを候補にしません。得点はSCOREの位置証拠または明示文字列が必要で、曖昧ならunknownを保持します。[回帰・費用・限界](doc/architecture/reference_2048_recognition.md)。

- #567任意Planner: 最初1手だけをVerifier/Safetyへ渡し、fresh実画面で状態・候補・期限を照合。非SELECTは有限再観測/無入力停止、予測と実Outcomeを分離します。[設計と限界](doc/architecture/planner_runtime.md)。

- #491: 6軸評価を既定Session/Context/Traceへ接続。actualと候補hypothetical、未知と中立、coverage/支持率を区別し、typed profile/同frame仮説でBaselineのsoft順位を変えます。Safety/allowedは維持し、pixel/Model自己申告をactual進捗へ転記しません。[契約と費用](doc/architecture/runtime_evaluation.md)。

- Beliefの確率遷移予測とjoint観測尤度によるBayesian更新（Issue #564）。未校正/尤度不足ではunknownを保持し、実入力を選びません。[設計と検証](doc/architecture/belief_update.md)。

- 型付き不変WorldStateHypothesis / BeliefSnapshotと有界候補Store（校正確率・比較scoreの区別、unknown残余、K/byte/TTL/scope制約）。詳細は `doc/architecture/belief_snapshot.md`。
- 汎用Planning Risk/情報価値Policyで、仮想chance分布の下側損失と未知、観測後mapping全体のrisk、Goal episode再観測予算を検査できます（#566）。任意Providerを#567でVerifier/Safetyへ接続しました。実測比較は検証中、Unknownness Action Policy全体は#206です。[契約と限界](doc/architecture/planning_risk_information.md)。
- #422: 実Sessionの既定Modeにbounded Light workerを接続。receiptとverified効果、profile保証hash識別、unknown保留、Gate light検証→Mode CAS→durable ack、Stop事実退避。一般ゲームの因果検証/#433識別/#409完全Utility/#425自動資源分類は未完。[契約とp50/p95](doc/architecture/light_adaptation.md)。

- Runtime Mode / Phase のコア契約（5 Mode / 8 Phase、既定 Light、安全境界、Step 版固定、共有 RunController の Stop、Journal / checkpoint）。詳細は `doc/architecture/runtime_modes.md`。
- 任意構成したKnowledgeFeatureAdapterで、scopeと期限が一致する版を一stepへ固定し、Baselineのsoft prior・重みへ反映できます。モデル文脈への追加は既定OFFです。[Knowledge投影の説明](doc/architecture/knowledge_feature_adapter.md)を参照してください。

- 起動済みWindowsウィンドウの取得・選択・範囲キャプチャ
- 画面取得ボタンと連続実行は同じframe producerから画面を取得し、重複したWindows captureを行いません。
- 追加consumerは同じframe列を購読できます。最新frame方式は未処理frameを新しいframeへ置き換え、順序保持方式は設定上限まで到着順を維持して、満杯時は新しいframeを破棄します。この購読契約自体は録画機能を実装しません。
- 各Observationの`features.capture_metadata`にはframe IDとcapture時刻を記録します。capture時刻とSession Event Journalの時刻は、システム日時の変更で逆行しないprocess-monotonic時間源を使います。取得から既定1秒を超えたframeはObservation生成に使いません。
- 画面取得後にObservation内のOCR・画像候補と任意のAutomation候補を統合し、GUIの評価結果に入力元と安全判定を表示
- ローカル規則またはOllamaによる判断
- ゲームIDで任意登録する仮想遷移契約と、2048の移動・合成・確率spawnの参照モデル（Issue #479）。画面取得・DOM・OS入力へ依存せず、未知ゲームではモデル利用不可を返します。[設計・実画面検証](doc/architecture/transition_simulation.md)を参照してください。
- `UnknownnessMonitor`へ渡された正規化済みの不確かさ根拠を5つのscope別に融合し、瞬間unknownと時間平滑化後の持続unknownを判定
- 明示許可付きのWindowsクリック・キー入力
- dry-run、連続実行、停止（ボタン/Esc/F12/手動マウス移動）
- ルールまたはOllamaによるsuccess/failure/ongoing評価
- 判断・実行・状態評価の履歴とログ
- 判断計画と世代付き実行受領記録を分離し、実操作後の明示観測を元の操作へ関連付けます。dry-run・入力前失敗を実操作Outcomeへ混ぜず、最後の操作も終端終了前に保存します。送信不明はunknownとして保持します。
- Session Event Envelope v1とSQLite WAL Journal（UTC/monotonic、連番、correlation、未コミットtransactionの復旧）
- ExperienceからDecision/OCR/UI/Embedding/Evaluator向けのTraining Datasetを生成・重複排除・固定seed分割・JSON export
- Native Runtime C ABI v1の任意ロード、version検証、instance初期化／終了とstatus変換
- FAST_CV対応Native RuntimeをFrameAnalyzerへ明示注入した場合、BGRA統計・perceptual hash・bright-regionをC++でbatch処理できます。未注入時はPython実装を使います。

- 数値Knowledge Snapshot v1をgame/scene/profile/Player lineageで隔離して保存・読取できます。不変版、出所、uncertainty、期限、整合性と読込容量を検証し、不正時はstatic fallbackを返します（Issue #529）。

- Knowledgeの更新候補を、同じ根拠付き判断ケースの成果・回帰・資源で比較し、本番版参照を原子的に切替・復元するAPI（Issue #532）。実入力を発行せず、取得済みのstep用Snapshotを変更しません。

## 制約

- Beliefは不変候補Storeに加え、#564の有限仮説に対するBayesian予測・観測更新Coreを備えます。本番Recognition / Journal / Decisionへの自動配線は#405/#407/#567の残件です。particle resamplingは未実装です。探索Coreは#565、実Decisionへの接続は#567の境界とし、予測を観測事実へ変換しません。[更新の契約とCPU検証](doc/architecture/belief_update.md)を参照してください。
- 固定版2048 campaignの`--save-failure-evidence`は既定OFFです。明示ON時、起動失敗の所有PID窓metadataと取得可能なWGC画素をcleanup前に有界保存します。診断を追加しただけで、自然なbrowser起動失敗の原因を解消したとは扱いません（#459/#637）。

- #421のMode契約と#422の本番Light workerは接続済みですが、既定の学習識別方針は無効です。信頼済みprofileの安定識別と検証済み効果がなければ因果学習を保留します。重学習・交互実行・自動資源分類・UIは#183/#423–#426/#188の残件です。通常のOutcome / Verifier / Safety評価を無効化しません。

- Windows入力は実環境でのみ動作し、ゲームがWindowsメッセージを無視する場合は`mouse`方式が必要です。
- 実入力は既定で無効です。
- 仮想遷移は複数Belief対応のbounded探索Coreへ接続しました（#565）。部分観測では共通open-loop MPC、明示保証時だけ状態別Expectimaxです。制限worker・CPU oracleまでを検証し、任意Providerで既存Verifier/Safetyへ最初1手だけ渡し、実観測で再計画できます（#567）。比較は検証中です。共通OCR/Rule経路の実ブラウザ試験では、テトリス開始後のキー候補生成と、ブロック崩しの追跡・パドル制御が未達でした。汎用ゲーム対応や攻略強化の完成として扱いません。
- OCRは`Pillow`と`pytesseract`の任意依存です。未導入時も画像候補と任意のAutomation候補をGUIで評価できます。明るさだけから作るconfidence `0.0` の画像候補は候補評価器が拒否します。ゲーム固有の成功条件は追加調整が必要です。
- `UnknownnessMonitor`は検出器の根拠を受け取るコアAPIです。画面認識器・prototype検索器・知識検索器からの自動入力とゲーム実行pipelineへの接続は未実装です。unknown時の行動選択も行わず、その方針はIssue #206の対象です。
- Dataset Builderは定義済みのExperience event形式のみをsample化します。Modelの学習・評価・昇格処理そのものは未実装です。sensitive artifact参照は明示opt-inがない限りDatasetから除外されます。
- GameSessionControllerの各SessionはSession Event Journalへ判断・実行・Safety・RuntimeLog eventを記録し、初回Sessionで旧JSON/JSONL履歴を移行します。Session Journalをまたぐ検索APIとTimeline/Video/Learningからの横断参照はIssue #263で対応します。
- Knowledge Promotion Gateにはofflineの対照評価・公開APIと、#422が利用する限定的なlight検証があります。通常Sessionのworkerは検証済み候補をStep境界で公開し、永続化確認を行います。これは全回帰評価やモデル重学習の完了ではありません。任意callbackの隔離評価（#173/#183）とWindows実ゲームでの学習効果は未確認です。
- 長期目的、知識埋め込み、複数人格比較は未実装です。
- Native Runtimeの汎用batch処理はメタデータ契約のみで`NOT_IMPLEMENTED`を返します。専用Frame Preprocess API以外のcapture/input画素処理は未実装で、GUI実行経路もNativeへ自動切替しません。
