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
| `server.connections_awaiting_request_line` | int（gauge） | snapshot時点で受理済み・要求行未受信のまま開いている接続数 |
| `server.connections_without_request_line` | int（飽和） | 要求行が空のまま閉じた接続数 |
| `server.request_line_errors` | int（飽和） | 要求行の解析失敗数 |
| `server.requests_parsed` | int（飽和） | 要求行を解析できた要求の総数（allowlist外の `/favicon.ico` 等を含む） |
| `server.requests_in_flight` | int（gauge） | 解析済みで応答の記録が終わっていない要求数 |
| `server.resources.<name>` | 既存counter＋追加 | 固定12名を診断有効時に**全て0で初期化**する（未要求でもentryがある）。追加は `failed_request_phase`・`failed_headers_phase`・`failed_body_phase`（phase別の失敗数。既存の `last_failed_phase` は残すが分類には使わない）と `first_started_ms`・`first_headers_ms`・`first_completed_ms`・`first_failed_ms`（無ければnull） |
| `server.recording_incomplete` | bool | 既存のlatch |
| `server.snapshots_equal` | bool | CDPの前と後に取ったserver snapshotが同一か |
| `frame` | object | `frame_sha256`・`png_sha256`（既存header値の写し）、取得時刻 `captured_ms`（取得元が時刻を持たなければnull）、保全時刻 `preserved_ms`、保全Frameの `empty_board`（既存認識器の16空セル判定、閾値不変）と `board_accepted`（その時点の認識が盤面を受理したか） |
| `cdp` | object | `started_ms`・`finished_ms`・既存のrender/timing結果のsha256 |
| `target_matches_generation` | bool | CDPの対象URLがこの世代のserver URLと完全一致したか（URL自体は出さない） |
| `browser_version` | 文字列（64字まで） | browser実行ファイルのfile version。取得不能ならnull |
| `ci_context` | object | 環境変数の `GITHUB_EVENT_NAME`・`GITHUB_RUN_ID`・`GITHUB_RUN_ATTEMPT`・`GITHUB_SHA`（無ければnull。秘密ではない値だけ） |
| `classification` | object | 下記 |

全体の上限は既存の `MAX_DIAGNOSTIC_BYTES` と `browser` 節16 KiBを変えない。増えるfieldは固定数で、CDP envelopeの256 field上限内に収まることをテストで確認する。

### 全episodeのstartup要約（成功時も出す）

明示診断時は、成功・失敗を問わず各episodeのreportに `startup_summary` を出す: `correlation_id`、`server_started_ms=0`、`first_board_accepted_ms`（最初に盤面を受理した親process時刻の相対ms、受理しなければnull）、`server_connection_summary`（上表の接続counter 4種、時刻・URLなし）、`browser_version`、`ci_context`。集計toolはこれを読む（成功episodeのp50/p95と対照に使う）。

### `classify_startup_failure(record) -> dict`

戻り値は `{"category", "boundary", "evidence_complete", "missing": [<固定名>...], "all_matching": [<category>...], "control_required", "next_check"}`。categoryは固定集合で、境界の順に最初に該当したものを返す。後段で成立する他のcategoryも `all_matching` に残し、隠さない。

「欠落script」= 固定allowlistのscriptで `completed` が0のもの。server側の判定（1〜4）は `page` と欠落scriptだけを対象にし、他resource（例 `main_css`）の失敗は `other_resource_failures` に記録して分類を続ける。

