# E2E受入試験手順

## 目的

Issue #30 の第一段階受入として、人間が候補JSONを編集せずに `Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next Decision` を連続実行できることを確認する。

この手順は利用者とWindows E2E実装者向けです。画面captureから候補選択・live input・再観測までを確認し、サンプルUIと固定版2048の対象範囲を分けて扱います。

GitHub ActionsのWindows E2E jobは、固定サンプルGUIの24 action受入、pinned 2048の起動・実画面認識、Campaign Runnerによる600秒live-input campaignを別々に実行します。サンプルUIのmilestone runnerと2048 Campaign Runnerは異なるrunnerです。

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

launcherはChromiumのdevice scale factor（CSS pixelを画面pixelへ描く倍率）を0.75に設定します。画面の縦横が小さいWindows desktopでも、2048の盤面全体をcapture frameに表示するためです。

HWND（Windowsがwindowへ割り当てる識別番号）は、launcherが起動したbrowser process IDと2048を含むwindow titleの両方に一致するwindowから取得します。ephemeral portはWindowsが空き番号から選ぶ一時portです。専用browser profileは既存の通常ブラウザー設定やwindowを再利用しないために使います。`--app` windowは2048だけを開く専用windowです。

### 起動失敗と一時プロファイル削除失敗を調べる

起動したプロセスが終了した場合、launcherは`launched_pid`と`exit_code`をエラーへ記録します。25秒以内に対象ウィンドウを見つけられない場合は、`launched_pid`、直近の列挙で同じPIDが所有したタイトル付きウィンドウ数`owned_titled_windows`、別PIDでタイトルに2048を含むウィンドウ数`other_2048_windows`を記録します。件数は診断用であり、別PIDのウィンドウを操作対象に採用しません。ウィンドウのタイトル文字列はエラーへ記録しません。`owned_titled_windows=0`だけでは、タイトルのないウィンドウやWindows側の非表示状態まで存在しないと断定できません。

ブラウザー終了直後に専用プロファイルの削除が`PermissionError`になった場合、launcherは0.1秒間隔、最大10回で削除を試みます。待機時間の合計は最大0.9秒で、OSの削除処理自体にかかる時間は別です。最後まで削除できない場合や別のI/Oエラーは報告し、失敗したプロファイルの所有者を保持して`close()`を再実行できるようにします。起動と後処理が両方失敗した場合は、起動エラーを主例外として残します。後処理エラーは起動失敗の原因とはせず、処理中に発生した別の例外を示す`__context__`へ保持します。

E2E実装者は、起動失敗のPID・終了コード・件数と、後処理失敗の例外を分けて確認してください。これらの診断だけでは、CI上の起動タイムアウトの原因や、600秒のゲーム操作の成功は確定しません。

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

GitHub Actionsの `Windows sample loop and 2048 campaign E2E` jobで以下を実行する。

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

このコマンドは固定サンプルGUIの旧来の継続確認です。game-over後のbrowser再起動や2048 score/tile集計は行いません。pinned 2048のPR acceptanceには次節のCampaign Runnerを使います。

