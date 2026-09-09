# Versions

## Unreleased

- 実入力許可時にGUIの実行ボタン・連続実行ボタン・状態表示を実入力用へ切り替える。
- window_message方式の実入力前に対象ウィンドウ選択を検証する。
- 対象ウィンドウのキャプチャに失敗した場合、連続実行を停止する。
- RuleProviderの状態評価をローカルのOutcomeEvaluatorへ統一し、Rule経路でネットワークアクセスしないようにする。
