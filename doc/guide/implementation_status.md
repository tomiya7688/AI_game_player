# 場所ごとの実装状況と、完成までの残作業

[完成図と現状図へ戻る](target_and_current.md) ｜ [全体案内へ戻る](README.md)

**確認日：2026-11-01。統合ソース基準：`f145c946685ae7361f8d3e6416b85c6fdf07e988`。** 初版の`d31377e`から、実行受領記録、Modeと軽量知識更新、型付き状態仮説、起動失敗診断を反映しました。mainへ統合した資料7件も照合しています。このページは全プラグインを網羅した監査ではなく、標準GUIと主要な拡張部品の確認表です。

「部品あり」はコードが存在すること、「接続あり」は標準の操作から呼ばれることです。「実環境の証拠」は、その版・機材・ゲーム・設定で試した記録を指します。テストファイルへのリンクは試験の入口であり、その試験を今回すべて実行したという意味ではありません。

## 1．現在どこまでつながっているか

| 場所 | 部品の実装 | 標準の実行経路 | 実環境について言える範囲 | 次に必要なこと |
|---|---|---|---|---|
| 画面取得 | Windows取得、範囲指定、共有フレームあり | 接続あり。WGC（Windowsの新しい方式による画面取得）の利用条件と、GDI（従来の描画機能による画面取得）への代替あり | 対象環境・取得方式ごとの確認が必要 | 取得失敗・古い画面・再開を含む安定性の確認 |
| 画像の特徴 | 色・明るさ・画像比較・明るい領域の検出あり | Pythonが標準 | ボタンの意味認識まで証明しない | 認識器の選択と場面別の比較 |
| 文字認識 | Tesseractへの接続部品あり | 依存が利用できる場合だけ | 言語データの版と、この利用者PCでの動作は未確認 | 導入状態・失敗理由・実行証拠の整理 |
| 候補整理・評価 | 重複整理、入力元、拒否理由あり | 接続あり | 明るさだけの確信度0.0候補は許可されない | 複数手法・複数画面の根拠の扱いを拡充 |
| 操作判断 | RuleProviderとOllamaProviderあり | 初期設定は規則。Ollamaは選択時 | モデル欄の名前から実推論を断定できない | 共通契約・選択方針・実モデルの証拠 |
| 追加のモデルなし判断 | BaselineDecisionProvider、NoisyLanguageProviderあり | 標準GUIの二択とは別の部品・検証用途 | 試験用の乱数選択は学習済み言語モデルではない | 能力別の基準判断と選択経路への接続 |
| 評価の統合 | EvaluationFusion、OutcomeDetectorあり | 判断材料・前回結果の評価に接続 | 固定の重み付き計算は、自動学習された重みではない | 状況別の重み、所要時間・改善期待値の更新 |
| 分からなさの測定 | UnknownnessMonitorのコアAPIあり | 自動の信号入力と標準パイプライン接続は未実装 | APIの試験と、ゲーム上の判断は別 | 再観測・保留・探索の方針を接続 |
| 安全と入力 | 回答照合、安全評価、停止、期限付き実行制御あり | 実入力は明示許可時だけ。確認運転が既定 | OS・入力方式・ゲームごとの条件付き | 対象消失・失敗・復旧の検証を継続 |
| 記録と知識 | Session Journal、知識保存、Experience契約、Artifact Storeあり | 判断・実行・安全・ログのSession記録は接続 | すべての画像の自動録画を意味しない | Session横断参照、動画とイベントの対応 |
| 学習用データ | 変換、重複整理、出所保持、固定条件での分割あり | 対応イベントを渡す別処理 | データ作成はモデル更新ではない | 実データ収集から学習工程への接続 |
| 軽量な数値知識の改善 | 不変保存・集計・一判断中の版固定・検証/公開/復帰あり | #422の有限workerを実Sessionへ接続。既定の識別方針は無効で、効果不明は保留 | verified効果を与える結合fixtureで選択変化。Windows実ゲームの改善は未確認 | 一般の因果検証・安定識別、#433/#402等 |
| モデル重学習 | 学習能力・互換性・成果物の由来を表す契約あり | GPU学習器の自動運転は未実装 | モデル重学習の成果は未確認 | #183等で学習器・評価・採用を接続 |
| 複数の状態仮説 | 型付き不変Store、有限仮説のBayesian予測・観測更新Coreあり | Recognition/Journal/Decisionへの自動接続は未実施 | 整数/曖昧2048のCPU fixtureと有理数oracle照合。ゲーム認識精度ではない | #405/#407の校正済み根拠、#567のRuntime接続 |
| 起動失敗の診断 | 所有PID窓の有界metadata/画素保存あり | campaignの既定OFF flagを明示した場合だけ | Linux fake回帰。自然失敗時のWindows captureは未確認 | #637の診断から#459の原因を特定・修正 |
| Native画像処理 | C++ FAST_CVと呼び出し契約あり | 前処理部品を明示的に渡した場合だけ | WGC画面取得とは別機能 | 標準接続の選択と実測。汎用batchは未実装 |
| 別PC・外部エージェント | Ollamaへの接続と拡張用の契約あり | 外部エージェントの標準接続は未確認 | 別PCでの実推論をこの調査では試していない | Codex参照実装、登録・権限・画像入力・来歴 |