| 順 | category | 条件（全て満たす） | boundary |
|---|---|---|---|
| 0 | `evidence_incomplete` | 下記の検証違反のいずれか | 判定不能 |
| 1 | `connection_without_request` | `requests_started`=0 かつ `failed_requests`=0 の欠落scriptがあり、`connections_without_request_line` + `connections_awaiting_request_line` > 0 | TCP受理→要求行 |
| 2 | `request_not_observed_by_server` | 欠落scriptの `requests_started`=0、要求行なし（閉/開とも）=0・`request_line_errors`=0・受理数=`requests_parsed`。OSのlisten queueに残った接続はserverから見えないため、「browser未送信」と「OS queue滞留」は区別しない | browser送信〜server受理（区別不可） |
| 3 | `server_failed_before_headers` | 対象resourceの `failed_request_phase`+`failed_headers_phase` > 0 | server→header |
| 4 | `server_failed_in_body` | 対象resourceの `failed_body_phase` > 0（header完了・completed=0でもphase別失敗が無ければ該当しない） | server→body |
| 5 | `served_response_invalid` | 欠落はないが、scriptの最終statusが200以外、または完了本文SHAがlockの期待値と不一致 | server配信内容 |
| 6 | `served_constructor_missing` | 全scriptがHTTP200・完了・SHA一致、かつ必要constructorのいずれかが未定義。JSの構文/実行エラーかbrowser受信失敗かは区別しない（Consoleを取らない） | server配信→constructor定義 |
| 7 | `constructors_defined_no_dom_tiles` | 全constructorが定義済み（CDPは `typeof` による定義判定で、GameManagerの生成は記録しない）、`tile_count`=0 | constructor定義→GameManager生成/DOM |
| 8 | `dom_present_frame_empty_candidate` | CDP時点で `tile_count`>0 かつ表示boolean true、それより前の保全Frameの `empty_board`=true | DOM→RAF/compositor/Capture（候補） |
| 9 | `frame_nonempty_not_recognized` | 保全Frameの `empty_board`=false かつ `board_accepted`=false | Capture→Recognizer |
| 10 | `unclassified` | 上記いずれも非該当 | 不明 |

- 分類の前に全fieldを検証する。違反は `evidence_incomplete` とし、`missing` に固定名を入れる: `schema`（不一致・旧schema）、`correlation_id`（節間の不一致）、`target_generation`（`target_matches_generation`≠true）、`recording_incomplete`、`snapshot_changed`（`snapshots_equal`≠true または `requests_in_flight`>0）、`connection_balance`（受理数 ≠ 要求行なし(閉)+要求行待ち(開)+解析失敗+`requests_parsed`）、`field_range`（counterが0〜2^31-2の整数でない。飽和値は判別不能）、`time_order`（時刻が0〜3,600,000の有限値でない、resourceの `started≤headers≤completed` 違反、Frame取得→保全→CDP開始→CDP終了の順の違反、`captured_ms` がnull）、`outcome_balance`（resourceの `completed`+phase別失敗の合計が `requests_started` を超える）、`frame_result`（`empty_board`/`board_accepted` がbooleanでない）。同一resourceの `completed`>0 と失敗>0の同時成立は再要求として正当であり矛盾にしない。
- 候補category: 1と8は `control_required=true`。browserの投機的preconnectは要求行なし接続を正常時にも作り得るため、category 1は、同じcohort（同一event・seed・`browser_version`）の成功episodeすべての `server_connection_summary` で要求行なし接続が0であることを集計で示すまで根因と呼ばない。category 8は、Frame取得時点のDOMを観測していないため本設計の範囲では対照で確定できない。確定にはFrame取得と同時刻のDOM観測が必要で、それは後続Issueで扱う（確定できないまま根因と呼ばない）。
- Resource Timingの `entry_count=0`・API無しはcategoryの根拠に使わず、`next_check` の補足にだけ入れる（entry無しを成功や原因と扱わない）。
- この関数は判定の記録だけを行い、入力・Planner・lease・Stop・失敗判定には戻さない。

## 手順

