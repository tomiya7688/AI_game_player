Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: #459

# Issue #794 Observation Lease失効の時系列因果と回帰防止

## 現状調査

### 自然失敗の時系列（CI `Windows sample loop and 2048 campaign E2E`）

証拠: run 41938316919 attempt 1（PR #730）と run 41941522328 attempt 1（PR #732）の`--save-heartbeat-diagnostics`出力（heartbeat ring 8件）、artifact `kadoka-windows-e2e-trace`（run 41938316919の`2048-campaign-20261105T000534Z`）。

`FailSafeConfig.lease_timeout_seconds=0.75`、heartbeat間隔`min(0.75/3, 0.25)=0.25s`。heartbeat 1回は「Runtime lock → 永続lease読取（Stop確認） → `clock()`で更新 → 永続lease書込（fsync+replace）」。外部watchdogは別processで50msごとに**ディスク上の**`lease_expires_at`と`time.time()`を比べ、超過で`recovery_required/lease_expired`を書いて終了する。

run 41938316919（単位ms、monoは同一process内の差）:

| seq | lock待ち | 読取 | 書込 | 前回終了→今回開始 | 更新時刻差(前回比) | 結果 |
|---|---|---|---|---|---|---|
| 521 | 0.0 | 33.4 | 40.6 | 250.7 | 328.4 | renewed |
| 522 | 8.0 | 192.3 | 312.2 | 250.2 | 491.0 | renewed |
| 523 | 0.0 | 180.7 | 1.6 | 268.7 | **761.5** | renewed（自期限333.8843を11.5ms過ぎてから更新） |
| 524 | 0.0 | 6.9 | - | 265.7 | - | persisted_stop |

run 41941522328: seq 369 読取148.4/書込260.9、seq 370 読取221.0/書込25.5、更新時刻差**749.7ms**（期限まで0.33ms）で、更新後の書込完了まで25.5ms ディスク上は失効状態 → seq 371 で`persisted_stop`。

因果:
1. 失敗直前のstepは`frame_analysis`733ms/`board_adaptation`227msの純Python計算（main thread、GIL保持）。この間main threadは`record_observation`/`check_command`を呼ばず、leaseはheartbeatだけで延長される。
2. heartbeat threadのfile I/Oは約30回のGIL解放・再取得を含む。CPU-boundなthreadが居ると再取得のたびに切替間隔（5ms）程度待つ（GIL convoy）。ローカル再現（`E:/projects/.review-work/AI_GAME_Player/issue-794-observation-lease/probe_gil.py`）: 待機main threadでは読取p50 9.9ms/書込p50 9.4ms、CPU-bound main threadでは読取p50 127ms（最大252）/書込p50 220ms（最大281）。CIの192/312msと同じ桁。
3. heartbeatは「完了後に0.25s待つ」固定遅延のため、更新間隔＝書込(n)+0.25s+lock+読取(n+1)。seq 522書込312ms＋待ち268.7ms＋seq 523読取180.7ms=761.5ms > 750msで、seq 523は**既に失効したleaseを更新**した。watchdogはその間（run1: 11.5ms＋書込1.6ms、run2: 書込25.5ms）にディスク上の失効を観測しStopを書いた。
4. 次のheartbeat/`check_command`が永続Stopを採用し、`runtime_not_active (recovery_required, lease_expired)`で入力を拒否した（Safetyは正しく働いた）。`observation_timing`のstage値は失効の直接原因ではなく、同時に走ったCPU負荷の指標。
5. ローカルで現行コードのheartbeat loopをCPU-bound main thread下で30秒（`probe_margin.py`）: 42更新中37回で「前回のディスク上期限 − 今回の書込完了」が負（最小−373ms、p50−183ms）。watchdog無効で計測したため実行は継続したが、watchdog有効なら全て停止条件。

