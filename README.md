# AI Game Player

未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。ローカル規則またはOllamaを使い、実入力は明示許可までdry-runで扱います。

## 基本フロー

```text
画面キャプチャ → OCR/候補生成 → 安全評価 → Rule/Ollama判断 → 実行 → 結果評価 → 次の観測
```

主要コードは `src/ai_game_player/` にあり、観測（capture/source）、評価（evaluator/outcome）、判断（provider/engine）、実行（action_executor/windows_input）、履歴・ログを分離しています。

- [現在の機能と制約](key_info.md)
- [クラス図](doc/class_diagram.mmd)
- [シーケンス図](doc/sequence_diagram.mmd)
- [評価指標](doc/評価指標機能説明書.md)

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

現在のOllamaProviderを使う場合は `ollama serve` とモデルの取得が必要です。これは現行実装上の任意Providerであり、通常ユーザー向けの最終必須依存にはしません。実入力は対象ウィンドウ、入力方式、実入力許可を確認してから有効化してください。

## 推論Provider方針

今後の標準経路は、Kadoka同梱のLocal Inference Serviceを自動起動し、versionedな `Kadoka Inference Provider API` 経由で利用する構成です。

- 何も選ばない場合: local-onlyの同梱Provider
- Advanced: Ollama等のlocal external Provider
- Expert/Developer: Remote API / 独自Adapter
- Remote送信: 明示opt-in

Provider API仕様は [Inference Provider API v1](doc/architecture/inference_provider_api.md)、既定Model候補は `config/default_models.json` を参照してください。

## テスト

```powershell
$env:PYTHONPATH = "src;."
py -3.10 -m unittest discover -s tests -v
```

GitHub Actionsでもテストを実行します。