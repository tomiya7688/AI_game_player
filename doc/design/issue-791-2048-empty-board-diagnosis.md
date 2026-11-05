Spec-Status: READY
Depends-On: none（#459・#202・#247は親/参照。PR #660/#662/#668の診断は統合済み）
Decision-Gaps: none
Parent: #459

# Issue #791 2048の空盤面・HTMLActuator欠落の原因カテゴリ診断

署名: Tomiya

## 成果物

1. 失敗1件のFrame・HTTP・JS・DOMの証拠を、同じ**相関ID**と同じ親process単調時計で結ぶ診断記録 `reference-startup-correlation/v1`（明示診断時のみ）。
2. 記録から原因カテゴリを決める純粋関数 `classify_startup_failure`。証拠が欠けたらカテゴリを推測せず `evidence_incomplete` にする（fail-closed）。
3. 各境界の負例fixtureと回帰テスト。
4. 保存済みreportから起動時間のp50/p95と失敗率を対照と比べるoffline集計tool。
5. 製品の挙動修正は**今回は行わない**。理由と、カテゴリ確定後に修正する条件を「手順7」に定める。

## 利用者の操作

なし。CIの既存フラグ `--save-browser-diagnostics`（campaign・Windows planning comparison）で記録が有効になる。集計は開発者が手元で実行する。

```bash
PYTHONPATH=src:. python -m tools.summarize_2048_startup_runs \
  --cohort pr-<sha>-a1=<artifact dir> --cohort main-<sha>-a1=<artifact dir> --output <file>.json
```

## 現状調査（確認済みと推測の区別）

確認済み（原典を読んだもの）:

- 失敗: PR run 41355115740 / job 124993406230、head 37bbafb、seed481 Baseline。45秒の初期観測期限で0入力。job logの `reference_game_failure_diagnostics` を本設計時に再取得して照合した。CDPは `ready_state=complete`・visible・focused、GameManager/Input/Storage constructor true、`actuator_constructor=false`、`tile_count=0`。server write summaryにはpage・main_css・7 scriptが各1件HTTP200で完了と記録され、`html_actuator_js`・`grid_js`・`tile_js` の行が無い。page 4,135 bytesのSHA256はseed付きの期待byte列と一致。
- この記録は旧schemaで、`requests_started`・`headers_completed`・`failed_requests` が無い。よって3 scriptの「要求未到達」「header前の失敗」「browser側load失敗」を区別できない（`doc/architecture/reference_http_accept_queue.md` と同じ結論）。
- 固定 `index.html` のscriptは同期読込（async/defer無し）で、順序はbind→classlist→animframe→keyboard_input_manager→**html_actuator→grid→tile**→local_storage_manager→game_manager→application。後続のgame_manager.jsが実行済み（constructor true）なので、3 scriptは「永久に保留」ではなく、browserがエラーで終わらせて解析を続けたと読める。**推測**: 同期scriptの読込エラー時に後続を続ける挙動からの推論で、Windows実機のエラー種別は未確認。
- 失敗後に既に追加済みのもの: 開始/header完了/失敗phaseのcounter（明示診断時）、固定11件のResource Timing（status・size・存在boolean）、listen backlog 32（`ReferenceGameHTTPServer`）、Linux Chromiumでabort/404/空bodyが同じ見た目を作る再現（`doc/validation/reference_browser_resource_timing_linux.json`）。
- 再発の調査（2026-11-05、`gh run list` 直近200件のlanguage-ci）: failure 10件のうち確認できたjobは、seed479/480の比較依存（OCR実行ファイル）導入失敗（job 126957509927・126957478628、比較実行前）とsample loopの起動競合（run 41958841719、#794で対処済みの `wait_for_sample_ready` を含まないhead）。**37c30統合後の空盤面再発は、確認した範囲では見つかっていない**。通信のTLS timeoutで全10件は照合しきれず、残りは未確認。

現状の不足（本設計で埋める）:

- server counterに時刻が無く、TCP受理数と要求行の数を数えていない。受理済みで要求行が来なかった接続（RSTやheader前切断）を数えられない。
- 失敗Frame（`planning_failure_header` のframe/PNG SHA）、server summary、CDP結果が別々のJSONにあり、同じ起動世代であることを示す共通IDが無い。
- 境界ごとの判定が人の読解に頼っている。

## 入出力

### 相関ID・時計

- `correlation_id`: `LoopbackStaticServer.start` が起動世代ごとに `secrets.token_hex(16)` で作る。秘密ではなく、URLや本文には入れない（ページのbyte列を変えない）。
- 時刻はすべて親processの `time.monotonic_ns()` を、その世代のserver開始時刻 `t0` からの相対ms（小数1桁、0〜3,600,000にclamp）で表す。HTTP handler・Frame取得・CDP呼出しは同じ親process内なので同じ時計になる。renderer側の時刻（Resource Timingの値）は混ぜず、既存どおり存在booleanだけを使う。

