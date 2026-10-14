# E2E受入試験手順

## 目的

Issue #30 の第一段階受入として、人間が候補JSONを編集せずに `Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next Decision` を連続実行できることを確認する。

本受入は2つの形で同じrunnerを使う。

1. **PR Gate**: Windows上の固定サンプルGUIを明確な一区切り（24 action）まで完走する。
2. **10分継続試験**: 同じWindows runnerを600秒以上動作させ、停止理由と全step traceを保存する。

固定サンプルGUIは実ゲームの代替となる最小受入対象であり、候補をコード内JSONで注入しない。実際のウィンドウをCaptureし、`FrameAnalyzer` / `BrightRegionDetector` が検出したUI領域からActionCandidateを生成する。

## Windowsで固定版2048を起動する

Issue #245のlauncherは、MIT版2048をWindowsのEdgeまたはChromeで起動し、Issue #246/#247の実画面E2Eで使う対象windowを準備する。このlauncherは画面から盤面を認識したり、AIからキー入力を送ったりしないため、起動成功だけでAI gameplay完了とは判定しない。

launcherは `tools/reference_games/2048.source.lock.json` に記録したupstream commit `478b6ec346e3787f589e4af751378d06ded4cbbc` を取得する。lock fileはrepository、commit、Git tree SHA、全34ファイルのGit blob SHA、機械可読なMIT license ID、LICENSE.txtの著作権表示、license URLを保持する。Git tree SHAはcommit内のファイル構成を識別し、Git blob SHAは各ファイルの内容を識別するhashです。upstreamは[2048 by Gabriele Cirulli](https://github.com/gabrielecirulli/2048)であり、commitのLICENSE.txtにMIT Licenseと `Copyright (c) 2014 Gabriele Cirulli` が記録されている。

初回起動には `codeload.github.com` への接続が必要です。launcherは検証済みsourceを `build/reference_games/2048/<commit>` に保持し、次回以降は全ファイルの有無とGit blob SHAを照合してcacheを再利用します。既存cacheがlockと一致しない場合、launcherはcacheを削除・上書きせずエラーで停止します。原因を確認して再試行する利用者は、browser windowを閉じた後、該当するcommit folderだけをcache外へ退避してから起動し直します。

Windows利用者はリポジトリrootから以下を実行します。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_reference_game_2048.py
```

launcherはEdgeを優先して検索し、見つからない場合はChromeを検索します。`--browser`へ実行ファイルのpathを指定すれば検索結果を上書きできます。launcherは `127.0.0.1` のephemeral portだけでゲームを配信し、専用browser profileを使った `--app` windowを起動します。window titleに2048を含み、起動したbrowser processが所有するHWNDだけを選び、desktop内に最大1180×850でresizeします。launcherは前面化も要求しますが、Windowsがforeground変更を拒否する場合は既存windowを残して起動成功とし、JSON出力の `window_is_foreground` を `false` にします。desktopが640×480未満の場合、launcherはゲームwindowを表示できないことをエラーで伝えます。

HWND（Windowsがwindowへ割り当てる識別番号）は、launcherが起動したbrowser process IDと2048を含むwindow titleの両方に一致するwindowから取得します。ephemeral portはWindowsが空き番号から選ぶ一時portです。専用browser profileは既存の通常ブラウザー設定やwindowを再利用しないために使います。`--app` windowは2048だけを開く専用windowです。

Edge/Chromeが標準pathにない場合、利用者は実行ファイルpathを指定します。

```powershell
python tools/run_reference_game_2048.py --browser "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

利用者がゲームwindowを閉じるかCtrl+Cを押すと、launcherは起動したbrowser process tree、一時browser profile、localhost serverを終了します。検証済みsource cacheは次回起動に使うため残します。このlauncherはAI実入力を有効にしません。

Windows CIは次のintegration testでpinned sourceの配信、PIDに結び付いた2048 windowの特定、window起動後の終了処理を確認します。

```powershell
$env:PYTHONPATH = "src;."
$env:KADOKA_RUN_2048_INTEGRATION = "1"
python -m unittest discover -s tests -p "test_reference_game_2048_launcher.py" -v
```

## 2048 Screen Adapterをpixel fixtureとWindows実画面で検証する

この節は2048 Screen Adapterを変更する実装者向けです。Adapterは同じcapture時点のBGRA pixel（青・緑・赤・alphaの順で保持する画面byte列）とOCR（画像内の文字を読む処理）のObservationから盤面・現在score・game-over状態を作ります。AdapterはOCRが`GAME OVER`を読むか、満杯のboardに隣り合う同値tileがなく合法moveが存在しない場合にgame-overを返します。Adapterはboard confidenceが0.65以上でgame-overでない場合にUP/DOWN/LEFT/RIGHTのkeyboard候補を返します。confidenceは色と形状の一致度を表すheuristic値であり、正しさの確率ではありません。boardを認識できない場合、Adapterはboard confidenceを0、board矩形とstate signatureを`null`にし、arrow候補を返しません。明示的な`GAME OVER`文字列をOCRが検出した場合は、boardが未知でもgame-overを`true`にします。4方向候補は各方向で盤面が変化する保証ではなく、画面で認識したboardへの入力案です。Adapterはbrowser DOMやgame内部stateを判断入力に使いません。

`tests/fixtures/2048/standard.json` は `tools/reference_games/2048.source.lock.json` が固定するupstream commit `478b6ec346e3787f589e4af751378d06ded4cbbc` のCSS色、4x4の配置、cell寸法、game-over overlay、score OCR候補を記録します。`tests/test_game_2048_adapter.py` はこのデータから320×380 pixelのBGRA画面を合成し、Adapterの入力へ渡します。このfixtureはブラウザーから保存したPNGではなく、画面認識を同じpixel条件で再現する合成fixtureです。

合成fixtureを使った回帰テストは次のコマンドで実行します。

```powershell
$env:PYTHONPATH = "src;."
python -m unittest discover -s tests -p "test_game_2048_adapter.py" -v
```

テストは16 cellの値、`SCORE`と`BEST`を区別した現在score、game-over overlayとOCR文言、board・score・終了状態から計算するSHA-256 state signature、4方向候補、および2048以外の画面で候補を作らないことを確認します。合成fixtureの成功だけではWindowsの実window captureを検証したことになりません。

Windows上で固定版ゲームの実画面をcaptureする回帰テストは、launcher integration testで実行します。

```powershell
$env:PYTHONPATH = "src;."
$env:KADOKA_RUN_2048_INTEGRATION = "1"
python -m unittest discover -s tests -p "test_reference_game_2048_launcher.py" -v
```

このintegration testは起動したPIDの2048 windowを選び、HWND captureからboardと4方向候補を検査します。初回は `codeload.github.com` から固定版sourceを取得する必要があります。source取得またはwindow captureに失敗した実行は実画面検証の成功とせず、失敗理由を解消して再実行します。GitHub ActionsのWindows jobも同じ環境変数でこのintegration testを実行します。

## PR Gateの経路

```text
tools/e2e_sample_game.py
  -> WindowsScreenCapture
  -> FrameAnalyzer / BrightRegionDetector
  -> image ActionCandidate
  -> DecisionPipeline / RuleProvider
  -> Action Safety Evaluator
  -> WindowsInputExecutor (live mouse input)
  -> sample GUI state change
  -> capture again
  -> state-change outcome + E2E trace
```

GitHub Actionsの `Windows closed-loop sample E2E` jobで以下を実行する。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_windows_e2e.py --steps 24
```

成功条件は以下の全て。

- サンプルウィンドウを実際に列挙してHWNDを取得できる。
- Window captureからbright-region候補を自動生成できる。
- DecisionPipelineが候補を選択できる。
- Action Safety Evaluatorを通過できる。
- `WindowsInputExecutor` が `executed=True / mode=live` を返す。
- 入力後に画面signatureが変化する。
- 24 action後にsample milestoneへ到達する。
- `build/e2e/e2e_trace.jsonl` がartifactとして保存される。

## 10分継続試験

Windows実環境で以下を実行する。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_windows_e2e.py --duration 600 --steps 100000
```

`--duration 600` は600秒到達を成功条件にする。sample milestoneが先に到達しないよう、duration指定時はsample側のstep上限を十分大きく設定する。

実ゲームを使う場合も `ContinuousE2ERunner` を利用し、`observation_probe` とmilestone判定だけを対象ゲームへ差し替える。競技オンラインゲーム、anti-cheat回避、DRM回避はProject-supported scope外とする。

## Trace format

各stepはJSONLで次を保存する。

- `observation_id`
- `frame_signature`
- `recognized_elements`
- `candidate_actions`
- `selected_action`
- `provider`
- `reason`
- `execution.action_id / executed / mode / detail`
- `outcome.status / confidence / state_changed / after_signature`
- `latency_ms`

最後にsummaryを保存する。

```json
{
  "event": "summary",
  "success": true,
  "steps": 24,
  "elapsed_seconds": 0.0,
  "stop_reason": "milestone_reached",
  "live_input_verified": true
}
```

## 停止理由

runnerは少なくとも以下を区別する。

- `milestone_reached`
- `duration_reached`
- `repeated_observation`
- `controller_stopped`
- `wall_time_limit`
- `step_limit`
- `safety_block`
- `recognition_or_candidate_failure`
- `target_or_capture_failure`
- `execution_failure`

E2E failureは無理に成功扱いせず、traceをartifactへ残してcomponent blockerを特定する。

## Safety

PR Gateは専用サンプルウィンドウだけを対象とする。live inputを使うためCIでもdry-runではないが、対象HWNDを取得した上でそのウィンドウ座標から生成された候補のみを実行する。

#22 Action Safety Evaluatorを通過後に入力する。#33 SafetyGuard / Emergency Stop、#40 Fail-safe Runtimeのhard safety責務は別Issueとして維持する。

## 実ゲーム受入時の記録

実ゲームで試験する場合は次をrun metadataへ残す。

- game / version / scene
- Windows version
- Provider / model
- input mode
- start/end time
- total steps
- success/failure
- stop reason
- trace artifact path
- 発見したblockerと関連Issue

実ゲームE2Eで人間による候補JSON編集は行わない。認識誤りや候補不足で止まった場合は、その停止自体をfailure evidenceとして残す。
