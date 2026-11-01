# AI Game Player 現在の機能

## できること

- 型付き不変WorldStateHypothesis / BeliefSnapshotと有界候補Store（校正確率・比較scoreの区別、unknown残余、K/byte/TTL/scope制約）。詳細は `doc/architecture/belief_snapshot.md`。

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
- Session Event Envelope v1とSQLite WAL Journal（UTC/monotonic、連番、correlation、未コミットtransactionの復旧）
- ExperienceからDecision/OCR/UI/Embedding/Evaluator向けのTraining Datasetを生成・重複排除・固定seed分割・JSON export
- Native Runtime C ABI v1の任意ロード、version検証、instance初期化／終了とstatus変換
- FAST_CV対応Native RuntimeをFrameAnalyzerへ明示注入した場合、BGRA統計・perceptual hash・bright-regionをC++でbatch処理できます。未注入時はPython実装を使います。

## 制約

- Beliefは候補保持のCore APIとCPU fixtureまでです。本番Recognition / Journal / Decisionへの配線、Bayesian更新は #405/#407/#564 で対応し、予測を観測事実へ変換しません。

- Windows入力は実環境でのみ動作し、ゲームがWindowsメッセージを無視する場合は`mouse`方式が必要です。
- 実入力は既定で無効です。
- 仮想遷移は探索・本番Decisionへ未接続です（#478、#565-#567）。共通OCR/Rule経路の実ブラウザ試験では、テトリス開始後のキー候補生成と、ブロック崩しの追跡・パドル制御が未達でした。汎用ゲーム対応や攻略強化の完成として扱いません。
- OCRは`Pillow`と`pytesseract`の任意依存です。未導入時も画像候補と任意のAutomation候補をGUIで評価できます。明るさだけから作るconfidence `0.0` の画像候補は候補評価器が拒否します。ゲーム固有の成功条件は追加調整が必要です。
- `UnknownnessMonitor`は検出器の根拠を受け取るコアAPIです。画面認識器・prototype検索器・知識検索器からの自動入力とゲーム実行pipelineへの接続は未実装です。unknown時の行動選択も行わず、その方針はIssue #206の対象です。
- Dataset Builderは定義済みのExperience event形式のみをsample化します。Modelの学習・評価・昇格処理そのものは未実装です。sensitive artifact参照は明示opt-inがない限りDatasetから除外されます。
- GameSessionControllerの各SessionはSession Event Journalへ判断・実行・Safety・RuntimeLog eventを記録し、初回Sessionで旧JSON/JSONL履歴を移行します。Session Journalをまたぐ検索APIとTimeline/Video/Learningからの横断参照はIssue #263で対応します。
- 長期目的、知識埋め込み、複数人格比較は未実装です。
- Native Runtimeの汎用batch処理はメタデータ契約のみで`NOT_IMPLEMENTED`を返します。専用Frame Preprocess API以外のcapture/input画素処理は未実装で、GUI実行経路もNativeへ自動切替しません。