### `reference-startup-correlation/v1`（`failure-diagnostics.json` の `correlation` 節）

| key | 型・上限 | 内容 |
|---|---|---|
| `schema` | 固定文字列 | `reference-startup-correlation/v1` |
| `correlation_id` | 32桁hex | 起動世代ID |
| `episode` / `seed` | int / uint32かnull | 既存値 |
| `server.connections_accepted` | int（2^31-1で飽和） | `process_request` 到達数 |
| `server.connections_without_request_line` | int（同） | 要求行が空のまま終わった接続数 |
| `server.request_line_errors` | int（同） | 要求行の解析失敗数 |
| `server.requests_parsed` | int（同） | 要求行を解析できた要求の総数（allowlist外の `/favicon.ico` 等を含む） |
| `server.resources.<name>` | 既存counter＋時刻 | 既存の固定12名だけ。`first_started_ms`・`first_headers_ms`・`first_completed_ms`・`first_failed_ms`（無ければnull） |
| `server.recording_incomplete` | bool | 既存のlatch |
| `frame` | object | `frame_sha256`・`png_sha256`（既存header値の写し）、Frame自体の取得時刻 `captured_ms`（取得元が時刻を持たなければnull）と保全時刻 `preserved_ms` |
| `cdp` | object | `started_ms`・`finished_ms`・既存のrender/timing結果のsha256 |
| `target_matches_generation` | bool | CDPの対象URLがこの世代のserver URLと完全一致したか（URL自体は出さない） |
| `classification` | object | 下記 |

全体の上限は既存の `MAX_DIAGNOSTIC_BYTES` と `browser` 節16 KiBを変えない。増えるfieldは固定数で、CDP envelopeの256 field上限内に収まることをテストで確認する。

### `classify_startup_failure(record) -> dict`

戻り値は `{"category", "boundary", "evidence_complete", "missing": [<固定名>...], "next_check"}`。categoryは固定集合で、境界の順に最初に該当したものを返す。

| 順 | category | 条件（全て満たす） | boundary |
|---|---|---|---|
| 0 | `evidence_incomplete` | schema不一致、相関IDの不一致、`target_matches_generation` がtrueでない、`recording_incomplete`、必須節の欠落（旧schemaを含む）のいずれか | 判定不能 |
| 1 | `connection_without_request` | 欠落scriptがあり、`connections_without_request_line` > 0 | TCP受理→要求行 |
| 2 | `request_not_observed` | 欠落scriptの `requests_started` = 0 かつ 受理数 = `requests_parsed` + 要求行なし + 解析失敗 | browser送信/TCP受理前 |
| 3 | `server_failed_before_headers` | `failed_requests` > 0 かつ phase が `request`/`headers` | server→header |
| 4 | `server_failed_in_body` | phase `body`、または completed=0 で header完了済み | server→body |
| 5 | `served_not_executed` | 全scriptがHTTP200・完了・既知SHA一致、かつ必要constructorのいずれかがfalse | JS load→constructor |
| 6 | `constructed_no_dom_tiles` | 全constructor true、`tile_count` = 0 | constructor→DOM |
| 7 | `dom_present_frame_empty` | `tile_count` > 0 かつ表示boolean true、Frameは空盤面 | DOM→RAF/compositor/Capture |
| 8 | `frame_nonempty_not_recognized` | Frameに空でない盤面があるのに認識が盤面を返さない | Capture→Recognizer |
| 9 | `unclassified` | 上記いずれも非該当 | 不明 |

- 「欠落script」= 固定allowlistのscriptで `completed` が0のもの。
- browserの投機的preconnectは要求行なしの接続を正常時にも作り得る。そのためcategory 1は「候補」であり、正常runの同じcounter（下記の対照）が0に近いことを集計で示すまで根因と呼ばない。`classification.control_required=true` を付ける。Resource Timingの `entry_count=0`・API無しはcategoryの根拠に使わず、`next_check` の補足にだけ入れる（entry無しを成功や原因と扱わない）。
- 空盤面かどうかは既存の失敗header（16空セルのboolean）を使い、検出器の閾値は変えない。
- この関数は判定の記録だけを行い、入力・Planner・lease・Stop・失敗判定には戻さない。

## 手順

