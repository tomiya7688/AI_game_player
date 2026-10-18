# Campaign Runnerの実行契約

## 目的と対象読者

この文書は、Campaign Runnerを再利用するE2E試験の実装者が、試験時間、episode切替、停止分類、保存結果を同じ契約で扱うための仕様です。episodeはゲームを一度起動してから閉じるまでの試行単位です。画面認識、操作候補の生成、ゲーム攻略方針は対象ゲームごとのepisodeへ委ねます。

## 現在の実装範囲

`src/ai_game_player/campaign_runner.py` はLevel固有の `CampaignEpisode` と `CampaignConfig` を外部へ公開するimport入口です。`campaign_contract.py` は状態・操作結果・設定・failure分類の型を定義し、`campaign_loop.py` はepisode lifecycleとdeadline loopを実行し、`campaign_execution.py` はartifactと最終reportを管理します。`campaign_artifacts.py` はJSONL eventとmetric集計だけを担当します。Issue #247の2048実装は `tools/run_2048_campaign.py` がepisodeを作り、固定版2048のwindowだけを操作します。

2048 episodeは初回の操作可能な盤面を取得した後にFailSafeRuntimeをre-armし、`DecisionPipeline.record_observation()`で観測時刻を記録します。同じepisodeの2回目以降は、新しいwindow frameを取得した直後に観測時刻を記録し、画像解析と候補検証を続けます。盤面と候補を受理するまで次のactionへ進みません。観測期限は5秒で、観測時刻が5秒間更新されない場合はFailSafe watchdogが追加の実入力を拒否します。

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

`trace.jsonl` はcampaign開始、episode開始、terminal検出、action、stall recovery、failure、summaryを1行1 JSON objectで保存します。画面の生画像は既定では記録しません。`report.json` はduration、elapsed time、stop reason、action/step数、episode/restart/recovery数、last/peak score、scoreを得た観測数、最大tile、actionから次画面観測までのlatency集計、git/source metadataを保存します。`TesseractOcrRecognizer`とTesseract executableが利用可能な環境ではAdapterが現在scoreを記録します。OCRが利用できない場合、`last_score`と`peak_score`は`null`になり、`score_samples`は0、`metadata.score_ocr_enabled`は`false`です。最大tileは画像から認識したboardから集計します。

Runnerは既存のtraceまたはreportを上書きしません。出力先を使い回さず、新しいrun directoryへ保存します。CLIはUTC時刻を含むrun directoryを作成します。

`campaign_started` eventと`report.json`は、milestone callbackが設定されたかを `milestone_enabled` で示します。reportはcallbackが目標到達を返したかを `milestone_reached` で示します。traceはaction後に目標へ到達したとき `milestone_reached` eventを記録します。

## 固定版2048の認識失敗を画像から調査する手順

Windowsで固定版2048の開発用campaignを実行する開発者は、`--save-failure-evidence`を指定すると、認識失敗の原因を画像と画素データから調べられます。CLIは専用Edge / Chrome windowを起動して実入力を送信します。保存対象はCLIが起動した固定版2048のwindowであり、desktop全体ではありません。

```powershell
$env:PYTHONPATH = "src;."
python tools/run_2048_campaign.py --profile pr --save-failure-evidence
```

オプションを指定しない場合、CLIは画面を保存しません。指定した場合も、正常な観測中は最後に受理したframeを一枚だけメモリに保持します。認識timeout、画像解析例外、Adapterの状態欠落のいずれかで停止すると、対象episodeのruntime directory内に新しい`recognition-failure-*` directoryを作成します。既存bundleを上書きしません。

| ファイル | 調査できる内容 |
| --- | --- |
| `manifest.json` | 元の失敗理由、対象HWND、試行数などの診断値と、各frameの寸法・取得時刻・frame ID・BGRA SHA-256・盤面とconfidence |
| `last-playable.png` / `.bgra` | 最後に受理した盤面。episode内で一度も受理していなければ保存しません |
| `last-captured.png` / `.bgra` | 認識に失敗した最後の取得画面。新たなcaptureは行わず、実際に解析したframeを保存します |

PNGは画面確認用の無損失RGB画像です。`.bgra`は上から下へのrow順、paddingなし、1 pixelあたりB・G・R・Aの4 byteです。認識器へ同じ画素を再入力する場合は、manifestのwidth・heightと`.bgra`から`ScreenFrame`を作成してください。PNGではalphaを表示に使いませんが、BGRAには元のalphaも保存します。

開発者は、artifactを展開した後、リポジトリルートで次のコマンドを実行すると、保存済み画素を現行Adapterで再解析できます。pathは実際に展開した`manifest.json`へ置き換えてください。コマンドはブラウザ・画面取得・実入力・OCR・追加ライブラリを使わず、元のmanifestや画像を変更しません。

```powershell
$env:PYTHONPATH = "src;."
python -m tools.replay_2048_recognition "build/e2e/example/recognition-failure-example/manifest.json"
```

再解析コマンドはv1 schema、frame寸法、BGRA byte数、SHA-256を検証してから認識します。manifestは1 MiB、各BGRAは64 MiBを上限とし、bundle直下の固定名BGRA以外や、bundle外へ解決されるリンクは読みません。上限超過や破損の場合は元artifactと寸法を確認してください。JSON標準外のNaN・Infinityも拒否します。

JSONの検証は未知の診断項目も含む全階層に適用します。同じobject内の重複キーと、`1e999`のようにfloatへ変換すると無限大になる指数表記も拒否します。比較元の`game_state`はobjectである必要があります。`confidence`があればbool/nullを除く0〜1の数値、`board`があればnullまたは4×4配列、各cellはnull・整数0・2以上の2冪整数を指定してください。整数に見えるfloatのcellは受理しません。board/confidenceの項目欠落は従来どおり比較不能として許可します。

