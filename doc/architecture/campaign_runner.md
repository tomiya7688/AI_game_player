# Campaign Runnerの実行契約

## 目的と対象読者

この文書は、Campaign Runnerを再利用するE2E試験の実装者が、試験時間、episode切替、停止分類、保存結果を同じ契約で扱うための仕様です。episodeはゲームを一度起動してから閉じるまでの試行単位です。画面認識、操作候補の生成、ゲーム攻略方針は対象ゲームごとのepisodeへ委ねます。

## 現在の実装範囲

`src/ai_game_player/campaign_runner.py` はLevel固有の `CampaignEpisode` と `CampaignConfig` を外部へ公開するimport入口です。`campaign_contract.py` は状態・操作結果・設定・failure分類の型を定義し、`campaign_loop.py` はepisode lifecycleとdeadline loopを実行し、`campaign_execution.py` はartifactと最終reportを管理します。`campaign_artifacts.py` はJSONL eventとmetric集計だけを担当します。Issue #247の2048実装は `tools/run_2048_campaign.py` がepisodeを作り、固定版2048のwindowだけを操作します。2048 episodeの `observe()` は画面状態を取得した後に `DecisionPipeline.record_observation()` を呼び、FailSafeRuntimeの観測完了時刻を更新します。Windows CIで記録したaction-to-observationの最大値は2,666msであるため、2048 episodeの観測期限を5秒に設定します。観測時刻が5秒間更新されない場合、FailSafe watchdogは追加の実入力を拒否します。

PR用profileは600秒、Release用profileは1800秒です。利用者は `--duration-seconds` で実行時間を上書きできます。Campaign Runnerの成功条件は設定時間までlive OS inputを少なくとも1回検証し、設定したstep、terminal restart、cleanup条件に違反せず終了することです。2048のscoreやtile値は記録値であり、成功判定のしきい値ではありません。

呼び出し側は `CampaignRunner.run()` に任意の `milestone_probe(CampaignState) -> bool` を渡せます。Runnerは各live action後の画面状態をcallbackへ渡し、`true` ならdeadlineを待たず `milestone_reached` で終了します。`false` ならprofile deadlineまで続けます。callbackの例外またはbool以外の返却値は `milestone_failure` として記録します。Issue #247の2048 CLIはcallbackを渡さないため、600秒または1800秒のdurationだけで停止します。

## Level固有episodeの契約

`CampaignEpisode` は以下の4操作を提供します。

| 操作 | 入力 | 出力 | 失敗時の扱い |
| --- | --- | --- | --- |
| `observe()` | なし | `CampaignState` | capture、recognition失敗をhard failureとして停止 |
| `act()` | 直前に観測した状態 | `CampaignActionResult` | decision、Safety、OS inputのhard failureとして停止 |
| `record_outcome(status, confidence, evidence)` | 直前actionの画面変化 | なし | Safety outcomeを記録できない場合は停止 |
| `close()` | なし | なし | episode resourceの終了失敗をreportへ記録 |

`CampaignState.signature` は同じゲーム状態かを比較する画面由来の識別値です。`terminal` は画面Adapterが終端画面を認識した結果です。`score` と `max_tile` は未認識時に `None` を許します。Episodeは `act()` が読み取る画面を `observe()` の画面と一致させ、別時点の画面に対して操作を実行してはいけません。

`CampaignActionResult.executed` が `true` で、`mode` が `live` でない操作は、実入力を検証した結果として数えません。dry-runだけでdeadlineへ到達したrunは失敗です。

## Episode lifecycleとbounded recovery