1. **server世代の記録**（`tools/reference_browser_diagnostics.py`・`tools/reference_game_2048_launcher.py`）
   - `ResourceResponseCounts` に `t0` と時刻fieldを追加し、`started`/`headers_completed`/`record`/`failed` で最初の時刻だけを書く。時刻取得や記録の例外は既存どおり `recording_incomplete` をlatchし、元の送信例外を上書きしない。
   - 接続counter（受理・要求行なし・解析失敗・解析済み要求の4整数）は明示診断時だけ有効で、失敗時に限らず各episodeのreportへ `server_connection_summary` として出す（正常runの対照分布のため。URL・時刻は含めない）。`ReferenceGameHTTPServer` を変えず、launcher内で診断用subclassを作り `process_request` で受理数を数える。handlerの `handle_one_request` で要求行が空なら `connections_without_request_line`、`parse_request` がfalseなら `request_line_errors` を数える。backlog 32・HTTP/1.0・daemon handler・close/port解放・finite listenは変えない。診断無効時のclass・挙動は現状と同一。
2. **Frame・CDPとの結合**（`tools/planning_failure_header.py`・`tools/reference_episode_failure_diagnostics.py`・`tools/run_2048_campaign.py`・`tools/compare_2048_windows_planning.py`）
   - 失敗headerはFrameの取得時刻 `captured_ms` と保全時刻 `preserved_ms` を、CDPは呼出しの前後で `started_ms`/`finished_ms` を記録する。順序は既存どおり「Frame保全→heartbeat→CDP（1回限り）」。CDPの開始が `preserved_ms` より前、または `captured_ms` がnullなら `evidence_incomplete`。
   - `correlation` 節を組み立てて `classify_startup_failure` の結果を加え、既存の `failure-diagnostics.json` に保存する。失敗の種類・終了コード・reportの成否は変えない。
3. **分類関数**（新規 `tools/reference_startup_correlation.py`）: 上表を固定順で評価する純粋関数。入力は辞書だけで、I/O・時計・乱数を使わない。
4. **集計tool**（新規 `tools/summarize_2048_startup_runs.py`）: `--cohort 名前=ディレクトリ` を複数受け取り、各ディレクトリ配下の既存reportと `failure-diagnostics.json` だけを読む（network無し、symlink・巨大ファイルは拒否）。cohortごとに、server開始から最初の盤面受理までのms（p50/p95）、空盤面失敗の件数/試行数、categoryの内訳、成功episodeの `server_connection_summary`（要求行なし接続の分布）を出す。push/PR・修正後main・seed・初回/再実行はcohort名で分け、合算しない。成功した再実行だけのcohortは「失敗率の改善の証拠」と表示しない（`retry_only=true` を出す）。
5. **既存Linux probe**（`tools/probe_reference_browser_resources.py`）: 10条件のreportに各条件の `classify_startup_failure` 結果を加える。開発専用・Playwright任意依存のまま、CI必須にしない。
6. **文書**: `doc/architecture/reference_browser_post_failure_diagnostics.md` に相関記録とカテゴリ表、`doc/architecture/reference_http_accept_queue.md` に接続counterを追記。`doc/versions.md`。
7. **修正の判断（今回は修正しない）**: 37c30統合後の再発は確認範囲で見つからず、確定した根因が無い。根因の無い修正（期限延長・retry・reload・Chrome flag・空盤面の許可）は失敗を隠すため行わない。Windowsで再発し、categoryが1〜8のいずれかに確定し、そのcategoryを決定論的に再現する負例を作れた場合だけ、後続Issueで最小修正を行う。例: 1/2ならserverの受理経路、3/4ならhandlerの送信経路、5〜7はbrowser側の境界の追加切り分け。

### 失敗時の挙動（fail-closed）

- 空盤面の間は従来どおり `capture_playable_state` が盤面を受理せず、入力ゲートはSAFE_IDLEのまま。45秒の期限で失敗する。本変更は待ち時間・入力・再試行を増やさない。
- 診断の失敗（例外・上限超過・相関不一致）は `evidence_incomplete` または既存の `unavailable` になり、元の失敗を上書きしない。カテゴリを推測で埋めない。
- 診断は既存の明示フラグ時だけで、通常のuser起動経路・Source・Planner・学習に流さない。URL・token・本文・Console・盤面値・得点・storageは出さない（既存のPrivacy境界を維持）。

## テスト

