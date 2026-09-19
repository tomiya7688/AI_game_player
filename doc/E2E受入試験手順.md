# E2E受入試験手順

## 目的

Issue #30 の第一段階受入として、人間が候補JSONを編集せずに `Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next Decision` を連続実行できることを確認する。

本受入は2つの形で同じrunnerを使う。

1. **PR Gate**: Windows上の固定サンプルGUIを明確な一区切り（24 action）まで完走する。
2. **10分継続試験**: 同じWindows runnerを600秒以上動作させ、停止理由と全step traceを保存する。

固定サンプルGUIは実ゲームの代替となる最小受入対象であり、候補をコード内JSONで注入しない。実際のウィンドウをCaptureし、`FrameAnalyzer` / `BrightRegionDetector` が検出したUI領域からActionCandidateを生成する。

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