1. **server世代の記録**（`tools/reference_browser_diagnostics.py`・`tools/reference_game_2048_launcher.py`）
   - `ResourceResponseCounts` を診断有効時に固定12名で0初期化し、`t0`・時刻field・phase別失敗counterを追加する。`started`/`headers_completed`/`record`/`failed` で最初の時刻だけを書く。時刻取得や記録の例外は既存どおり `recording_incomplete` をlatchし、元の送信例外を上書きしない。
   - 接続counterとgaugeは明示診断時だけ有効。`ReferenceGameHTTPServer` を変えず、launcher内で診断用subclassを作り `process_request` で受理数と要求行待ちgaugeを増やす。handlerの `handle_one_request` で要求行を読んだ時点でgaugeを減らし、空なら `connections_without_request_line`、`parse_request` がfalseなら `request_line_errors`、trueなら `requests_parsed` と `requests_in_flight` を増やし、応答の記録後に `requests_in_flight` を減らす。全counterは1つのlockで更新し、snapshotも同じlockで取る。backlog 32・HTTP/1.0・daemon handler・close/port解放・finite listenは変えない。診断無効時のclass・挙動は現状と同一。
2. **Frame・CDPとの結合**（`tools/planning_failure_header.py`・`tools/reference_episode_failure_diagnostics.py`・`tools/run_2048_campaign.py`・`tools/compare_2048_windows_planning.py`）
   - 失敗headerは `captured_ms`・`preserved_ms` と、保全Frameに既存認識器をそのまま適用した `empty_board`・`board_accepted` を記録する（閾値・認識の挙動は変えない）。server snapshotをCDPの直前と直後に取り、CDPは前後で `started_ms`/`finished_ms` を記録する。順序は既存どおり「Frame保全→heartbeat→CDP（1回限り）」。
   - `correlation` 節を組み立てて `classify_startup_failure` の結果を加え、既存の `failure-diagnostics.json` に保存する。各episodeのreportへ `startup_summary` を出す。失敗の種類・終了コード・reportの成否は変えない。
3. **分類関数**（新規 `tools/reference_startup_correlation.py`）: 上表を固定順で評価する純粋関数。入力は辞書だけで、I/O・時計・乱数を使わない。
4. **集計tool**（新規 `tools/summarize_2048_startup_runs.py`）: `--cohort 名前=ディレクトリ` を複数受け取り、各ディレクトリ配下の既存reportの `startup_summary` と `failure-diagnostics.json` だけを読む（network無し、symlink・巨大ファイルは拒否）。cohortごとに、`first_board_accepted_ms` のp50/p95（成功episode）、空盤面失敗の件数/試行数、categoryの内訳、成功episodeの要求行なし接続の分布を出す。cohortの採用規則: 1 cohortの全episodeは同じ `ci_context`（event・run_id・run_attempt・sha）でなければ拒否する。失敗episodeを除外せず失敗率の分子に数え、p50/p95には入れず件数を併記する。`evidence_incomplete` の記録はcategory内訳で別に数え、そのcounterや時刻を他の集計に使わない。`run_attempt`>1のcohortには `retry_only=true` を付け、失敗率の改善の証拠と表示しない。`ci_context` が無い（手元実行）cohortは `attempt_unknown=true` とし比較を `reference_only` にする。比較する2 cohortの成功episodeが各10件未満、または `browser_version` が異なる場合も `comparison=reference_only`。
5. **既存Linux probe**（`tools/probe_reference_browser_resources.py`）: 10条件のreportに各条件の `classify_startup_failure` 結果を加える。開発専用・Playwright任意依存のまま、CI必須にしない。
6. **文書**: `doc/architecture/reference_browser_post_failure_diagnostics.md` に相関記録とカテゴリ表、`doc/architecture/reference_http_accept_queue.md` に接続counterを追記。`doc/versions.md`。
7. **修正の判断（今回は修正しない）**: 37c30統合後の再発は確認範囲で見つからず、確定した根因が無い。根因の無い修正（期限延長・retry・reload・Chrome flag・空盤面の許可）は失敗を隠すため行わない。Windowsで再発し、categoryが1〜9のいずれかに確定し（候補の1・8は対照条件を満たし）、そのcategoryを決定論的に再現する負例を作れた場合だけ、後続Issueで最小修正を行う。例: 1/2ならserverの受理経路、3〜5ならhandlerの送信経路・配信内容、6〜8はbrowser側の境界の追加切り分け。

