# AI Game Player

未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。ローカル規則またはOllamaを使い、実入力は明示許可までdry-runで扱います。

## 基本フロー

```text
画面キャプチャ → OCR/候補生成 → 安全評価 → Rule/Ollama判断 → 実行 → 結果評価 → 次の観測
```

主要コードは [`src/ai_game_player/`](src/ai_game_player/) にあります。現在はroot package直下にmoduleが多いため、読む入口は [`src/ai_game_player/README.md`](src/ai_game_player/README.md) のSource Mapを参照してください。責務別subpackageへの段階移行はIssue #356で管理します。

- [現在の機能と制約](key_info.md)
- [クラス図](doc/class_diagram.mmd)
- [シーケンス図](doc/sequence_diagram.mmd)
- [評価指標](doc/評価指標機能説明書.md)

## ソースコードを読む

GitHub上から直接sourceへ移動できます。

- [Python package: `src/ai_game_player/`](src/ai_game_player/)
- [Python source map / 読む順序](src/ai_game_player/README.md)
- [Application層](src/ai_game_player/applications/)
- [Native Runtime境界](src/ai_game_player/runtime/)
- [UI](src/ai_game_player/ui/)
- [C++ Native Runtime](native/)
- [Tests](tests/)
- [Development tools](tools/)

`src/ai_game_player/` 直下のflat module群は責務別packageへ段階移行します。新規コードは、既存の責務subpackageがある場合はrootへ増やさずそこへ配置する方針です。

## 現在できること

- 起動済みWindowsの一覧取得と対象ウィンドウ選択
- Windows画面キャプチャ、OCR候補と手入力候補の統合
- RuleProvider / OllamaProviderによる候補判断
- dry-run、明示許可付きWindows入力、連続実行、停止（ボタン/Esc/F12/手動マウス移動）
- success/failure/ongoingのルール評価とOllama状態評価
- 判断・実行・状態評価の履歴とJSONLログ保存

## 起動

```powershell
$env:PYTHONPATH = "src;."
py -3.10 -m ai_game_player
```

Ollamaを使う場合は `ollama serve` とモデルの取得が必要です。実入力は対象ウィンドウ、入力方式、実入力許可を確認してから有効化してください。

## テスト

```powershell
$env:PYTHONPATH = "src;."
py -3.10 -m unittest discover -s tests -v
```

GitHub Actionsでもテストを実行します。