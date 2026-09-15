# SafetyGuard / Emergency Stop 説明書

## 目的

Issue #33 の最終防衛線として、LLM・Recognition・Action Safety Evaluatorの判断に依存しない決定論的な入力Guardを提供する。

実行順は次の通り。

```text
Recognition / Decision
 -> Action Safety Evaluator (#22: 意味・異常性)
 -> SafetyGuard (#33: hard invariant)
 -> ActionExecutor
 -> Windows input
```

`Action Safety Evaluator` が SAFE を返しても、SafetyGuardが拒否したActionはOSへ送られない。SafetyGuard内部で例外が起きた場合もallowせず `guard_exception` としてfail-closedする。

## SafetyGuardが検査するもの

`SafetyGuardConfig` の既定値で次を検査する。

- Action kind: `click / double_click / key / wait` のみ
- click座標の存在
- 対象HWNDの存在・可視性
- 対象PIDのsession中固定
- global mouse入力時のforeground一致
- window/client area外座標の拒否
- `ALT+TAB / ALT+F4 / CTRL+ALT+DELETE / WIN / F12` の拒否
- key hold最大時間
- wait最大時間
- 1秒あたりAction数
- burst数
- 同一Action連続回数
- 短いAction cycle
- no-progress継続時の反復Action
- session最大Action数
- session最大時間

実Windows executorを利用するlive入力はHWND無しでは実行できない。テストや外部backendのために明示的に注入されたcustom executorはtarget validationを別backend責務として扱えるが、Action schema/rate/repeat/Emergency Stop等のSafetyGuard自体は通過する。

## Target validation

`WindowsTargetProbe` はAction直前に対象windowを検査する。

- `IsWindow`
- `IsWindowVisible`
- `GetWindowThreadProcessId`
- `GetWindowRect`
- `GetClientRect`
- `ClientToScreen`
- `GetForegroundWindow`（global mouse入力時）

最初に観測したPIDをsessionへbindし、その後同じHWNDが別PIDへ変化した場合は `target_pid_changed` で停止する。ゲーム終了によるwindow消失は `target_invalid` で停止する。

click座標は単なるwindow矩形内だけでなくclient area内であることを要求する。title barやwindow borderへの誤入力を許可しない。

## Emergency Stop

`EmergencyStopMonitor` はRecognition / Decision loopとは別daemon threadでF12を監視する。F12検出後は共有 `EmergencyStop` が `EMERGENCY_STOP` になり、以降のActionを拒否する。

停止操作は現在実装されている実行状態に依存してはならない。現在の最低保証は次の通り。

- idle / Action間: 次の入力を実行しない
- Recognition / Decision処理中: その処理の完了を待たずEmergency Stop状態を独立してlatchし、その後の入力を実行しない
- wait中: 約20ms単位でstopを確認し、途中で中断する
- key hold中: stopを確認してheld keyをreleaseし、中断する
- double-click途中: 1回目と2回目の間もstopを確認し、停止後の2回目を送らない
- Safety block中 / target loss中: 既にfail-closedであり、停止状態から勝手に復帰しない
- re-arm後: 再度停止操作を受け付ける

ここでいう「停止」は、少なくともOS/Gameへの追加入力が停止し、held inputが解放され、明示re-armまで新規入力が拒否されることを意味する。任意の第三者model processやOS processを強制killすることは #33 の保証には含めない。process crash / OOM / hard kill時のout-of-process lease/watchdogは #40 の責務とする。

再開は暗黙には行わない。`RunController.start()` または `DecisionPipeline.rearm_safety()` の明示操作をre-armとする。

## Windows input境界

`mouse` modeはglobal cursorを利用するため、SafetyGuardは対象windowがforegroundであることを要求する。

`window_message` modeのkey/clickは対象HWNDへ直接messageを送り、global cursorへfallbackしない。clickではscreen座標をclient座標へ変換してから `WM_LBUTTONDOWN / WM_LBUTTONUP` を送る。

wait、key hold、double-clickのinter-click intervalはstop-awareにし、長さのある入力処理でEmergency Stopを無視する区間を作らない。

## Native Runtime

C++ Native Runtime ABIに `kadoka_safety_validate_action` を追加した。Native側では高頻度かつ単純な次のinvariantを再検査する。