### 失敗時の挙動（fail-closed）

- 空盤面の間は従来どおり `capture_playable_state` が盤面を受理せず、入力ゲートはSAFE_IDLEのまま。45秒の期限で失敗する。本変更は待ち時間・入力・再試行を増やさない。
- 診断の失敗（例外・上限超過・相関不一致）は `evidence_incomplete` または既存の `unavailable` になり、元の失敗を上書きしない。カテゴリを推測で埋めない。
- 診断は既存の明示フラグ時だけで、通常のuser起動経路・Source・Planner・学習に流さない。URL・token・本文・Console・盤面値・得点・storageは出さない（既存のPrivacy境界を維持）。

## テスト

| # | 試験（`tests/`） | 対応する受入条件 |
|---|---|---|
| T1 | `test_reference_startup_correlation.py`: 各category 0〜10の最小記録で期待category・boundaryになる。同時に成立し得る組（例: 欠落script AがRST後に要求行なし、欠落script Bがheader前失敗→1と3）で先の境界が勝ち、後段は `all_matching` に残る。`main_css` だけの失敗は `other_resource_failures` に入り分類を止めない。404・SHA不一致は5 | AC1 |
| T2 | 同: 37bb seed481の実記録（旧schema、本文値はlog由来の公開済みcounterのみ）を固定fixtureにし、`evidence_incomplete` と `missing=[requests_started,...]` になる。原因を命名しない | AC1, AC3 |
| T3 | 同: 相関ID不一致・`target_matches_generation=false`・CDP時刻がFrameより前・`recording_incomplete`・飽和counter・負/非finite時刻・`snapshots_equal=false`・`requests_in_flight>0`・接続の収支不一致・完了+失敗が開始数超過・`empty_board` 非boolean がそれぞれ `evidence_incomplete`（固定 `missing` 名つき）。完了1件と失敗1件が開始2件と並ぶ再要求は正当として分類される | AC3 |
| T4 | `test_reference_game_http_server.py`: loopbackで実socketを開いて要求行を送らず閉じる→`connections_accepted=1`・`connections_without_request_line=1`、解析系counterは不変。閉じずに保持中のsnapshotでは `connections_awaiting_request_line=1`。allowlist外のGETは `requests_parsed` だけを増やす。欠落script付きの記録で分類1・`control_required=true` | AC2 |
| T5 | 同: テスト用handler subclassで `html_actuator.js` のheader送信前に例外→ `failed phase=headers`、時刻は `first_failed_ms` のみ。分類は3。body途中の例外で4。同resourceにheader前失敗→body失敗の順で起きてもphase別counterが両方残り、分類は先の境界3テスト用subclassは `tests/` 内だけに置き、製品に故障注入flagを足さない | AC2 |
| T6 | 同: 診断有効時は固定12名が0で初期化される（未要求scriptもentryあり）。診断無効時は接続counter・時刻fieldが存在せず、server class・backlog 32・応答byte列が現状と同一 | AC4 |
| T7 | 同: 時刻・counter記録の例外注入で元の送信例外が保たれ `recording_incomplete=true` | AC3, AC4 |
| T8 | `test_reference_episode_failure_diagnostics.py`: Frame保全前はCDPを呼ばず、`correlation` 節の時刻順がFrame→CDPで、server snapshotがCDP前後で取られ、`empty_board`・`board_accepted` が記録され、byte上限超過時は既存の縮退形になる。成功episodeにも `startup_summary`（`first_board_accepted_ms`・接続counter・`browser_version`・`ci_context`）が出る | AC3 |
| T9 | `test_summarize_2048_startup_runs.py`: 疑似reportで p50/p95・失敗率・category内訳がcohortごとに出る。cohortを合算しない。失敗episodeは失敗率に入りp50/p95に入らない。`evidence_incomplete` は別計上。`run_attempt`>1は `retry_only=true`、`ci_context` 無しは `attempt_unknown`、成功10件未満や `browser_version` 違いは `reference_only`。異なる `ci_context` が混ざったcohortは拒否 | AC5 |
| T10 | 既存の入力・Stop・lease・Planner・score・45秒期限の関連テスト（`test_2048_campaign*`、`test_compare_2048_windows_planning*`）が無変更で通る。`finish_task.bat` 全体 | AC4 |
| T11（任意・手動） | Linuxの `probe_reference_browser_resources` 10条件で abort/404/空bodyのcategoryを記録（Playwright/Chromiumが要るためCI非必須、受入判定の根拠に使わない。実施時は条件ごとの期待/実測を `doc/validation/` に保存、未実施なら理由をPRに書く） | 参考 |

