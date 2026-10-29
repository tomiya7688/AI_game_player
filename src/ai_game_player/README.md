# Python実装の読み方と責務別の入口

この案内は、Pythonの基本文法を知る開発者が、AI Game Playerの処理を読み始めるための索引です。`src/ai_game_player/`にはGUI、観測、判断、安全評価、入力、記録の実装があります。アプリを起動する手順は[リポジトリのREADME](../../README.md#起動)を確認してください。

## 目的から実装を選ぶ

| 調べる処理 | 実装の入口と役割 |
|---|---|
| アプリの起動と部品の接続 | [`__main__.py`](__main__.py)が起動し、[`app.py`](app.py)がGUIと操作処理を組み立てます。 |
| 画面と入力欄 | [`ui/`](ui/)に画面構築と補足表示があります。[`applications/`](applications/)には品質ゲート用の処理があります。 |
| 観測から実行までの接続 | [`pipeline.py`](pipeline.py)が観測、候補統合、判断、安全評価、実行を接続します。 |
| 画面取得と観測の提供 | [`screen_capture.py`](screen_capture.py)が画像を取得し、[`captured_source.py`](captured_source.py)と[`observation_source.py`](observation_source.py)が観測と候補を提供します。 |
| 画像・文字・UIの認識 | [`frame_analyzer.py`](frame_analyzer.py)、[`ocr_recognizer.py`](ocr_recognizer.py)、[`ui_recognition.py`](ui_recognition.py)から、候補の抽出処理を追えます。 |
| 候補の評価と次の操作の判断 | [`engine.py`](engine.py)が[`decision_context.py`](decision_context.py)の判断用情報を使い、[`provider.py`](provider.py)へ選択を依頼します。[`evaluator.py`](evaluator.py)は候補を評価します。 |
| 判断の検証と操作の安全評価 | [`decision_verifier.py`](decision_verifier.py)が判断の信頼性を検証し、[`action_safety.py`](action_safety.py)が操作の危険度を評価します。 |
| 実入力と停止 | [`action_executor.py`](action_executor.py)が実行を管理し、[`windows_input.py`](windows_input.py)がWindowsへ入力を送ります。[`run_control.py`](run_control.py)、[`safety_guard.py`](safety_guard.py)、[`fail_safe_runtime.py`](fail_safe_runtime.py)が停止状態や監視を扱います。 |
| 操作後の結果評価 | [`outcome.py`](outcome.py)と[`outcome_fusion.py`](outcome_fusion.py)が、成功・失敗・継続の評価を扱います。 |
| 経験と学習用データ | [`experience.py`](experience.py)、[`experience_reader.py`](experience_reader.py)、[`learning_dataset.py`](learning_dataset.py)、[`learning.py`](learning.py)に記録の読取・変換処理があります。 |
| 履歴と成果物の保存 | [`event_journal.py`](event_journal.py)、[`history.py`](history.py)、[`artifact_store.py`](artifact_store.py)、[`runtime_log.py`](runtime_log.py)が記録を保持します。 |
| Pythonとネイティブ実装の境界 | [`runtime/`](runtime/)が実装の登録と組立てを扱い、[`native/`](../../native/)にC++側の実装があります。境界の契約は[実行層の設計](../../doc/architecture/runtime_layers.md)で説明しています。 |

## 一回の操作を読む順序

まず`pipeline.py`の`DecisionPipeline.run_and_execute()`を開いてください。このメソッドは入力元から観測と候補を取得し、候補の統合、エンジンによる判断、信頼性の確認、安全評価、実行を順に進めます。安全評価で拒否された操作や、実入力に必要な信頼性の条件を満たさない判断は実行しません。

次に`engine.py`と`action_executor.py`を読むと、判断と入力を分けて確認できます。dry-runではOS入力を送らず、実入力では停止要求などの条件を確認してから入力します。操作後の結果評価は`outcome.py`などの責務です。判断だけを行う経路は、入力する経路と区別して確認してください。

## 現在の配置と今後の移行

現在は多くの実装ファイルが`src/ai_game_player/`直下にあります。責務別のディレクトリへの移行は[親Issue #356](https://github.com/tomiya7688/AI_game_player/issues/356)で管理しています。次の表は移行計画であり、このREADMEを追加した時点で移動済みであることを示しません。実際の配置は、このページの実装リンクから確認してください。

| 移行する範囲 | 要求と完了条件 |
|---|---|
| 画面取得・認識 | [Issue #358](https://github.com/tomiya7688/AI_game_player/issues/358) |
| 判断・評価 | [Issue #359](https://github.com/tomiya7688/AI_game_player/issues/359) |
| 実行・安全制御 | [Issue #360](https://github.com/tomiya7688/AI_game_player/issues/360) |
| 保存・経験・学習 | [Issue #361](https://github.com/tomiya7688/AI_game_player/issues/361) |
| 公開importと互換性の整理 | [Issue #362](https://github.com/tomiya7688/AI_game_player/issues/362) |
| 直下への実装追加を検査するCI | [Issue #363](https://github.com/tomiya7688/AI_game_player/issues/363) |

新しい実装を追加する開発者は、同じ責務を持つ既存ディレクトリがある場合、そのディレクトリへ配置してください。移動時には対応するimport、テスト、この索引のリンクも更新してください。この案内の追加では、ファイルの移動や実行時の動作変更は行いません。

## 変更を検証する

対応するテストはリポジトリ直下の[`tests/`](../../tests/)に、開発用コマンドは[`tools/`](../../tools/)にあります。ソースREADMEから見たリポジトリ直下は二階層上です。

リポジトリ直下で次のコマンドを実行すると、このページとルートREADMEの相対リンクが実在するファイルまたはディレクトリを指すことを確認できます。リンク先ページ内の見出しと外部URLの到達性は、このテストでは確認しません。

```powershell
python -m unittest discover -s tests -p test_source_navigation.py -v
```

実装を変更した開発者は、対応するテストの後に`finish_task.bat`を実行してください。作業全体の手順は[開発ワークフロー](../../doc/ワークフロー/ワークフロー.md)にあります。
