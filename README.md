# AI Game Player

未知のGUIゲームを、観測・候補評価・判断・履歴保存の段階に分けて扱う実験用エンジンです。

## 起動

```powershell
$env:PYTHONPATH = "src"
py -3.10 -m ai_game_player
```

GUIでは画面観測JSONとAutomation候補JSONを入力し、ローカル規則またはOllamaで1ステップ判断できます。実行Executorの既定値はdry-runで、OSへのマウス・キーボード入力は行いません。

## テスト

```powershell
$env:PYTHONPATH = "src"
py -3.10 -m unittest discover -s tests -v
```

画面取得、画像解析、OCR候補、Automation候補、候補統合、知識JSON、判断履歴、実行履歴、評価指標を責務ごとに分離しています。