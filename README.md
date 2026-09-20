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

## CI上の低品質AI耐性

Kadokaは高品質LLMだけを前提にせず、テスト専用の `NoisyLanguageProvider` も常時CIで利用します。これは実MLモデルではなく、候補内から擬似ランダムに操作を選び、意味の薄い文章を返すdeterministic fixtureです。

目的は攻略性能ではなく、低品質なAI出力が続いてもCore/Safetyが壊れないことと、LLM推論時間をほぼ除いたKadoka側overheadを継続測定することです。


## 1.0.0の配布方針

1.0.0はWindows x64 / local-firstを基本とし、配布物rootの実行ファイルを起動すれば、必要なruntime・Local Inference Service・model/packageが自動的に解決され、通常利用を開始できる状態を目標にします。通常ユーザーへPython、pip、CMake、compiler、Ollama等の手動導入は要求しません。

Basic導線は `Launch -> Game/Window Select -> Start/Stop` です。Remote Providerはoptionalかつ明示opt-inです。詳細は `doc/release_1_0.md` を参照してください。

Product/Engineの最終名称はIssue #154で管理します。現時点のKadoka表記を最終Product名として固定しません。

## Privacy defaults for 1.0.0

- telemetry: OFF
- Remote Provider: OFF
- raw frame / replay persistent storage: OFF
- trace: local-only + bounded retention
- diagnostic export / remote transmission: explicit user action

## License

- first-party code / docs / non-character software assets: MIT License
- Kadoka / Maru character assets: Obake License
- third-party code / runtime / model / game / asset: upstream licenseを維持

詳細は `LICENSE`、`ASSET_LICENSES.md`、`THIRD_PARTY_NOTICES.md` を参照してください。


### 1.0.0 License boundary

1.0.0ではfirst-party配布範囲をMITで揃えます。賢者ミスクはMITのため1.0.0に含められます。Kadoka / MaruなどObake License対象の素材・Character機能は1.1以降へ延期します。third-partyは各upstream licenseと必要noticeを維持します。