- action kind
- viewport
- click座標
- key hold上限
- key code
- Alt+Tab / Alt+F4 / Ctrl+Alt+Delete / Win / F12

`kadoka_runtime_query()` は `KADOKA_CAP_SAFETY` を公開する。

Python側はbundle等でNative Runtimeを発見できた場合にこのvalidatorも通す。Native libraryが存在しない開発環境ではPythonの同等Guardを利用できるが、Native validatorをロードした後のcall failureはfail-closedになる。

Native APIはUbuntu / Windowsの両方でCTestを実行する。

## Config

主な既定値:

| 項目 | 既定値 |
| --- | ---: |
| max actions / 1 sec | 30 |
| burst actions / 5 sec | 120 |
| same action repeats | 100 |
| cycle repeats | 25 |
| no-progress repeats | 25 |
| session actions | 10000 |
| session duration | 7200 sec |
| key hold | 1.0 sec |
| wait | 5.0 sec |

`SafetyGuardConfig.to_dict()` / `from_dict()` で設定を保存・復元できる。

Safety required項目を一般ユーザー向け設定から完全無効化することは想定しない。値を変更する場合も0や負数は受理しない。

## Audit log

`SafetyLog` は各ActionについてJSONLで次を保存する。

```json
{
  "action_id": "continue",
  "allowed": true,
  "code": "allowed",
  "reason": "deterministic SafetyGuard checks passed",
  "state": "active",
  "target_pid": 1234,
  "timestamp": 0.0
}
```

`DecisionPipeline` ではgame directory直下の `safety_guard.jsonl` に保存する。

意味的Safetyの `action_safety.json` とは分離し、どの層がActionを止めたか判別可能にする。

## no-progress接続

`DecisionPipeline.record_safety_outcome()` からSafetyGuardへscreen progressを通知する。`ongoing / success` またはscreen signature change evidenceがある場合はprogressあり、それ以外はno-progressとして蓄積する。

no-progressが閾値へ達した状態で同一Actionを繰り返すと `no_progress` でhard stopする。

高度なOutcome Detection / Fusionは #34 で実装し、この接続点をそのまま利用する。

## 専用Safety CI

`.github/workflows/safety-ci.yml` をSafetyGuard / Emergency Stop専用のblocking候補Gateとして分離する。通常の `tests` / `language-ci` が通っていてもSafety専用Gateが失敗した場合、Safety変更は正常とはみなさない。

専用CIは次の3層を検証する。

1. `SafetyGuard Python (Ubuntu / Windows)`
   - SafetyGuard回帰
   - ActionExecutor境界
   - WindowsInput境界
   - EmergencyStopMonitor / explicit re-arm
2. `Native Safety ABI (Ubuntu / Windows)`
   - C++20 build
   - `kadoka_safety_validate_action`
   - Native CTest
3. `Windows Emergency Stop live-target smoke`
   - 実HWNDの固定サンプルGUIを利用
   - idle / Action間から停止可能
   - Recognition / Decision相当のupstream処理中でも独立して停止状態をlatch
   - wait途中の停止
   - key hold途中の停止とrelease
   - double-click途中の停止
   - explicit re-arm後のみlive input復帰
   - invalid coordinate拒否
   - target window消失後のfail-closed
   - `safety_guard.jsonl` artifact保存

Windows smokeの最終結果には `all_implemented_states_stoppable=true` を要求する。新しい長時間実行状態や入力種別を追加した場合は、この状態マトリクスへ同時にケースを追加する。

## Test coverage

Python regression testで最低限次を固定する。

- client area内の正常click
- window外 / client area外click
- target window消失
- restart等によるPID変化
- background targetへのglobal input
- Alt+Tab / Alt+F4 / Ctrl+Alt+Delete / Win / F12
- hold timeout
- 1000 click相当burst
- repeat / cycle
- no-progress
- Guard内部例外
- Emergency Stop / explicit re-arm
- held input release
- JSONL audit

さらに既存Windows closed-loop E2E Gate (#30) をSafetyGuard込みで通し、実入力経路がGuardを迂回していないことを継続検証する。Safety専用CIはこれとは別に、停止可能性そのものを独立Gateとして検証する。