| # | 試験（`tests/`） | 対応する受入条件 |
|---|---|---|
| T1 | `test_reference_startup_correlation.py`: 各category 0〜9の最小記録で期待category・boundaryになる。順序の境界（1と2が同時に成立する等）で先の境界が勝つ | AC1 |
| T2 | 同: 37bb seed481の実記録（旧schema、本文値はlog由来の公開済みcounterのみ）を固定fixtureにし、`evidence_incomplete` と `missing=[requests_started,...]` になる。原因を命名しない | AC1, AC3 |
| T3 | 同: 相関ID不一致・`target_matches_generation=false`・CDP時刻がFrameより前・`recording_incomplete` がそれぞれ `evidence_incomplete` | AC3 |
| T4 | `test_reference_game_http_server.py`: loopbackで実socketを開いて要求行を送らず閉じる→`connections_without_request_line=1`、他のcounterは不変。allowlist外のGETは `requests_parsed` だけを増やす。欠落script付きの記録で分類1・`control_required=true` | AC2 |
| T5 | 同: テスト用handler subclassで `html_actuator.js` のheader送信前に例外→ `failed phase=headers`、時刻は `first_failed_ms` のみ。分類は3。body途中の例外で4 | AC2 |
| T6 | 同: 診断無効時は接続counter・時刻fieldが存在せず、server class・backlog 32・応答byte列が現状と同一 | AC4 |
| T7 | 同: 時刻・counter記録の例外注入で元の送信例外が保たれ `recording_incomplete=true` | AC3, AC4 |
| T8 | `test_reference_episode_failure_diagnostics.py`: Frame保全前はCDPを呼ばず、`correlation` 節の時刻順がFrame→CDPで、byte上限超過時は既存の縮退形になる | AC3 |
| T9 | `test_summarize_2048_startup_runs.py`: 疑似reportで p50/p95・失敗率・category内訳がcohortごとに出る。cohortを合算しない。成功した再実行だけのcohortは `retry_only=true` | AC5 |
| T10 | 既存の入力・Stop・lease・Planner・score・45秒期限の関連テスト（`test_2048_campaign*`、`test_compare_2048_windows_planning*`）が無変更で通る。`finish_task.bat` 全体 | AC4 |
| T11（手動） | Linuxの `probe_reference_browser_resources` 10条件で abort/404/空bodyが期待categoryになる（CI非必須、結果を `doc/validation/` に保存） | AC2 |

テストは決定的・外部network不要（loopbackのみ）。時刻は注入可能な時計で固定する。

## 受入条件との対応

| Issueの完了条件 | 本設計での対応 | 判定 |
|---|---|---|
| AC1 空盤面失敗を原因カテゴリまで特定、または証拠不足を明示して次Issueへ | 分類関数とcategory表。37bbの既存失敗は `evidence_incomplete`（旧schema）と明示。次の再発で新記録からcategoryが出る。再発しないまま本Issueを閉じる場合は、未特定と次の観測計画を後続Issueに引継ぐ | T1, T2 |
| AC2 再現可能な根因に対応する負例テスト・最小修正（必要な場合） | 各境界の負例（T4・T5・T11）。製品修正は根因が確定していないため「必要な場合」に該当しないと判断し、条件を手順7に定めた | T4, T5, T11 |
| AC3 失敗Frame/JS/HTTP/DOMの証拠に同一世代・時刻 | `correlation_id` と親processの単調時計による相対ms。不一致はfail-closed | T2, T3, T7, T8 |
| AC4 本番入力権限/score/Plannerを変更せず、45秒/Stop/Privacy/CI budgetを維持 | 診断は明示フラグ時だけ、判定・入力に戻さない。追加はcounter数個と1回の時刻取得で、CDPは既存1回のまま | T6, T7, T10 |
| AC5 p50/p95/失敗率を対照と比較、未実施項目はOpen Issueへ | 集計toolとcohort分離。Windowsでの比較実測は実装PRのCI runと、その後の再発時に行い、未実施分は後続Issueへ | T9 |

## 完了条件

- T1〜T10が通り、`finish_task.bat` が全段成功。
- 実装PRのWindows CI（push・PR両イベント）で、`--save-browser-diagnostics` の成功runでは `correlation` 節が出ないこと（失敗時だけ書く）と、既存の比較・campaignが成功することを確認する。集計toolで実装PRのcohortとmain対照のp50/p95を出し、PR本文へ貼る。
- 文書と `doc/versions.md` を更新。

## 代替案と不採用理由

- **URLのqueryに相関IDを付ける**: ページのbyte列・Resource Timingのname照合が変わり、元の失敗条件と比較できなくなるため不採用。
- **失敗後に `Network.enable` で過去の失敗を取得**: 有効化前のイベントは得られず、診断がrenderer状態を変える。既存方針どおり不採用。
- **startup期限の延長・reload・retry**: 失敗を隠し、根因の確定を妨げるため不採用。
- **常時の詳細ログ**: Privacyと記録量のため、既存の明示フラグ時だけにする。

## 範囲外・残件

- 根因確定後の製品修正（手順7の条件を満たした時点で後続Issue）。
- 再発が無いままの長期観測計画と、全10件の失敗runの未照合分（TLS timeoutで未確認）。本Issueのclose時に後続Issueへ引継ぐ。
- sample UIの起動競合（run 41958841719）は#794で対処済みで本Issueの対象外。
- Lease失効（#794）、Capture（#202）の個別課題。