1. Runnerは最初のepisodeを作り、画面状態を観測します。
2. Runnerは終端状態でない場合にactionを1回実行し、同じwindowの画面を再観測してSafetyへ結果を記録します。
3. 画面状態が変わらない操作が `stall_action_limit` 回続くと、Runnerはepisodeを閉じて新しいepisodeを作るstall recoveryを試します。回復回数が `recovery_limit` に達した後も停滞が続けば `stall_limit` で停止します。
4. Adapterがterminal状態を返すと、Runnerは現在episodeを閉じ、別episodeを起動して新しいgameを開始します。再起動回数が `terminal_restart_limit` を超える前に停止します。
5. Runnerはaction後の画面状態を設定済みのmilestone callbackへ渡します。callbackが `true` を返した場合、Runnerはmilestone到達eventを記録してloopを終了します。
6. milestoneが設定されていない場合、またはcallbackが `false` を返した場合、Runnerはduration deadlineまでloopを続けます。hard failureが発生した場合はfailure codeで停止し、episodeを閉じた後にJSONL traceとJSON reportを保存します。

どちらのepisode再生成も、deadline、step上限、起動失敗、capture失敗、recognition失敗、decision/Safety/execution失敗を無視しません。Recoveryは成功を仮定せず、新windowでboardを再認識してから次actionを許可します。

## 停止分類

`stop_reason` は次の機械判定用codeのいずれかです。

| Code | 意味 |
| --- | --- |
| `milestone_reached` | 呼び出し側のcallbackがaction後の状態を目標到達と判定 |
| `duration_reached` | deadlineまでlive inputを検証してcampaignを完了 |
| `safety_block` | Action SafetyまたはSafety outcome記録が操作を拒否 |
| `recognition_failure` | 2048 boardまたは実行可能候補を認識できない |
| `capture_failure` | 対象windowからframeを取得できない |
| `decision_failure` | Provider、Decision Reliability、decision pipelineがactionを確定できない |
| `execution_failure` | OS input実行器が操作を完了できない |
| `live_input_not_verified` | live OS inputが1回も確認できない、または操作結果がliveでない |
| `episode_start_failure` | browser windowまたはepisodeの起動に失敗 |
| `terminal_restart_limit` | game-over後の再起動回数が上限に達した |
| `restart_failed` | 再起動したepisodeがplayable screenから始まらない |
| `stall_limit` | 上限回数のstall recovery後も画面状態が進まない |
| `step_limit` | deadlineより先に最大action数へ到達 |
| `artifact_write_failure` | traceまたはreportを保存できない |
| `cleanup_failure` | 正常終了後にcapture、pipeline、browserを閉じられない |
| `campaign_configuration` | durationまたは出力pathが許容条件に合わない |
| `milestone_failure` | milestone callbackが例外を出した、またはbool以外を返した |
| `unexpected_failure` | 既知の分類に当てはまらない例外が発生 |

Runnerの `success` は `duration_reached` または `milestone_reached` で停止し、live input検証、episode cleanup、artifact保存がすべて成功した場合だけ `true` です。callbackを設定しない2048 CLIは `duration_reached` だけを成功停止理由にします。

## Artifactsとmetric

`trace.jsonl` はcampaign開始、episode開始、terminal検出、action、stall recovery、failure、summaryを1行1 JSON objectで保存します。画面の生画像は記録しません。`report.json` はduration、elapsed time、stop reason、action/step数、episode/restart/recovery数、last/peak score、scoreを得た観測数、最大tile、actionから次画面観測までのlatency集計、git/source metadataを保存します。`TesseractOcrRecognizer`とTesseract executableが利用可能な環境ではAdapterが現在scoreを記録します。OCRが利用できない場合、`last_score`と`peak_score`は`null`になり、`score_samples`は0、`metadata.score_ocr_enabled`は`false`です。最大tileは画像から認識したboardから集計します。

Runnerは既存のtraceまたはreportを上書きしません。出力先を使い回さず、新しいrun directoryへ保存します。CLIはUTC時刻を含むrun directoryを作成します。

`campaign_started` eventと`report.json`は、milestone callbackが設定されたかを `milestone_enabled` で示します。reportはcallbackが目標到達を返したかを `milestone_reached` で示します。traceはaction後に目標へ到達したとき `milestone_reached` eventを記録します。
