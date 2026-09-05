# AI Game Player 現在の機能

## できること

- 画面観測JSONとAutomation候補JSONを入力して、候補を検証・統合できます。
- ローカル規則ProviderまたはOllama Providerで1ステップの判断ができます。
- 候補ごとの評価結果（採用・除外理由）をGUIで確認できます。
- Windowsでは対象ウィンドウを選択し、その範囲をキャプチャできます。
- GUIから判断、dry-run実行、連続dry-run、停止・再開ができます。停止は「■ 停止（連続実行を停止）」ボタンまたは `Esc` キーです。
- 判断履歴、実行履歴、評価指標、実行ログを保存できます。
- 設定は `data/config.json`、実行ログは `user_data/output/log/` に保存されます。

## 安全上の現在の制約

- 実際のマウス・キーボード入力は未実装です。Executorの既定値は常にdry-runです。
- 連続dry-runは判断と履歴確認用であり、ゲーム画面を操作しません。
- Ollamaを使う場合は、ローカルでOllamaが起動している必要があります。
- Windows以外では、Windows画面キャプチャとウィンドウ一覧取得は利用できません。

## 起動

```powershell
$env:PYTHONPATH = "src"
py -3.10 -m ai_game_player
```

Windowsでは `run_ai_game_player.bat` も利用できます。

## 開発状態

テストスイートは、観測・評価・判断・履歴・dry-run実行・設定・ログ・停止制御を対象にしています。実ウィンドウでの長時間運転や実入力は、別途安全確認が必要です。