付随して見つかった安全上の欠陥（同じ時間窓で起き得る）:
- **lost update**: heartbeat/`record_observation`/`check_command`/`complete_command`は永続leaseの読取（Stop確認）と書込を別々のcross-process lockで行う。watchdogがその間にStopを書くと、Runtimeが`active`で上書きし、watchdogは既に終了しているため外部監視が無いまま入力が続き得る。
- **失効後の更新**: heartbeat等は自分のleaseが既に失効していても延長する（上記3）。`check_command`とwatchdogは失効を停止条件にしており、意味が一致しない。
- closeは`runtime closed`でjournal/leaseを上書きし、失効理由がディスク上に残らない（ringはclose前に保存済み）。

### 既存の関連実装

- `src/ai_game_player/fail_safe_runtime.py`: `AtomicJsonStore`、`FailSafeRuntime`（heartbeat、`_honor_persisted_stop_locked`、`_write_lease_or_fail`）、`run_external_watchdog`。
- `src/ai_game_player/heartbeat_diagnostic.py`: 明示ON時だけのring（#459由来）。
- `tools/run_2048_campaign.py`、`tools/reference_episode_failure_diagnostics.py`: 失敗時にringを出力。
- #459の分割ブランチ（observation-timing / observation-refresh / campaign-reliability / recognition-evidence）は全てmain取込済み。今回はそれらのring・`observation_timing`を証拠源として再利用する。

## 成果物

- `src/ai_game_player/fail_safe_runtime.py`
  - `AtomicJsonStore.replace_unless(value, reject)`: 1回のcross-process lock内で読取→判定→書込。
  - `FailSafeRuntime`: heartbeat・`record_observation`・`check_command`・`complete_command`のlease書込を`replace_unless`の条件付き書込にする（lost update解消。初期化・arm・遷移・watchdogの書込は無条件書込のまま）。heartbeatの別読取を廃止。lease延長は`_extend_lease_locked(now)`に集約し、失効済みleaseは延長せず`recovery_required/lease_expired`へ遷移。heartbeat loopを開始時刻基準の期限スケジュールにする。closeのjournal detailsへ直前の状態・理由を残す。
  - 書込・読取経路の冗長なsyscall（毎回のmkdir、Windowsで必ず失敗するdirectory fsync、replace成功後のunlink、読取前の`exists()`）を除く。fsync・atomic replace・lockの意味は不変。
- `src/ai_game_player/heartbeat_diagnostic.py`: `renewal_mono_ns`、結果`lease_expired`、集計`summary`（更新数、期限余裕ms min/p05/p50、1回のheartbeat処理時間ms（entry→完了）p50/p95/max、負の余裕件数）。schema_version 2。
- `tools/run_2048_campaign.py`: heartbeat診断ON時、episode close前に`heartbeat_lease_budget`の1行JSONを出力（成功runでも余裕を記録）。
- テスト: `tests/test_fail_safe_lease_renewal.py`（新規）、`tests/fixtures/issue794_lease_expiry_rings.json`（自然失敗2件のring）、`tests/test_fail_safe_heartbeat_diagnostics.py`、`tests/test_fail_safe_runtime.py`の更新、campaign出力のテスト。
- 文書: `doc/FailSafeRuntime説明書.md`、`doc/versions.md`、本設計書。

## 入出力

- `AtomicJsonStore.replace_unless(value: dict[str, object], reject: Callable[[dict[str, object], bool], bool]) -> dict[str, object] | None`
  - lock内で現在値（不正・不在は`{}`）と存在flagを`reject`へ渡す。`True`なら書かずに現在値を返す。`False`なら`value`をatomic書込して`None`。
  - lock timeout・OSError・reject内例外は呼出元へ送出（呼出元は既存どおり`recovery_required`へ）。
