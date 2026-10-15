# AI Game Player

未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。ローカル規則またはOllamaを使い、実入力は明示許可までdry-runで扱います。

## 基本フロー

```text
画面キャプチャ → OCR/候補生成 → 安全評価 → Rule/Ollama判断 → 実行 → 結果評価 → 次の観測
```

主要コードは `src/ai_game_player/` にあり、観測（capture/source）、評価（evaluator/outcome）、判断（provider/engine）、実行（action_executor/windows_input）、履歴・ログを分離しています。

- [現在の機能と制約](key_info.md)
- [参加領域とレビュー先](doc/contributor/subsystem_ownership_map.md)
- [クラス図](doc/class_diagram.mmd)
- [シーケンス図](doc/sequence_diagram.mmd)
- [評価指標](doc/評価指標機能説明書.md)

## 現在できること

- 起動済みWindowsの一覧取得と対象ウィンドウ選択
- Windows画面取得後にOCR・画像候補と任意のAutomation候補を統合し、入力元と安全評価をGUIへ表示
- RuleProvider / OllamaProviderによる候補判断
- dry-run、明示許可付きWindows入力、連続実行、停止（ボタン/Esc/F12/手動マウス移動）
- success/failure/ongoingのルール評価とOllama状態評価
- 判断・実行・状態評価の履歴とJSONLログ保存

## 起動

```powershell
$env:PYTHONPATH = "src;."
py -3.10 -m ai_game_player
```

画面取得後、GUIはObservationに含まれるOCR・画像候補と「Automation候補JSON」欄の候補を統合し、評価結果に入力元を表示します。Automation候補が不要なら候補欄を空配列 `[]` のまま使えます。画像の明るい領域だけから作ったconfidence `0.0` の候補は評価で拒否され、実行候補になりません。

Ollamaを使う場合は `ollama serve` とモデルの取得が必要です。実入力は対象ウィンドウ、入力方式、実入力許可を確認してから有効化してください。

## テスト

```powershell
$env:PYTHONPATH = "src;."
py -3.10 -m unittest discover -s tests -v
```

GitHub Actionsでもテストを実行します。
