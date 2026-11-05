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
  - `FailSafeRuntime`: 全てのlease書込を`replace_unless`の条件付き書込にする（lost update解消）。heartbeatの別読取を廃止。lease延長は`_extend_lease_locked(now)`に集約し、失効済みleaseは延長せず`recovery_required/lease_expired`へ遷移。heartbeat loopを開始時刻基準の期限スケジュールにする。closeのjournal detailsへ直前の状態・理由を残す。
  - 書込・読取経路の冗長なsyscall（毎回のmkdir、Windowsで必ず失敗するdirectory fsync、replace成功後のunlink、読取前の`exists()`）を除く。fsync・atomic replace・lockの意味は不変。
- `src/ai_game_player/heartbeat_diagnostic.py`: `renewal_mono_ns`、結果`lease_expired`、集計`summary`（更新数、期限余裕ms min/p05/p50、周期ms p50/p95/max、負の余裕件数）。schema_version 2。
- `tools/run_2048_campaign.py`: heartbeat診断ON時、episode close前に`heartbeat_lease_budget`の1行JSONを出力（成功runでも余裕を記録）。
- テスト: `tests/test_fail_safe_lease_renewal.py`（新規）、`tests/fixtures/issue794_lease_expiry_rings.json`（自然失敗2件のring）、`tests/test_fail_safe_heartbeat_diagnostics.py`、`tests/test_fail_safe_runtime.py`の更新、campaign出力のテスト。
- 文書: `doc/FailSafeRuntime説明書.md`、`doc/versions.md`、本設計書。

## 入出力

- `AtomicJsonStore.replace_unless(value: dict[str, object], reject: Callable[[dict[str, object], bool], bool]) -> dict[str, object] | None`
  - lock内で現在値（不正・不在は`{}`）と存在flagを`reject`へ渡す。`True`なら書かずに現在値を返す。`False`なら`value`をatomic書込して`None`。
  - lock timeout・OSError・reject内例外は呼出元へ送出（呼出元は既存どおり`recovery_required`へ）。
- 拒否条件（Runtime）: 存在するが読めないlease、または同一epochで`state`が空/`active`以外。別epochの値は従来どおり上書き可（明示rearmの契約を維持）。
- 新しいreason codeは追加しない（`lease_expired`/`lease_unreadable`/既存storage系を使用）。`check_command`は条件付き書込が永続Stopで拒否されたとき`runtime_not_active`、書込例外のとき従来どおり`storage_failure`を返す。
- heartbeat診断 schema_version 2: 行に`renewal_mono_ns`、結果語彙に`lease_expired`、snapshotに`summary`。上限32KiB・ring 8件は不変。`summary`の期限余裕は「heartbeat更新だけを見た下限」（main threadの更新があれば実際の余裕はこれ以上）。
- `heartbeat_lease_budget`行: `{"event":"heartbeat_lease_budget","schema_version":1,"diagnostic_only":true,"lease_timeout_ms":750.0,"summary":{...}}`。診断OFF・取得失敗時は出さない。失敗判定には使わない。
- 不変: `lease_timeout_seconds=0.75`、`watchdog_poll_seconds=0.05`、`observation_timeout_seconds`、`max_command_age_seconds`、watchdogの判定・書込、Stop/EMERGENCY_STOPの意味、明示rearmの要件、fsync。

## 手順