- 拒否条件（Runtime）: 存在するが読めないlease、または同一epochで`state`が空/`active`以外。別epochの値は従来どおり上書き可。epochを変えるのは初回leaseと明示rearm（`_persist_or_fail`の無条件書込）だけで、延長系の書込は常に現在epochの値を書く。従来の`_honor_persisted_stop_locked`も別epochの停止を無視しており、その意味を変えない（別epochの停止で現在epochを止める変更はStopの意味変更になるため今回はしない）。
- 失効境界: Runtime（`check_command`・`_extend_lease_locked`）もwatchdogも`now > lease_expires_at`で失効（`now == expires`は未失効）。この演算子を揃えたまま変更しない。
- directory不在: `replace_unless`の読取は`{}`・不在として判定し、書込の一時file作成が`FileNotFoundError`のときだけ親directoryを作って1回再試行する。lock fileも同様。
- 新しいreason codeは追加しない（`lease_expired`/`lease_unreadable`/既存storage系を使用）。`check_command`は条件付き書込が永続Stopで拒否されたとき`runtime_not_active`、書込例外のとき従来どおり`storage_failure`を返す。
- heartbeat診断 schema_version 2: 行に`renewal_mono_ns`、結果語彙に`lease_expired`、snapshotに`summary`。上限32KiB・ring 8件は不変。`summary`の期限余裕は「heartbeat更新だけを見た下限」（main threadの更新があれば実際の余裕はこれ以上）。
- `heartbeat_lease_budget`行: `{"event":"heartbeat_lease_budget","schema_version":1,"diagnostic_only":true,"lease_timeout_ms":750.0,"summary":{...}}`。診断OFF・取得失敗時は出さない。失敗判定には使わない。
- 不変: `lease_timeout_seconds=0.75`、`watchdog_poll_seconds=0.05`、`observation_timeout_seconds`、`max_command_age_seconds`、watchdogの判定・書込、Stop/EMERGENCY_STOPの意味、明示rearmの要件、fsync。

## 手順

1. 期限スケジュール: heartbeat threadは各周回の開始をmonotonic時計で記録し、次周回を「開始時刻+間隔」に置く。処理が間隔を超えたら待たずに次を始める。authority時計（`clock`）は判定にだけ使い、スケジュールに使わない。これで更新間隔は`max(間隔, 1周回の処理時間)`になり、書込(n)と待ちと読取(n+1)の直列加算が無くなる。
2. 失効済みleaseを延長しない: `_extend_lease_locked(now)`は`now > _lease_expires_at`なら`_transition(RECOVERY_REQUIRED, "lease_expired")`して`False`。heartbeat・`record_observation`・`complete_command`が使う（`check_command`は既存の失効判定の後に延長）。初回lease・明示rearmは従来の`_renew_lease_locked`（失効判定なし）。
3. 条件付き書込: `_write_lease_or_fail`は`replace_unless`で、拒否時は読んだ値で既存の永続Stop採用処理（`_adopt_persisted_locked`、heartbeat停止・入力解放）を行い`False`。heartbeatは別の永続読取をせず、この1回のlock内で読取と書込を行う（I/Oとlock取得が半分）。他の入口の事前Stop確認は維持。
   - heartbeatは`extend_at_commit=True`: lock内の読取・停止判定の**後**、書込値を作る直前にauthority時計で失効確認と延長を行う（`replace_unless`へ書込値の生成関数を渡す）。旧実装と同じく「更新→保存完了」は書込時間だけになる。失効時は生成関数が書込を中止し、cross-process lockを解放した後で`_transition(lease_expired)`する（lock保持中の再取得を避ける）。
   - 実装中の発見: 更新をlock取得前に取ると「更新→保存完了」に読取が加わり、run 41938316919のring再生で余裕が−12msになった。このため更新を書込直前へ移した。