テストは決定的・外部network不要（loopbackのみ）。時刻は注入可能な時計で固定する。

## 受入条件との対応

| Issueの完了条件 | 本設計での対応 | 判定 |
|---|---|---|
| AC1 空盤面失敗を原因カテゴリまで特定、または証拠不足を明示して次Issueへ | 分類関数とcategory表。37bbの既存失敗は `evidence_incomplete`（旧schema）と明示。次の再発で新記録からcategoryが出る。再発しないまま本Issueを閉じる場合は、未特定と次の観測計画を後続Issueに引継ぐ | T1, T2 |
| AC2 再現可能な根因に対応する負例テスト・最小修正（必要な場合） | 各境界の診断負例（T4・T5）は今回行う。製品修正は「不要」と判断したのではなく、根因が未確定で修正の要否を判定できない状態。Issueを閉じる時点でも未確定なら、AC2は後続Issueへ引継ぐ（下記「Issueを閉じる条件」）。根因確定時の修正条件は手順7 | T4, T5 |
| AC3 失敗Frame/JS/HTTP/DOMの証拠に同一世代・時刻 | `correlation_id` と親processの単調時計による相対ms。不一致はfail-closed | T2, T3, T7, T8 |
| AC4 本番入力権限/score/Plannerを変更せず、45秒/Stop/Privacy/CI budgetを維持 | 診断は明示フラグ時だけ、判定・入力に戻さない。追加はcounter数個と1回の時刻取得で、CDPは既存1回のまま | T6, T7, T10 |
| AC5 p50/p95/失敗率を対照と比較、未実施項目はOpen Issueへ | 集計toolとcohort分離。Windowsでの比較実測は実装PRのCI runと、その後の再発時に行い、未実施分は後続Issueへ | T9 |

## 完了条件

- T1〜T10が通り、`finish_task.bat` が全段成功。
- 実装PRのWindows CI（push・PR両イベント）で、`--save-browser-diagnostics` の成功runでは `correlation` 節が出ないこと（失敗時だけ書く）と、既存の比較・campaignが成功することを確認する。集計toolで実装PRのcohortとmain対照のp50/p95を出し、PR本文へ貼る。
- 文書と `doc/versions.md` を更新。

## Issueを閉じる条件（実装PRの完了とは別）

実装PRの完了（上の「完了条件」）は診断機構の完成であり、Issue #791の完了ではない。Issueは次のどちらかで閉じる。

1. Windowsで空盤面が再発し、新記録のcategoryが1〜9に決まり（候補categoryは対照条件を満たし）、AC2の負例と最小修正が後続PRで入った。根因に対して製品修正が不要と判断した場合（例: runner環境側の要因）は、その根拠と負例をcloseコメントに書く。
2. 再発しないまま閉じる場合: 後続Issueを作り、そのIssue番号・未確定のAC（AC1の原因カテゴリ、AC2、AC5のWindows実測）・次の観測条件（`--save-browser-diagnostics` のcohort、対照の採用規則）を本Issueのcloseコメントに書く。成功した再実行だけを根拠に閉じない。

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