既存一覧の[現在の機能と制約](../../key_info.md)を併用してください。ただし同ファイル末尾のNative画面取得に関する包括的な説明は、`screen_capture.py`の条件付きWGC経路と区別して読む必要があります。WGCがあることから、画像前処理・入力まで全部Nativeへ切り替わるとは言えません。

## 2．モデルを呼ぶ場所は三つを分ける

**今回の操作判断：** 判断材料を作り、設定されたRuleProviderまたはOllamaProviderへ渡します。Ollamaの確認した要求はJSONを文章にしたもので、画素画像を直接送る要求ではありません。

**前回操作の結果を調べる処理：** OutcomeDetectorが終了表示や画面変化などを統合します。結果を保留した場合や確信度が基準未満の場合、対応するProviderがあれば意味評価を追加します。

**GUIからの画面状態評価：** `RuntimeComposition.assess_outcome()`はOllamaが選択されていれば直接Providerへ要求します。この経路を「低確信のときしかモデルを呼ばない」と説明してはいけません。

設定先が別PCの場合は、画面由来の状態、候補、目標、履歴などがその接続先へ送られます。HTTPのままなら通信内容の暗号化はありません。送信先と管理者、保存・利用の扱いを確認し、[脅威モデル](../security/threat_model.md)を参照してください。設定モデルと実際の呼び出しを表示する機能は[Issue #627](https://github.com/tomiya7688/AI_game_player/issues/627)で追跡します。

## 3．記録とゲーム内の事実は同一ではない

#628以後、`GamePlayerEngine.step()`は判断計画だけを保存します。正常送信の実行受領記録を画面・操作・停止世代へ固定し、`complete_execution_observation()`で明示された入力後の観測を元操作へ一度だけ関連付けます。判断のみ・確認運転・入力前失敗で過去の実操作を上書きせず、送信不明や旧世代の監査記録を因果学習へ混ぜません。[受領記録](../architecture/execution_receipts.md)と[安全境界](safety.md)を参照してください。

受領記録は送信事実であり、ゲームへの有効性の証明ではありません。画面変化、得点表示、モデルの主張だけをverified効果へ変換しません。通常のOutcomeや繰り返し評価は継続しますが、一般的な「無意味だった」という因果的正解ラベルの自動生成は未達です。

## 4．完成形で必要な処理選択と学習

完成形の調整役は、複数の画面の照合、複数回の推論の一致、信頼できる結果の選別、手法ごとの重み付け、前段の結果による候補の制限を選択肢に持ちます。全部を毎回実行するのではなく、残り時間と改善の見込みに応じて使います。安全検査の必要条件は、重みや多数決で変更しません。

推論時間の見込みと操作判断への寄与を実測から更新し、各操作での選択は整理済みの表や条件分岐を使います。モデルを使わない運転でも、経験から知識、しきい値、候補の優先度などの更新候補を作り、比較評価して採用する設計です。

[Issue #19](https://github.com/tomiya7688/AI_game_player/issues/19)の5方式は#421でModeと処理段階の契約になり、#422で「推論と軽い適応」を実Sessionの既定へ接続しました。これは5方式すべての学習器・交互/並行実行・自動資源制御・UIが完成したという意味ではありません（#183/#423–#426/#188）。

数値知識の#529保存、#530集計、#531判断投影、#532検証/公開は、#422のworkerと安全なStep境界を通して接続します。既定では安定した学習識別を設定せず、未知の効果を保留します。信頼済みprofileとverified効果がある場合の限定的な更新・選択変化はCPU結合fixtureで確認されています。Windows実プレイの精度改善、全ゲームでのno_effect認識、GPUモデル重学習の証明ではありません。[接続条件・故障・計測値](../architecture/light_adaptation.md)へ進んでください。

## 5．実装とテストへ進む

| 確認対象 | ソース | 対応するテストの入口 |
|---|---|---|
| 観測・画像・OCR | [observation_service.py](../../src/ai_game_player/observation_service.py)、[frame_analyzer.py](../../src/ai_game_player/frame_analyzer.py)、[screen_capture.py](../../src/ai_game_player/screen_capture.py) | [test_observation_service.py](../../tests/test_observation_service.py)、[test_frame_analyzer.py](../../tests/test_frame_analyzer.py)、[test_ocr_recognizer.py](../../tests/test_ocr_recognizer.py) |
| 規則・Ollama・GUI評価 | [config.py](../../src/ai_game_player/config.py)、[provider.py](../../src/ai_game_player/provider.py)、[session_composition.py](../../src/ai_game_player/runtime/session_composition.py) | [test_provider_decision.py](../../tests/test_provider_decision.py)、[test_provider_outcome.py](../../tests/test_provider_outcome.py)、[test_session_composition.py](../../tests/test_session_composition.py) |
| 追加の判断部品 | [baseline_provider.py](../../src/ai_game_player/baseline_provider.py)、[noisy_provider.py](../../src/ai_game_player/noisy_provider.py) | [test_baseline_provider.py](../../tests/test_baseline_provider.py)、[test_noisy_provider.py](../../tests/test_noisy_provider.py) |
| 判断・結果・不確かさ | [engine.py](../../src/ai_game_player/engine.py)、[decision_context.py](../../src/ai_game_player/decision_context.py)、[outcome_fusion.py](../../src/ai_game_player/outcome_fusion.py)、[unknownness.py](../../src/ai_game_player/unknownness.py) | [test_decision_context.py](../../tests/test_decision_context.py)、[test_outcome_fusion.py](../../tests/test_outcome_fusion.py)、[test_unknownness.py](../../tests/test_unknownness.py) |
| 入力と安全 | [pipeline.py](../../src/ai_game_player/pipeline.py)、[action_executor.py](../../src/ai_game_player/action_executor.py)、[fail_safe_runtime.py](../../src/ai_game_player/fail_safe_runtime.py) | [test_pipeline_execute.py](../../tests/test_pipeline_execute.py)、[test_fail_safe_runtime.py](../../tests/test_fail_safe_runtime.py) |
| 記録から学習契約 | [event_journal.py](../../src/ai_game_player/event_journal.py)、[learning_dataset.py](../../src/ai_game_player/learning_dataset.py)、[learning.py](../../src/ai_game_player/learning.py) | [test_event_journal.py](../../tests/test_event_journal.py)、[test_learning_dataset.py](../../tests/test_learning_dataset.py)、[test_learning_contract.py](../../tests/test_learning_contract.py) |

## 6．何を確認したら状態を更新するか

部品の実装、標準経路への接続、対象環境での試験結果の順に、根拠を付けて更新します。CIの成功は、対象コミット・実行回・ゲーム・機材の範囲内の証拠です。以前の失敗を再実行の成功で消したり、PRがマージされたことだけで製品の全機能を完成扱いしたりしません。

残作業の入口は、処理選択が[#13](https://github.com/tomiya7688/AI_game_player/issues/13)、不確かさからの行動方針が[#206](https://github.com/tomiya7688/AI_game_player/issues/206)、共通推論契約が[#174](https://github.com/tomiya7688/AI_game_player/issues/174)、学習が[#180](https://github.com/tomiya7688/AI_game_player/issues/180)、録画が[#201](https://github.com/tomiya7688/AI_game_player/issues/201)、Session横断参照が[#263](https://github.com/tomiya7688/AI_game_player/issues/263)、実行モデルの証拠が[#627](https://github.com/tomiya7688/AI_game_player/issues/627)です。実装作業は各親Issueの子Issue単位で進め、説明資料の[#606](https://github.com/tomiya7688/AI_game_player/issues/606)を製品機能の実装先の代わりにしません。


## 7．統合版で実画面を確かめた範囲

2026-11-01、実装基準`f145c946685ae7361f8d3e6416b85c6fdf07e988`で、隔離Chromiumへ通常入力を送り、画面からの認識・判断・実行を再検証しました。以下は実際に確認した範囲で、Windows WGC/Win32入力や汎用攻略性能の受入れではありません。

| 対象と判断経路 | 実操作と結果 | 証明しないこと |
|---|---|---|
| 固定版2048、既存の方向巡回判断 | seed 479/480/481の3プレイで計404操作。次盤面はすべて予測分布と一致し、Stop後の入力拒否を確認。得点1332/1140/1256、最大タイルは各128 | 巡回判断と遷移照合であり、Bayesの本番Decision接続・学習による棋力改善の比較ではない |
| Tetris、共通OCRと無変更の規則判断 | 「Play」を1回クリックし、その後は許可候補なしで停止 | 開始後のキー候補生成・自律攻略は未達 |
| Breakout、共通OCRと無変更の規則判断 | 約29.21秒で30回クリック。Score、Level、数値等を選び、paddle制御は成立しなかった | 画面差や反復を因果的no_effectの正解にせず、学習改善・攻略成功と扱わない |

2048の操作経路・検証手順は[仮想遷移の実画面検証](../architecture/transition_simulation.md)、実操作の帰属は[実行受領記録](../architecture/execution_receipts.md)を参照してください。開発環境の補助証拠は`build/transition-2048/final-stack-{screen,tetris,breakout}/report.json`です。これらの実画面検証と、[独立10種fixtureのOS入力受入れ](../独立テストゲーム集.md)は別の証拠で、10種を汎用AIが攻略したことにはなりません。認識・探索・結果評価の未達は#30/#96/#565/#491/#566/#567等へ追跡します。