4. 遷移（`_persist_or_fail`）とwatchdogは無条件書込のまま（停止方向の書込で、上書きされても停止側）。
5. syscall削減は意味不変に限定: 書込先directoryの不在時だけmkdirして再試行、`os.name == "nt"`ではdirectory fsyncを試みない、replace成功時は一時fileのunlinkを省く（失敗時は従来どおり削除）、読取は`FileNotFoundError`で不在を判定。
6. close: 遷移前の`state/reason`をjournal detailsの`previous_state/previous_reason`へ残す。leaseの`state=safe_idle`は従来どおり。
7. 失敗時: lock timeout・書込例外は従来どおり`recovery_required`（fail-closed）。heartbeatが失効を検出した場合、watchdogより先にRuntime自身が停止する。heartbeatの永続読取はこれまで例外をそのまま送出してthreadを終了させ、lease失効まで停止が遅れていたが、条件付き書込内の失敗として即座に`recovery_required/heartbeat_storage_failure`になる（停止方向のみの変化）。
8. Runtimeの失効遷移とwatchdogのStop書込が競合しても、どちらの順でも最終状態は`recovery_required`で理由は`lease_expired`（両者同じ語彙）。journalは後勝ちだが状態・理由は同じ。以後の`check_command`は`runtime_not_active`で拒否する。
9. 期限スケジュールが保証するのは「1周回の処理時間が間隔を超えたときに待ちを加えない」ことだけ。1周回（または連続2周回の処理）がTTLを超えれば従来どおり失効して停止する（fail-closed、試験で固定）。2件の自然失敗ringを同じ相別所要時間で再生した場合の余裕は計算で示し、それ以上の一般的な保証はCIの`heartbeat_lease_budget`実測で確認する。

## テスト

| 受入条件 | 試験 |
|---|---|
| 自然失敗の因果を固定 | fixture（2 run のring）から固定遅延スケジュールの更新間隔761.5ms/749.7ms＋書込で期限超過になることを計算で確認し、同じ相別所要時間を期限スケジュールへ再生すると期限内になることを確認 |
| 期限スケジュール | 偽monotonic時計と台本の所要時間で`_heartbeat_loop`の待ち時間が`max(0, 間隔−経過)`になる |
| TTL超の周回はfail-closed | 1周回の処理がTTLを超える台本で、次のheartbeatが`False`・`lease_expired`になり延長しない |
| 失効境界 | `now == expires`では延長し、`now > expires`では延長しない |
| 失効leaseを延長しない | FakeClockで0.75s超過後のheartbeat/`record_observation`/`complete_command`が`False`、状態`recovery_required/lease_expired`、ディスクの期限0、その後の`check_command`（古い観測のcommand）が`runtime_not_active`で入力を許可しない |
| lost update防止 | 事前Stop確認と書込の間に同一epochのStopを書く注入で、`record_observation`/`check_command`/`complete_command`がStopを上書きせず採用する（`check_command`は`runtime_not_active`）。heartbeatは別読取を持たず`replace_unless`1回だけで停止判定と書込を行うことを確認 |
| 別epoch・不在leaseは従来どおり | 別epochのStopは上書きされ、不在fileは作成される |
| watchdog競合 | 失効leaseで実`run_external_watchdog`がStopを書いた後、heartbeatが`False`で再開しない |
| 書込障害・lock timeoutはfail-closed | 既存`FailingStore`系テストを`replace_unless`経路でも通す |
| 診断 | 有効時のclock/I/O回数が無効時と同じ、`renewal_mono_ns`の順序、`summary`の値（偽monotonic）、32KiB上限 |
| close前の理由保存 | `lease_expired`後のcloseでjournal detailsに`previous_reason=lease_expired` |
| campaign出力 | 診断ON時だけ`heartbeat_lease_budget`を1行出し、取得失敗でもcloseを妨げない |
| TTL/Stop不変 | `FailSafeConfig()`既定値と`run_external_watchdog`の既存テストが無変更で通る |

### 実測（実装後）