Windows実環境で以下を実行します。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_windows_e2e.py --duration 600 --steps 100000
```

`--duration 600` はsample windowのloopが最低600秒続くことを成功条件にします。sample milestoneが先に到達しないよう、duration指定時はsample側のstep上限を十分大きく設定します。

実ゲームを使う場合も `ContinuousE2ERunner` を利用し、`observation_probe` とmilestone判定だけを対象ゲームへ差し替える。競技オンラインゲーム、anti-cheat回避、DRM回避はProject-supported scope外とする。

## Pinned 2048 Campaign Runner

Campaign Runnerは、固定版2048を専用Edge/Chrome windowで起動し、画面認識・候補選択・window message input・再観測をprofileのdeadlineまで繰り返します。2048 episodeは画面状態を取得した後、DecisionPipelineからFailSafeRuntimeへ観測完了時刻を記録します。Windows CIで記録したaction-to-observationの最大値は2,666msです。FailSafeの観測期限は、この実測値より長い5秒に設定します。FailSafe watchdog（観測時刻の更新を監視する仕組み）は、5秒間新しい状態を記録できない場合に2048の実入力を停止します。game-overを画面から認識した場合は現在のbrowser episodeを閉じ、新しいepisodeを起動して続行します。一定回数の同一状態が続いた場合も、上限付きで新しいepisodeを起動して復旧を試します。攻略scoreやtile値の目標は設定しません。

Campaign RunnerのPR profileは600秒、Release profileは1800秒です。duration overrideを付けると選択profileの実行時間だけを置き換えます。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_2048_campaign.py --profile pr
python tools/run_2048_campaign.py --profile release
python tools/run_2048_campaign.py --profile pr --duration-seconds 30
```

このコマンドはWindows上でのみ実行できます。コマンドは専用browser windowへlive arrow-key inputを送り、dry-runではありません。既存browser windowや別アプリは操作対象にしません。初回起動にはpinned sourceを取得するためのネットワーク接続が必要です。`--browser`、`--cache-dir`、`--artifact-dir`を指定するとbrowser実行ファイル、検証済みsource cache、成果物の親directoryを変更できます。

Runnerは実行ごとにUTC時刻を含む新しいdirectoryを `build/e2e` 以下へ作り、`trace.jsonl` と `report.json` を保存します。既存成果物を上書きしません。traceはaction、state signature、score/tile、latency、episode lifecycle、stop reasonを記録し、生画面画像は保存しません。reportはprofile、目標時間、実時間、live input検証状態、action/step数、再起動/recovery数、last/peak score、scoreを得た観測数、最大tile、latency統計、game sourceとGit commitを記録します。`TesseractOcrRecognizer`とTesseract executableが利用可能な場合は画面OCRからscoreを記録します。OCR依存またはscore表示を認識できない場合、`last_score`と`peak_score`は`null`になることがあり、`score_samples`は読めたscore数を示します。reportの`metadata.score_ocr_enabled`はOCR recognizerの利用状態を示します。最大tileは認識した盤面から集計します。

成功条件は、live OS inputを1回以上確認したうえで600秒または1800秒のdeadlineへ到達し、hard failureなくepisodeを閉じて両成果物を保存することです。scoreまたは最大tileが高いことは成功条件ではありません。認識、capture、Safety、decision、input、stall recovery、episode起動、step上限、cleanupの問題は失敗categoryと理由として保存します。

## Sample E2E Trace format

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

2048 Campaign Runnerは `trace.jsonl` に `campaign_started`、`episode_started`、`step`、`episode_terminal`、`recovery_started`、`campaign_failure`、`cleanup_failure`、`campaign_summary` eventを保存します。別ファイルの `report.json` は一回のcampaignの集計結果です。

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

2048 Campaign Runnerは次の分類を `stop_reason` として保存する。`duration_reached`、`milestone_reached`、`safety_block`、`recognition_failure`、`capture_failure`、`decision_failure`、`execution_failure`、`live_input_not_verified`、`episode_start_failure`、`terminal_restart_limit`、`restart_failed`、`stall_limit`、`step_limit`、`artifact_write_failure`、`cleanup_failure`、`campaign_configuration`、`milestone_failure`、`unexpected_failure`。2048 CLIはmilestone callbackを設定しないため、通常の成功停止理由は `duration_reached` です。再利用するRunnerへcallbackを渡す実装者は、action後の `CampaignState` に対してboolを返してください。`true` は `milestone_reached`、例外またはbool以外の返却値は `milestone_failure` です。

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


## Reference Game Level 1-4

固定サンプルGUIをLevel 0とし、実ゲーム評価を段階化する。

