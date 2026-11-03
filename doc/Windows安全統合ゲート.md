# Windows安全統合ゲート（Issue #94）

GitHub ActionsのWindows runnerで、Windows固有のCapture・Window targeting・Input・Safety・bundleの回帰を検出する検査の一覧です。CI自体が利用者のデスクトップや任意のアプリへ実入力を送る主体にならないことを、検査の前提として固定します。

## 実入力センチネル下のplatform gate

`safety-ci`の`Windows platform gate (no live input)`ジョブは`tools/run_windows_platform_gate.py`を実行します。

- 対象: `GATE_TEST_MODULES`に列挙したWindows境界のテスト（target/window検証、座標変換、`WindowsInput`、desktop/HWND capture、Win32 ABI、bundle self-check、planning用window snapshotとファイル読取）と、`test_fail_safe*.py`（lease・TTL・hold・release-allの状態遷移）。1プロセスで実行します。
- 実入力センチネル: `tools/windows_input_sentinel.py`が`ctypes.CDLL.__getitem__`を差し替え、`ctypes.windll.user32`と`ctypes.WinDLL("user32")`のどちらから取得した関数も監視します。
  - グローバル入力API（`SendInput`、`keybd_event`、`mouse_event`、`SetCursorPos`、`SetPhysicalCursorPos`、`BlockInput`）は、実関数を呼ぶ前に必ず遮断します。
  - `PostMessage`・`SendMessage`・`SendMessageTimeout`・`SendNotifyMessage`のうち、キーボード（`0x0100`〜`0x0109`）・マウス（`0x0200`〜`0x020E`）と、入力と同等に操作を起こす`WM_INPUT`・`WM_SYSCOMMAND`・`WM_HOTKEY`・`WM_APPCOMMAND`は、送信先HWNDの所有PIDがテストプロセス自身と一致する場合だけ通します。所有PIDが取得できない（照会の例外を含む）HWNDとHWND 0は遮断します。送信先が窓ではなくthreadの`PostThreadMessage`は、これらのmessageを常に遮断します。`WM_PRINT`などの描画要求は制限しません。
  - 監視はinstall後に取得した関数だけに効きます。runnerは`ai_game_player`がinstall前にimport済みなら自己検査失敗とし、テストモジュールはinstall後に読み込みます。遮断記録には実行中のテストIDを添えます。
  - 遮断した呼出しは`InputSentinelViolation`を送出し、同時に記録します。テストが例外を握りつぶして成功しても、記録が1件でもあればgateは失敗します。
- 自己検査: テスト実行前に、両方の取得経路で入力関数が監視対象になっていること、遮断が実呼出し前に起きることを確かめます。監視されていない関数を見つけた場合は呼び出さずに中断します。判定に誤りがあっても無害なよう、自己検査ではVK 0と存在しないHWNDを使います。
- 空振り防止: Windowsでしか実行されない`REQUIRED_WINDOWS_TESTS`がskipされた、または実行されなかった場合は失敗します。Windows以外では実行を拒否します（終了コード2）。
- 証跡: `build/windows-platform-gate/report.json`（schema `windows-platform-gate/v1`）をartifact `kadoka-windows-platform-gate`として保存します。実行テスト数、skipと理由、遮断した呼出し、自作窓へ通した入力message数を含みます。

導入時の検査で、`test_fail_safe_runtime`の外部watchdog試験がWindowsで実OSへグローバルkey-up（`keybd_event`、SPACE）を送っていたことを検出しました。この試験はOS境界`_release_key_os`だけを記録用に置き換え、解放要求の内容（キー、input mode、HWND）を検証する形に直しました。本番の解放処理は変更していません。

### 既知の除外

- `test_live_input_tooltip`: Tk窓へ`<Enter>`/`<FocusIn>`を`event_generate`してtooltip表示を確かめる試験が、hosted Windows runnerではhover/focusを得られずに失敗します（PR #730の初回runで`AssertionError: unexpectedly None`、センチネルの遮断は0件）。入力の安全性とは無関係のため本gateの対象から外し、Ubuntuの全体テストでは引き続き実行します。Windows runnerで決定論的にする作業は#731で扱い、完了後に対象へ戻します。

### センチネルの範囲外

- 子プロセスのuser32呼出し。live-target smoke（`tools/run_windows_safety_ci.py`）、2048 E2E（`tools/run_windows_e2e.py`、`test_reference_game_2048_launcher`、`test_2048_campaign`）、外部watchdogの障害注入（`tools/run_fail_safe_ci.py`）は、runnerが自分で起動した窓を対象にする別ジョブで、このgateに含めません。
- user32以外の入力経路（DirectInput、ドライバ、UI Automation等）。プロジェクトは現在これらを使いません。
- Native Runtime（C++）からのWin32呼出し。

## 完了条件と検証場所の対応

| 完了条件 | 検証場所 |
|---|---|
| Windows上で主要integration testを実行できる | `safety-ci`: `SafetyGuard Python (windows-latest)`、`Windows platform gate (no live input)`。`fail-safe-ci`: 両ジョブのwindows-latest |
| target/window validationを自動検証できる | `test_safety_guard`（`WindowsTargetProbe`、対象の終了・再起動によるHWND/PID変化）、`test_runtime_target_boundary`、`test_window_selector`、`test_windows_capture_integration`（同一プロセス複数HWND、破棄・再生成） |
| coordinate transformのWindows回帰test | `test_windows_input`（window座標からscreen座標・client lParamへの変換、変換失敗時は送信しない）、`test_capture_region` |
| stale/duplicate/rate/hold等のSafety state | `test_fail_safe_runtime`（stale epoch・command age・observation、lease/TTL、release-all）、`test_fail_safe_numeric_boundaries`、`test_safety_guard`（hold上限、1000 click burst、repeat/cycle） |
| Native DLL/FFI load test | `safety-ci`: `Native Safety ABI (windows-latest)`（CTest、Python→DLLのSafety FFI、lifecycle） |
| package startup smokeと主要config load | `language-ci`: `Windows application bundle + startup smoke`（`--smoke-test`、DLL同梱、移設先の最小環境self-check） |
| CIで危険な実入力を発生させない | 本gateの実入力センチネル。live input系ジョブはrunnerが起動した窓だけを対象にし、既定では無効 |
| 実機-only検証項目が明示されている | 次節 |

## 実機-only検証項目

次はhosted runnerや本gateでは検証しません。実機での確認結果をPR本文に申告し、#84 Capability Matrixと#30 実ゲームE2E Gateで扱います。

- 実ゲーム（DirectX/OpenGL/custom renderer）への実入力と、`WM_PRINT`非対応窓のcapture（#30、#202）
- 高FPS・GPU captureとencodeの性能（#179）、Recorderのencoder start/stop/finalizeとsegment recovery（#201）
- DPI scaling・複数モニタ・RDP/ロック画面など、hosted runnerで再現できない表示環境
- 利用者の物理的な割込み（物理キー押下中のStop、F12による緊急停止の体感遅延）
- Broker/Guardian/Tk切替のkill-point受入とStop各段階の実測（Rust移行の#703、#704、#707、#709。本gateはPython経路の回帰検出で、これらの受入を代替しません）