- ring再生（同じ相別所要時間、旧I/O量のまま）: 最悪余裕 固定遅延 −53.0ms/−7.4ms → 期限スケジュール +21.0ms/+128.4ms（run 41938316919/41941522328）。旧I/O量のままでは余裕が小さいため、lock統合・syscall削減によるI/O短縮と合わせて効く。
- ローカルprobe（CPU-bound main thread 30秒、`probe_margin.py`）: 修正前 42更新中37回が負（最小−373ms、p50−183ms、周期p50 400ms） → 修正後 100更新で負0（最小+69ms、p05+181ms、p50+364ms、周期p50 204ms）。結果は`E:/projects/.review-work/AI_GAME_Player/issue-794-observation-lease/probe_{before,after}.txt`。

## 完了条件

- 上表の試験と`finish_task.bat`が通る。
- ローカル再現probe（CPU-bound main thread）で修正前後の期限余裕を記録する。
- PRのE2E jobが同一HEADで連続2回成功し、`heartbeat_lease_budget`の最小余裕を記録する。push/PR/main検証を区別して報告し、未達はIssueへ引継ぐ。

## 設計レビュー（packet-design.txt、SHA256 81D14A93…B2BB1）

採用: 失効境界の明示と試験（Luna）、TTL超周回のfail-closed試験と保証範囲の限定（Luna/Antigravity）、失効遷移とwatchdog Stopの競合結果の定義（Luna）、Stop後に古い観測のcommandが許可されない試験（Luna）、directory不在時の流れの明記（Antigravity）。
不採用: 別epochの停止でも延長を拒否する案（Luna/Antigravity）— 延長系は現在epochしか書かず、別epochの停止を無視する既存の意味を変えるとStop semanticsの変更になるため。書込前の残TTL閾値で先行停止する案（Antigravity）— 失効判定はwatchdogとRuntimeの同じ`>`境界で既に行い、閾値はTTL短縮と同等の意味変更になるため。Windowsのreplace共有違反試験（Antigravity）— 読取側も同じcross-process lockを取るため置換中の共有読取は起きず、既存の8回再試行も不変。

## 実装レビュー（packet-code-01/02）

採用: summaryの集計範囲を明確化（Luna）— `renewals`/`margin_samples`/`negative_margin_count`/`margin_ms_min`は全期間累積、p05/p50/周期統計は直近`window_samples`（2048）件。
確認のみ: 空・不正な既存leaseの拒否時の遷移（Luna）— 拒否値`{}`は`_adopt_persisted_locked`で`recovery_required/lease_unreadable`へ移る（`test_unreadable_lease_is_adopted_as_recovery`）。

## 既存テストへの影響

高負荷のfinish_task（全件883秒）で`test_comparison_preflight`のStop試験が`initial safety lease is not active`で失敗した。実時計と実heartbeat threadを使い、external watchdog無しのため、旧実装ではthreadが0.75秒以上止まっても失効後に暗黙延長されていた。新しい規則で正しく`lease_expired`になる。このfileはlease期限を検証しないため、試験専用に`lease_timeout_seconds=30`とした（製品既定値は不変）。同じ実行で落ちた`test_reference_browser_diagnostics`（1.165s対上限1.15s）と`test_runtime_boundary_benchmark`（IPC timeout）はfail-safeを使わない負荷起因の失敗で、単独実行・再実行（全件560秒、OK）では成功した。

## CodeRabbit指摘への対応（PR #813、9件を現行コードと照合し全て有効と判断）

