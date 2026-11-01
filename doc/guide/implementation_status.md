# 場所ごとの実装状況と、完成までの残作業

[完成図と現状図へ戻る](target_and_current.md) ｜ [全体案内へ戻る](README.md)

**確認日：2026-11-01。ソース基準：`d31377e42714f4d2e5ee1a402a4c42065d73bcd6`。** 前回基準の`4d8ae6f`からの変更はウィンドウ一覧の補足説明と関連UI・テスト・生成資料で、以下の判断・認識・記録の主要コードには変更がありません。このページは全プラグインを網羅した監査ではなく、標準GUIと主要な拡張部品の確認表です。

「部品あり」はコードが存在すること、「接続あり」は標準の操作から呼ばれることです。「実環境の証拠」は、その版・機材・ゲーム・設定で試した記録を指します。テストファイルへのリンクは試験の入口であり、その試験を今回すべて実行したという意味ではありません。

## 1．現在どこまでつながっているか

| 場所 | 部品の実装 | 標準の実行経路 | 実環境について言える範囲 | 次に必要なこと |
|---|---|---|---|---|
| 画面取得 | Windows取得、範囲指定、共有フレームあり | 接続あり。WGCの利用条件とGDIへの代替あり | 対象環境・取得方式ごとの確認が必要 | 取得失敗・古い画面・再開を含む安定性の確認 |
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
| モデルと知識の改善 | 学習能力・互換性・成果物の由来を表す契約あり | 学習・評価・採用の自動運転は未実装 | 学習成果や本番採用の成功は未確認 | 更新候補を比較し、採用・却下・元に戻す |
| Native画像処理 | C++ FAST_CVと呼び出し契約あり | 前処理部品を明示的に渡した場合だけ | WGC画面取得とは別機能 | 標準接続の選択と実測。汎用batchは未実装 |
| 別PC・外部エージェント | Ollamaへの接続と拡張用の契約あり | 外部エージェントの標準接続は未確認 | 別PCでの実推論をこの調査では試していない | Codex参照実装、登録・権限・画像入力・来歴 |

既存一覧の[現在の機能と制約](../../key_info.md)を併用してください。ただし同ファイル末尾のNative画面取得に関する包括的な説明は、`screen_capture.py`の条件付きWGC経路と区別して読む必要があります。WGCがあることから、画像前処理・入力まで全部Nativeへ切り替わるとは言えません。

## 2．モデルを呼ぶ場所は三つを分ける

**今回の操作判断：** 判断材料を作り、設定されたRuleProviderまたはOllamaProviderへ渡します。Ollamaの確認した要求はJSONを文章にしたもので、画素画像を直接送る要求ではありません。

**前回操作の結果を調べる処理：** OutcomeDetectorが終了表示や画面変化などを統合します。結果を保留した場合や確信度が基準未満の場合、対応するProviderがあれば意味評価を追加します。

**GUIからの画面状態評価：** `RuntimeComposition.assess_outcome()`はOllamaが選択されていれば直接Providerへ要求します。この経路を「低確信のときしかモデルを呼ばない」と説明してはいけません。

設定先が別PCの場合は、画面由来の状態、候補、目標、履歴などがその接続先へ送られます。HTTPのままなら通信内容の暗号化はありません。送信先と管理者、保存・利用の扱いを確認し、[脅威モデル](../security/threat_model.md)を参照してください。設定モデルと実際の呼び出しを表示する機能は[Issue #627](https://github.com/tomiya7688/AI_game_player/issues/627)で追跡します。

## 3．記録とゲーム内の事実は同一ではない

現在のコードでは、`GamePlayerEngine.step()`が判断を保存した時点で次回の結果評価に使う参照も更新します。`ActionExecutor.execute()`は確認運転で`executed=False`を返し、`DecisionPipeline.run_and_execute()`はその結果を実行履歴へ保存します。**この保存だけを根拠に「前回の参照は実入力した操作だけ」とは言えません。** 判断だけの経路、確認運転、安全拒否、実行失敗を含めた対応は別に確認が必要です。

設計上は、入力していない判断をゲームに与えた操作として学習・評価しないことが必要です。この点の説明と検証は[Issue #611](https://github.com/tomiya7688/AI_game_player/issues/611)と[Issue #612](https://github.com/tomiya7688/AI_game_player/issues/612)の残作業に含め、資料だけで保証済みにしません。

## 4．完成形で必要な処理選択と学習

完成形の調整役は、複数の画面の照合、複数回の推論の一致、信頼できる結果の選別、手法ごとの重み付け、前段の結果による候補の制限を選択肢に持ちます。全部を毎回実行するのではなく、残り時間と改善の見込みに応じて使います。安全検査の必要条件は、重みや多数決で変更しません。

推論時間の見込みと操作判断への寄与を実測から更新し、各操作での選択は整理済みの表や条件分岐を使います。モデルを使わない運転でも、経験から知識、しきい値、候補の優先度などの更新候補を作り、比較評価して採用する設計です。

[Issue #19](https://github.com/tomiya7688/AI_game_player/issues/19)には、推論のみ／学習のみ／両方／交互／推論と軽い適応の5方式と、「推論と軽い適応」を既定にする目標があります。**これは現行AppConfigで5方式が完成しているという意味ではありません。** 学習器の起動、更新候補の評価、採用、元に戻す工程は別々に接続を確認します。

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