1. 期限スケジュール: heartbeat threadは各周回の開始をmonotonic時計で記録し、次周回を「開始時刻+間隔」に置く。処理が間隔を超えたら待たずに次を始める。authority時計（`clock`）は判定にだけ使い、スケジュールに使わない。これで更新間隔は`max(間隔, 1周回の処理時間)`になり、書込(n)と待ちと読取(n+1)の直列加算が無くなる。
2. 失効済みleaseを延長しない: `_extend_lease_locked(now)`は`now > _lease_expires_at`なら`_transition(RECOVERY_REQUIRED, "lease_expired")`して`False`。heartbeat・`record_observation`・`complete_command`が使う（`check_command`は既存の失効判定の後に延長）。初回lease・明示rearmは従来の`_renew_lease_locked`（失効判定なし）。
3. 条件付き書込: `_write_lease_or_fail`は`replace_unless`で、拒否時は読んだ値で既存の永続Stop採用処理（`_adopt_persisted_locked`、heartbeat停止・入力解放）を行い`False`。heartbeatは別の永続読取をせず、この1回のlock内で読取と書込を行う（I/Oとlock取得が半分）。他の入口の事前Stop確認は維持。
4. 遷移（`_persist_or_fail`）とwatchdogは無条件書込のまま（停止方向の書込で、上書きされても停止側）。
5. syscall削減は意味不変に限定: 書込先directoryの不在時だけmkdirして再試行、`os.name == "nt"`ではdirectory fsyncを試みない、replace成功時は一時fileのunlinkを省く（失敗時は従来どおり削除）、読取は`FileNotFoundError`で不在を判定。
6. close: 遷移前の`state/reason`をjournal detailsの`previous_state/previous_reason`へ残す。leaseの`state=safe_idle`は従来どおり。
7. 失敗時: lock timeout・書込例外は従来どおり`recovery_required`（fail-closed）。heartbeatが失効を検出した場合、watchdogより先にRuntime自身が停止する。

## テスト

| 受入条件 | 試験 |
|---|---|
| 自然失敗の因果を固定 | fixture（2 run のring）から固定遅延スケジュールの更新間隔761.5ms/749.7ms＋書込で期限超過になることを計算で確認し、同じ相別所要時間を期限スケジュールへ再生すると期限内になることを確認 |
| 期限スケジュール | 偽monotonic時計と台本の所要時間で`_heartbeat_loop`の待ち時間が`max(0, 間隔−経過)`になる |
| 失効leaseを延長しない | FakeClockで0.75s超過後のheartbeat/`record_observation`/`complete_command`が`False`、状態`recovery_required/lease_expired`、ディスクの期限0 |
| lost update防止 | 延長と書込の間に同一epochのStopを書く注入で、heartbeat/`record_observation`/`check_command`/`complete_command`がStopを上書きせず採用する（`check_command`は`runtime_not_active`） |
| 別epoch・不在leaseは従来どおり | 別epochのStopは上書きされ、不在fileは作成される |
| watchdog競合 | 失効leaseで実`run_external_watchdog`がStopを書いた後、heartbeatが`False`で再開しない |
| 書込障害・lock timeoutはfail-closed | 既存`FailingStore`系テストを`replace_unless`経路でも通す |
| 診断 | 有効時のclock/I/O回数が無効時と同じ、`renewal_mono_ns`の順序、`summary`の値（偽monotonic）、32KiB上限 |
| close前の理由保存 | `lease_expired`後のcloseでjournal detailsに`previous_reason=lease_expired` |
| campaign出力 | 診断ON時だけ`heartbeat_lease_budget`を1行出し、取得失敗でもcloseを妨げない |
| TTL/Stop不変 | `FailSafeConfig()`既定値と`run_external_watchdog`の既存テストが無変更で通る |

## 完了条件

- 上表の試験と`finish_task.bat`が通る。
- ローカル再現probe（CPU-bound main thread）で修正前後の期限余裕を記録する。
- PRのE2E jobが同一HEADで連続2回成功し、`heartbeat_lease_budget`の最小余裕を記録する。push/PR/main検証を区別して報告し、未達はIssueへ引継ぐ。

## 代替案と不採用理由

- TTL延長・heartbeat間隔の単純短縮・watchdog閾値の緩和: Issueで禁止。短縮はI/O回数とlock競合を増やし、convoy下で改善しない。
- 失効後の無条件延長や自動rearm: 停止すべきleaseを延命するため不採用（むしろ現状のheartbeatの失効後延長を禁止する）。
- `sys.setswitchinterval`の縮小: process全体の挙動を変えるためRuntimeから行わない（範囲外）。
- lease書込のfsync省略: crash一貫性の意味変更でありowner判断が要るため今回はしない。
- heartbeatのnative thread/別process化: GILに依存しない根本策だが設計変更が大きい（範囲外）。

## 範囲外・残件

- 認識処理（frame_analysis/board_adaptation）のGIL保持時間の短縮、native heartbeat、lease fsyncの要否は後続Issueで扱う（PR時にIssue番号を記載）。
- `observation_stale`系の自然失敗はこのPRの証拠に無く、診断出力で引き続き監視する。