- 「全て」の過大表現（文書2か所）: 条件付き書込はheartbeat・record_observation・check_command・complete_commandのみ、初期化・arm・遷移・watchdogは無条件と明記。
- watchdogの`lease_store.read()`が`TimeoutError`で終了する: 捕捉して短い待機後に監視を継続（停止方向の変更なし。期限判定は次回読取）。
- 診断observerの例外が結果を変え得る: callback全体を例外隔離。
- check_commandが読取前の時刻で失効判定し、その後の延長で復活し得る: 時刻を永続読取の後で取得し、延長も失効確認つきにした。受理直前に期限を過ぎていれば`lease_expired`で拒否。
- 2回目のcloseが`previous_reason`を上書き: 既にclose済みなら再記録しない。
- 同一epochで`state`が空・欠落したleaseを書込可能としていた: 設計書の「空/active以外」に合わせ停止（Recovery）として扱う。
- `cycle_ms_*`は開始間隔ではなく処理時間: `duration_ms_*`に改名し文書を修正。
- 計測起点が時刻採取より後: `renewing`イベントで採取前にmonotonicを取り、余裕を保守側にした。
- 診断行の出力がcleanup前でstdout停止時に解放を妨げる: 行はcleanup前に作り、出力はlauncher.close()の後にした。

## sample E2Eの起動直後失敗（PRで観測、leaseとは別原因）

main取込後、push側のlanguage-ci（run 41958841719）で`Run sample-control-confirmed live input loop`が起動0.3秒・0 stepsの`recognition_or_candidate_failure`（`許可された操作候補がありません`）になった。同じHEADのpull_request側E2E（run 41958852024）は成功し、9375f83の2回も成功していたため非決定的。9375f83→HEADの製品差分は`tools/run_noisy_provider_benchmark.py`だけ。

原因: `find_window`はwindowの存在とstate fileだけを待ち、最初の描画を待たない。`WindowsSampleObservationSource.read`の最初のcaptureが未描画だと画像候補が0件になり、`match_sample_control_to_image`が`None`を返して即失敗する。ローカルで`origin/main`単体（本PRの変更なし）が同一の症状（0 steps、0.37〜0.8秒、同じエラー）を再現し、captureしたframeが全byte 0（distinct=1）であることを確認した。fail-safe・leaseは関与しない（候補の許可判定`ActionEvaluator`はfail-safeを参照しない）。

修正: `wait_for_sample_ready`を追加し、`DecisionPipeline`生成前に読取専用で確認済み候補が得られるまで最大20秒・0.1秒周期で待つ。入力は送らず、期限内に得られなければ`RuntimeError`で失敗する（fail-closed）。判定・Stop・TTLは変更しない。読取の例外は握り潰さない。試験は偽時計で、未描画の再試行、初回成功、予算超過の失敗、読取エラーの伝播を固定した。

制約: 修正後のローカル再実行ではcaptureが継続して全0のため20秒で失敗し、E2E全体のローカル成功は確認できなかった（このPCの描画状態による）。実E2Eの確認はCIで行う。

## 関連Issue #812 との関係

run 41945317245 attempt 1（seed 481）のplanner失敗は`planning_unavailable`/`compute_budget`で、heartbeat ringは全て`renewed`（`lease_expired`無し）。`invalid_frame`は失敗診断headerに保存frameが無いことを示す。GIL競合の兆候（読取150-206ms、書込237ms、更新間隔最大708ms）は共通だが機構は別のため、本PRでは#812を修正しない。

## 代替案と不採用理由

- TTL延長・heartbeat間隔の単純短縮・watchdog閾値の緩和: Issueで禁止。短縮はI/O回数とlock競合を増やし、convoy下で改善しない。
- 失効後の無条件延長や自動rearm: 停止すべきleaseを延命するため不採用（むしろ現状のheartbeatの失効後延長を禁止する）。
- `sys.setswitchinterval`の縮小: process全体の挙動を変えるためRuntimeから行わない（範囲外）。
- lease書込のfsync省略: crash一貫性の意味変更でありowner判断が要るため今回はしない。
- heartbeatのnative thread/別process化: GILに依存しない根本策だが設計変更が大きい（範囲外）。

## 範囲外・残件

- 認識処理（frame_analysis/board_adaptation）のGIL保持時間の短縮、native heartbeat、lease fsyncの要否は後続Issueで扱う（PR時にIssue番号を記載）。
- `observation_stale`系の自然失敗はこのPRの証拠に無く、診断出力で引き続き監視する。