全frameの比較元を先に検証するため、後続frameのboard/confidenceが不正でも先行frameの画素読込・認識は始まりません。型違いを認識結果の差として扱わず、入力エラーの終了値2で理由を表示します。frameの寸法・ファイル・hash検査は、比較元の検証を終えた後にframeごとに行います。元の資料を修正したり、不正な値を正規化して一致に見せたりはしません。

標準出力のJSONには各frameの`recorded_game_state`、`replayed_game_state`、`visual_matches`、候補数が入ります。`visual_matches.board`と`visual_matches.confidence`は、記録値と一致すればtrue、不一致ならfalse、解析例外などで記録値がなければnullです。元のOCR入力を保存していないため、score・state_signature・OCRに由来する終了判定は一致比較に含めません。現行Adapterを変更した場合、不一致は認識器の挙動が変わったことを示し、保存データの破損とは区別します。

終了値0は再解析完了かつ比較済み項目に不一致なし、1は盤面またはconfidenceに不一致、2は入力・hash・解析のエラーです。記録も再解析も盤面未検出なら一致となるため、終了値0はゲームが操作可能だったことやCI失敗の解消を意味しません。nullの項目は比較未実施として確認してください。

`failure_detail`の`failure_evidence=`には保存を完了したmanifestのpathが入ります。保存に失敗した場合は`failure_evidence_error=`に理由を追記し、元の`recognition_failure`を維持します。reportのmetadataは`failure_evidence_enabled`と`screenshot_policy`（`off`または`recognition_failure_only`）で保存設定を示します。設定の有効化だけではファイルが保存されたことを意味しないため、manifestの存在とhashを確認してください。

`language-ci`のWindows campaignは保存オプションを指定し、失敗時も既存の`kadoka-windows-e2e-trace` artifactへbundleを含めます。診断保存によって、認識confidenceの最低値、観測の5秒期限、認識失敗時の停止分類は変えません。

## 終了画面の文字と再試行ボタンを保存画素で検査する

認識器を変更する開発者向けに、`tests/fixtures/2048/terminal-render/`へ固定版2048の終了画面を保存しています。PNGは目視確認用、`.bgra.zlib`はzlibで圧縮したpacked BGRA、JSONは寸法・画素のSHA-256・生成元commit・ブラウザ版・期待する盤面を記録します。fixtureのライセンスと生成条件は同directoryの`README.md`にあります。

`tests/test_2048_terminal_render.py`は、0.75倍・1倍の保存画素だけをAdapterへ渡し、OCRなしで16 cellの値、終了状態、候補0件、confidence 0.65以上を確認します。通常のunit testはブラウザ、Pillow、playwrightを使いません。headless描画を再生するテストなので、WindowsのHWND captureや実入力、自然なゲーム進行による終了を検証しません。保存した終了画面を認識できることだけでは、Issue #459の過去のCI失敗が解消したとは判断できません。

Windows上でfixtureを再生成する開発者は、EdgeまたはChromeと任意の開発依存を用意して次のコマンドを実行してください。生成ツールはhash検証済みの固定版ソースをlocalhostで配信し、専用headlessブラウザの保存状態に決定的な終了盤面を設定します。ゲーム本体の描画処理とCSSで終了画面を表示し、overlayのanimation完了後に撮影します。認識器にはゲーム内部状態やDOMを渡しません。

```powershell
python -m pip install Pillow playwright
$env:PYTHONPATH = "src;."
python -m tools.render_2048_terminal_fixture --output-directory tests/fixtures/2048/terminal-render
python -m unittest discover -s tests -p test_2048_terminal_render.py -v
```

生成ツールは指定先の同名PNG・BGRA・JSONを上書きします。初期出力先は`build/e2e/terminal-render`です。再生成後はPNGの文字とボタンを目視確認し、JSONの生成条件とunit test結果を確認してください。ブラウザのfont・描画環境によって画素hashが変わるため、再生成時のhash一致は合格条件にしません。保存済みBGRAと対応するJSONのhash一致はunit testで検査します。

## CIの実失敗画像で盤面の短い行欠落を検査する

認識器の開発者向けに、`tests/fixtures/2048/gdi-row-gap.bgra.zlib`と対応JSONへ、run `39037879872`のartifact `12103297123`から取得した失敗画素を保存しています。画像はゲーム終了画面ではなく、操作可能な盤面です。manifestにはWGC timeoutとGDI fallbackが記録されています。保存BGRAのSHA-256は`48dbf6cb3a407b2fb96864052edfb7f60e4975ef8c35ae2dd5b6cda2337e6e2b`で、CIとローカルの修正前Adapterはともにboard未検出・confidence 0でした。

失敗画像ではy=475・476の2行で最長palette帯が行検出条件を満たさず、旧処理は375×375 pixelの盤面を高さ243と130 pixelの候補へ分断しました。両候補は既存の縦横比条件を満たさず、操作候補が0件になりました。現在の`group_board_color_rows`は、行間距離を2 pixelまたは帯幅の3%以内で橋渡しします。3%は固定版CSSの盤面余白15/500です。2 pixelを超える欠落では帯が横方向に重なり、左右どちらかの端が同じ余白内に揃うことも要求し、近くのscore欄と盤面を分けます。盤面全体の縦横比・密度・認識confidenceの条件は変更しません。

`tests/test_2048_gdi_row_gap.py`は元の画素hash、期待する16 cellと盤面矩形、4方向の候補を確認します。余白より大きい欠落の拒否とscore欄の分離も確認します。通常画面と終了画面の既存テストも合わせて実行してください。実画像による再現は、この行分断による認識失敗の根拠です。WGC timeoutの原因や、過去に起きた他のconfidence低下まで特定したことを意味しません。