| Level | Game | License | 主な評価 |
|---|---|---|---|
| 0 | Kadoka固定サンプルGUI | Project fixture | closed-loop contract / Windows live input |
| 1 | 2048 | MIT | keyboard / board state / basic planning |
| 2 | Memory Game | MIT | click target / memory / UI state / animation |
| 3 | RuggRogue | MIT code + upstream asset notices | menu / inventory / long-horizon state |
| 4 | Pygame Tetris | MIT | realtime / timing / repeated control |

Source URL、license path、固定commitは `config/e2e_reference_games.json` を正本とする。
CIでは `python tools/fetch_ci_games.py --all --clean` により `build/ci-games/` へ固定commitだけをmaterializeし、上流最新版を自動追従しない。

Level 1の実ゲームGateは Issue #176 を正本とする。Level 2-4はLevel 1でrunner/interfaceが安定した後、独立した実装単位へ分離する。

第三者ゲームの内部state/DOM等はmilestone検証に利用してよいが、Kadokaの通常Observation/Decision入力へ正解情報として直接注入しない。


## smoke-tiny Continuous Gameplay CI

`smoke-tiny` は単なるProvider疎通確認だけではなく、安価に長時間回せる実ゲーム耐久CIモデルとして扱う。

Level 1の2048では、Bundled Local Providerが実装された後、CIで次を標準Gateとする。

- Decision Providerは `smoke-tiny` を使用する
- DecisionをRuleProviderへfallbackしない
- 1 campaignは原則600秒
- game over等のterminalへ到達した場合は自動再開始し、campaign期限まで継続する
- candidate JSONを人間が編集しない
- 実画面Observation -> Candidate -> Provider Decision -> Safety -> Input -> Outcomeを通す
- score / max tile / episode数等は保存するが、当面はModel品質のhard thresholdにしない

Hard failure:
- Provider crash
- timeout budget exhaustion
- request/response schema violation
- `allowed_action_ids` 外のAction選択
- Safety bypass
- execution failure
- bounded recoveryで解消できないstall

これによりtiny Modelのゲーム攻略能力そのものではなく、**小型LLMを長時間Decision loopへ入れた時にKadoka全体が安全かつ連続して動くこと**を毎PRで検証する。

将来Level 2-4へ同じcampaign runnerを展開してよい。高難度Levelのscore/攻略性能をsmoke-tinyのmerge Gateへ直接しない。


## Noisy Language Provider 常設CI

高品質ModelだけをCIへ置くと、Core側が暗黙にModel品質へ依存していても検出しにくい。
そのため #178 の `NoisyLanguageProvider` を常設fixtureとして利用する。

Noisy ProviderはML runtimeを持たないseed固定pseudo-LMで、次の性質を持つ。

- allowed candidate内から擬似ランダムに選ぶ
- utilityの高い候補を優先しない
- reasonは意味の薄いword salad
- semantic outcomeはunknown、confidenceは0.1を固定して返す
- 同一seedでは再現可能
- Provider内部時間を計測可能

現在の常設Gateは`tools/run_noisy_provider_benchmark.py`です。実行結果は`build/noisy-provider/report.json`へ保存し、予算は`config/noisy_provider_performance_budgets.json`で管理します。旧Draftの`noisy_provider_ci.py`は重複実装として除外しました。

主な測定値:

```text
total_step_ms
provider_ms
non_provider_core_ms = max(0, total_step_ms - provider_ms)
```

初期budget:
- Provider選択・Outcomeの各p95 <= 1 ms
- Engine総時間・non-provider Coreの各p95 <= 150 ms on Linux baseline
- Windowsはperformance-ciの既存multiplierを適用

これは真のLLM性能benchmarkではなく、LLM待ち時間をほぼ除いたCore regression detectorである。

Level 1 / 2048 runner完成後は、同じNoisy Providerを実ゲームにも常設接続する。攻略scoreはGateにせず、crash、candidate grounding違反、Safety bypass、execution failure、回復不能stall等をhard failureとする。
