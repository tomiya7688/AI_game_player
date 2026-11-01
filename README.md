# AI Game Player

未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。ローカル規則またはOllamaを使い、実入力は明示許可までdry-runで扱います。

## 基本フロー

```text
画面観測・候補生成 → 候補評価 → 前回操作の結果評価 → 判断材料の構築 → AI判断・検証 → 安全評価 → 入力（許可時） → 次の画面観測
```

前回の操作記録がある場合、エンジンは新しい画面観測を使って前回操作の結果を評価し、その結果を今回の判断材料へ含めます。候補評価は判断材料を構築する前に行います。

主要コードは[`src/ai_game_player/`](src/ai_game_player/)にあります。観測、判断、安全評価、入力、記録の実装を調べる開発者は、[Python実装の読み方と責務別の入口](src/ai_game_player/README.md)から読み始めてください。

- [現在の機能と制約](key_info.md)
- [参加領域とレビュー先](doc/contributor/subsystem_ownership_map.md)
- [セキュリティ脅威モデル](doc/security/threat_model.md)
- [クラス図](doc/class_diagram.mmd)
- [シーケンス図](doc/sequence_diagram.mmd)
- [評価指標](doc/評価指標機能説明書.md)

## ソースコードを読む

開発者は、調べる処理に応じて次の入口を開いてください。各リンクはGitHub上のファイルまたはディレクトリへ移動します。

- [Python実装の索引と読む順序](src/ai_game_player/README.md)
- [Pythonパッケージ全体](src/ai_game_player/)
- [アプリケーションの品質ゲート](src/ai_game_player/applications/)
- [Pythonとネイティブ実装の接続](src/ai_game_player/runtime/)
- [GUIの画面構築と補足表示](src/ai_game_player/ui/)
- [C++のネイティブ実装](native/)
- [テスト](tests/)
- [開発用ツール](tools/)

責務別ディレクトリへの移行計画は[Issue #356](https://github.com/tomiya7688/AI_game_player/issues/356)で管理しています。現在の配置と移行計画の区別は、Python実装の索引で説明しています。

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


## 開発用の独立テストゲーム

テトリス・オセロ・将棋・チェス等の10種を、[独立したGPL検証fixture](tools/test_games/README.md)として起動できます。専用環境へ依存を導入し、通常アプリのruntime・配布対象から分離します。[操作受入れと未検証の範囲](doc/独立テストゲーム集.md)を確認してください。
