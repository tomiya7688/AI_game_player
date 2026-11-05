Spec-Status: BLOCKED
Depends-On: #164（済）, #261（済）, #789（PR #821 未マージ）
Decision-Gaps: none
Parent: #18
Blocked-By: #789 の root lock（`RetentionCollector.coordination_lock()`）と `ArtifactAdmissionCoordinator.admit` が main に入るまで実装を始めない。設計上の未決はない。

# Issue #788 Runtime Retention Adapter：Session/Journal参照SnapshotとGC Triggerの本番接続

## 目的

#164 の `RetentionCollector` を本番Sessionへ接続する。削除の判断（重複→TTL→budget）は #164 の `plan_retention` をそのまま使い、本Issueは次の3つだけを足す。

1. **live reference snapshot**: Session が参照中の Artifact（Event Journal・Experience・実行中の保存/Replay）を毎回集め、`collect(live_references)` へ必ず渡す。
2. **安全な実行条件**: snapshot が取れない・不完全・古い・世代不一致なら削除しない（fail-closed）。参照の追加と GC の競合を lock で防ぐ。
3. **bounded trigger**: 毎frameではなく、条件と最短間隔・1回の作業上限を持つ trigger で GC を起動し、Decision hot path を待たせない。

## 現状調査

### 再利用するもの

- `retention_collector.py`（#164）: `collect(live_references)` は「計画→全削除先の安全検査→索引の原子置換→object削除」。`live_references` の既定値は空で、空のまま呼ぶと参照中の Artifact も TTL/budget で消え得る。本Issueはこの既定値を本番経路で使わせない。`plan()` は副作用なしで同じ計画を返す。
- `retention_planner.py`: `RetentionPlan`（`removed_entries`、`deleted_artifacts`、`blocked_entries`、`bytes_before/after`、`over_budget`、`over_hard_limit`）。Trace の値はここから取る。
- #789（PR #821、未マージ）: `RetentionCollector.coordination_lock()`（root単位の OS advisory lock＋process内 `RLock`、再入可、`lock_timeout_seconds` で有界、待ち時間msを返す）。`register`/`set_protected`/`collect` は同じ lock 内で動く。`ArtifactAdmissionCoordinator.admit(request, writer)` が put と register を1つの transaction にする。**本Issueは admission・pending record・補償を再実装せず、この2つを呼ぶだけ**。
- `event_journal.py`（#261）: `EventJournal` は1 Session 1 SQLite（`events(sequence PRIMARY KEY, event_id, envelope_json)`）。`EventEnvelope.artifact_refs` に `ArtifactReference` を持つ。`self._lock`（`RLock`）で同一instanceを直列化。**artifact参照を読み出す API は無い**。
- `experience_reader.py`: `JsonlExperienceReader(path)` が episode/step/event の `artifacts` を持つ。末尾の破損行だけを切り捨て `recovery_notices` に記録し、途中の破損は `ExperienceRecoveryError`。
- `runtime/session_composition.py`: `RuntimeComposition.create_session_runtime` が `EventJournal(<game_directory>/session_events/<session_id>.sqlite3)` を作って pipeline factory へ渡し、`_managed_session` が `ManagedSessionRuntime` を作る。`ManagedSessionRuntime.close()` は adaptation→実行resource→provider→`close_event_journal` の順に閉じる。

### 現状の欠落

- 本番コードに `RetentionCollector` の生成・`collect` 呼出しが無い（#164 はライブラリのみ）。
- Session の Artifact を置く Store root が無い。現在 Session の Journal へ `artifact_refs` を書く本番経路も無い（knowledge snapshot/promotion/challenger は別rootの別Storeで、本Issueの対象外）。
- Journal から参照だけを取り出す手段が無く、取るには全 envelope の再解析が必要。

## 範囲

- 新規 `src/ai_game_player/runtime/retention_adapter.py`: snapshot型、参照source、trigger policy、`RuntimeRetentionAdapter`、背景 worker。
- `event_journal.py` への最小追加: `artifact_refs_after()`（参照だけを sequence 順に有界取得）。
- `runtime/session_composition.py`: Session ごとの Store root・Collector・Admission coordinator・adapter の生成、pipeline への受け渡し、close 順序への組込み、Trace の Journal 記録。
- `SessionRuntimeConfiguration.retention`（既定有効の保守的 policy）。
- テスト、文書、data inventory への保存先追加。

## 範囲外

- value-aware ranking（#165）、orphan cleanup・reconciliation（#790）、Artifact put/register transaction 本体（#789）、Privacy UI（#225）。
- 終了済み Session の Store root の掃除（Session 外の maintenance tool。後続Issueを起票する）。
- knowledge snapshot / promotion / challenger の各 Store（Session に属さない別root）。
- 既存 pipeline が実際に Artifact を保存する producer の追加（本Issueは保存口と参照収集を用意する。最初の producer は後続）。

## 成果物

| ファイル | 内容 |
|---|---|
| `src/ai_game_player/runtime/retention_adapter.py`（新規） | `RetentionReferenceSnapshot`、`LiveReferenceProvider`、`JournalReferenceSource`、`ExperienceReferenceSource`、`PinnedReferenceSource`、`CompositeReferenceProvider`、`RetentionTrigger`、`RetentionTriggerPolicy`、`SkippedRetention`、`RuntimeRetentionReport`、`RetentionTraceEvent`、`RuntimeRetentionAdapter`、`RetentionWorker` |
| `src/ai_game_player/event_journal.py` | `artifact_refs_after(sequence, *, limit)` を追加（既存メソッドは不変） |
| `src/ai_game_player/runtime/session_composition.py` | `SessionRetentionConfig`、adapter 生成・pipeline 引数 `retention_adapter`（受け付ける factory だけ）・`ManagedSessionRuntime.close()` への組込み |
| `src/ai_game_player/assets/privacy/data_inventory.json` | `session_artifacts/<session_id>/` を保存先として追加 |
| `tests/test_runtime_retention_adapter.py`（新規） | 単体・並行・trigger |
| `tests/test_runtime_retention_integration.py`（新規） | `create_session_runtime` 経由で Collector が実際に呼ばれる統合試験 |
| `tests/test_event_journal.py` | `artifact_refs_after` の試験追加 |
| 文書 | `doc/architecture/retention_core.md`（runtime 接続節）、`doc/architecture/session_event_journal.md`（参照読出し）、`doc/versions.md` |

## 入出力（API・データ形式）

### RetentionReferenceSnapshot（frozen dataclass）

| field | 型 | 契約 |
|---|---|---|
| `session_id` | str | adapter の Session と一致しなければ stale |
| `generation` | int | 永続source（Journal/Experience）の位置が進むたびに adapter が単調増加させる値。snapshot 時点の値。pin の増減は含めない（後述） |
| `created_at_ms` | int | UNIXエポックms（Collector と同じ clock） |
| `artifact_sha256s` | frozenset[str] | 参照中 digest（小文字SHA-256、検証済み） |
| `references` | tuple[ArtifactReference, ...] | `collect` へ渡す値（digest 昇順・重複除去） |
| `source_positions` | Mapping[str, str] | source名→位置。例 `journal: "seq=1234"`、`experience:<name>: "size=…;mtime_ns=…;sha256=…"`、`pins: "version=17"` |
| `complete` | bool | 全sourceが最後まで読めた時だけ True |
| `incomplete_reasons` | tuple[str, ...] | `complete=False` の理由（例 `journal_catchup_pending`、`experience_recovered_tail`） |

`artifact_sha256s` と `references` の digest 集合は一致しなければ不正（`ValueError`）。Issue の API に `references` と `incomplete_reasons` を追加したのは、`collect` が `ArtifactReference` を要求するためと、Trace に理由を残すため。

### 参照 source

`LiveReferenceProvider.snapshot() -> RetentionReferenceSnapshot`。本番は `CompositeReferenceProvider` が次の source を合成する（1つでも例外・不完全なら `complete=False`）。

- `JournalReferenceSource(journal)`: 前回の位置から `journal.artifact_refs_after(last_sequence, limit=journal_batch_limit)` で差分だけ読み、digest→参照数の索引を process 内に持つ（Journal は append-only なので削除は起きない）。1回の snapshot で読むのは最大 `journal_batch_limit`（既定 10,000 event）。追いつかなければ `journal_catchup_pending` で不完全。毎 trigger の計算量は「前回からの増分」で、全履歴は Session 開始後の初回（既存 Journal を開いた場合）に batch で1回だけ読む。
- `ExperienceReferenceSource(path, *, max_bytes)`: `(size, mtime_ns)` が前回と同じなら前回の集合を再利用。変わった時だけ `JsonlExperienceReader` で読み直す（上限 `max_bytes`、既定 64MiB）。`recovery_notices` があれば `experience_recovered_tail`、`ExperienceRecoveryError`・上限超過は例外として不完全。本番 Session には Experience writer がまだ無いので、`SessionRetentionConfig.experience_paths` が空なら登録しない。
- `PinnedReferenceSource`: 保存途中・Replay 再生中など、Journal/Experience にまだ書かれていないが参照中の digest を token 単位で保持する（`pin(sha256, reason) -> PinToken`、`release(token)`、`version`）。

### EventJournal.artifact_refs_after

`artifact_refs_after(sequence: int, *, limit: int) -> JournalReferenceBatch(last_sequence: int, refs: tuple[ArtifactReference, ...], exhausted: bool)`。`self._lock` 内で `SELECT sequence, envelope_json FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?` を実行し、各行を既存の `EventEnvelope.from_dict` で検証して `artifact_refs` だけを返す。破損行は `EventJournalCorruptionError`（source は不完全扱い）。closed なら `EventJournalClosedError`。`limit` は 1〜100,000。

### RuntimeRetentionAdapter

```text
RuntimeRetentionAdapter(collector, admission, provider, policy, *, session_id, clock, trace)
  reserve(sha256, *, reason) -> PinToken
  persist(request, writer) -> PersistedArtifact            # reserve → admission.admit → on_artifact_persisted
  on_artifact_persisted(token, reference, entry_id, protected=False) -> None
  recording(references) -> ContextManager[None]            # Journal/Experience へ参照を書く間の gate
  release(token) -> None
  maybe_collect(trigger) -> RuntimeRetentionReport | SkippedRetention
  request(trigger) -> None                                 # 非同期。hot path 用
  close_session() -> RuntimeRetentionReport | SkippedRetention
  pressure -> Literal["none", "over_budget", "over_hard_limit"]
  stats -> RetentionStats
```

- `on_artifact_persisted` は Issue の `(reference, entry_id, protected)` に `token` を加えた。admit 完了から pin までの間に GC が新規 entry を budget 退避する窓を無くすため、digest を **admit 前に** pin しておく必要があるから。`token` の digest と `reference.sha256` が違えば `ValueError`。`persist()` はこの順序を内部で守る本番用の口で、`request.expected_sha256` を必須にする（無ければ `ValueError`。#789 の容量判定でも既存 digest の追加容量0判定に使える）。
- `recording(references)`: 呼出側は Journal へ `artifact_refs` を append する間この context に入る。gate（後述）を取り、各 digest の object が Store に存在するか（`store.object_path(d).is_dir()`）を確認し、無ければ `RetentionReferenceError` を送出して append させない（消えた Artifact への参照を作らない）。抜ける時に該当 pin を「Journal 記録済み」として外す。
- `PersistedArtifact(reference, entry_id, token)`。producer が記録を諦めた場合は `release(token)` する。release されない pin は `close_session()` まで残る（安全側）。

### RetentionTrigger / RetentionTriggerPolicy

`RetentionTrigger`: `soft_budget_crossing` / `persist_count` / `periodic` / `session_close` / `maintenance`。

| policy 欄 | 既定 | 意味 |
|---|---|---|
| `min_interval_ms` | 5,000 | 前回 GC 開始からこの間は `session_close`・`maintenance` 以外を coalesce |
| `persist_count_threshold` | 64 | 前回 GC 以降の保存件数がこれに達したら `persist_count` |
| `periodic_interval_ms` | 300,000 | 背景 worker の周期 |
| `max_deleted_artifacts_per_run` | 256 | 1回で物理削除する object 数の上限 |
| `trigger_lock_timeout_seconds` | 0.5 | trigger 時の root lock 待ち上限 |
| `close_lock_timeout_seconds` | 5.0 | close 時の待ち上限 |
| `snapshot_max_age_ms` | 2,000 | snapshot 作成から collect 判定までの許容時間 |
| `journal_batch_limit` | 10,000 | 1 snapshot で読む Journal event 数 |

soft budget crossing は hot path で計測しない。adapter は「直近 GC の `bytes_after` ＋以後に保存した `size_bytes` の合計」を推定使用量として持ち、`soft_budget_bytes` を超えた時に trigger する（実測は GC 内の Collector が行う）。

### 戻り値

- `SkippedRetention(trigger, reason, detail)`。`reason`: `not_due` / `coalesced` / `already_running` / `closed` / `store_absent` / `snapshot_failed` / `snapshot_incomplete` / `snapshot_stale` / `session_mismatch` / `generation_mismatch` / `lock_timeout` / `index_error` / `safety_error`。**どの理由でも削除は0件**。
- `RuntimeRetentionReport(trigger, report: RetentionReport, snapshot_generation, source_positions, deferred_artifacts: int, pressure, metrics)`。`pressure` は `plan.over_hard_limit` なら `over_hard_limit`、`plan.over_budget` なら `over_budget`、他は `none`。pressure がある結果は成功扱いにせず、`adapter.pressure` に保持し Trace に `outcome=pressure` で出す。
- 例外は外へ出さない（hot path・close を止めない）。ただし `recording()` の `RetentionReferenceError` と `persist()` の #789 由来の例外は呼出側へそのまま返す。

### RetentionTraceEvent（Journal へ記録）

`event_type="retention.gc/v1"`、`payload`: `trigger`、`outcome`（`collected`/`pressure`/`skipped`）、`skip_reason`、`gc_count`（累積）、`removed_entries`、`deleted_artifacts`、`removed_bytes`（`bytes_before - bytes_after`）、`bytes_after`、`blocked_refs`（`blocked_entries` のうち deferral 由来を除いた数）、`deferred_artifacts`、`over_budget`、`over_hard_limit`、`snapshot_generation`、`source_positions`、`lock_wait_ms`、`snapshot_ms`、`collect_ms`、`total_ms`、`gate_wait_ms`。digest 一覧・path は載せない。`artifact_refs` は空。Trace の記録は gate と root lock を解放した後に行い、記録失敗は `stats.trace_errors` に数えるだけで GC 結果を変えない。

## 手順

### 責務境界

- adapter: 「いつ・どの参照集合で・どれだけ」GC するか。計画と削除は Collector、保存は #789 の coordinator。
- source: 参照の読み出しだけ。raw payload（Artifact 本体）は読まない。Journal/Experience の記録 JSON だけを読む。
- Session composition: 生成・寿命・close 順序。

### Store root と生成

- root は `<game_directory>/session_artifacts/<session_id>/`。Session ごとに分ける。理由: 他 Session の Journal はこの adapter の snapshot に入らないため、root を共有すると他 Session の参照中 Artifact を消し得る。dedup は Session 内に限られる（容量増は許容し、範囲外の後続で扱う）。
- root は最初の `persist()` で #789 の lock が作る。GC 時に root が無ければ `store_absent` で skip。
- `SessionRetentionConfig(enabled=True, policy=RetentionPolicy(soft_budget_bytes=1 GiB, hard_limit_bytes=2 GiB), trigger=RetentionTriggerPolicy(), experience_paths=())`。`enabled=False` なら adapter を作らず、どの経路も `collect` を呼ばない。

### GC 1回の流れ（`maybe_collect`）

1. `closed` なら `closed`。`_run_lock` を非 blocking で取り、取れなければ `already_running`（coalesce）。
2. trigger 判定: `session_close`/`maintenance` は常に実行。他は `min_interval_ms` 未満なら `coalesced`、条件（件数・推定使用量・周期）を満たさなければ `not_due`。
3. **gate** を取る（process 内 `threading.Lock`。`recording()` と共有。待ち時間を `gate_wait_ms`）。
4. root lock を取る: `collector.coordination_lock()`（timeout は trigger 種別で切替）。期限切れは `lock_timeout`。
5. `provider.snapshot()`。例外→`snapshot_failed`、`complete=False`→`snapshot_incomplete`、`session_id` 不一致→`session_mismatch`、`clock() - created_at_ms > snapshot_max_age_ms` または `generation < last_seen_generation`→`snapshot_stale`、`generation != adapter.generation`（gate 保持中なので変わらないはず。変わっていれば provider の実装誤り）→`generation_mismatch`。いずれも削除しない。
6. live = snapshot の参照 ∪ その時点の pin（`_state_lock` 内で写す）。gate を持っていても `persist()` の pin は増え得るが、その digest の admit は root lock を待つため、GC が古い object を消した後に admit が新しく作り直す（参照切れにならない）。このため pin は generation に含めない。
7. 作業上限: `collector.plan(live)` を求め、`deleted_artifacts` が上限以下ならそのまま。超えるなら `deleted_artifacts` の先頭（計画の決定的順序）から上限件を「今回分」とし、索引上の他の全 digest を live に加えた（= 今回分以外は削除不可にした。`ArtifactReference` は `store.read_metadata(d).to_reference()` で作る）上で次へ進む。live を増やすことは削除を減らす方向にしか働かないので安全。加えた件数を `deferred_artifacts` とし、0 でなければ次回を `persist_count` 相当として即 `request()` する。
8. `collector.collect(live)`（同じ root lock 内の再入）。`RetentionIndexError`→`index_error`、`RetentionSafetyError`→`safety_error`（Collector の契約で索引・object は不変、または途中停止でも orphan のみ）。
9. 結果から `pressure` を決め、計数をリセット（推定使用量 = `bytes_after`）。
10. root lock・gate を解放し、Trace を記録。

### 状態遷移

```text
OPEN ──request/maybe_collect──▶ OPEN（RUNNING 中の要求は coalesce して1回に畳む）
OPEN ──close_session()──▶ CLOSING ──最終 collect 完了/skip──▶ CLOSED
CLOSED: maybe_collect/request は `closed`、persist は RuntimeError、release は無視
```

背景 worker（`RetentionWorker`、1本の non-daemon thread）は `request()` で立つ flag と `periodic_interval_ms` の timeout を `threading.Condition` で待ち、起きたら `maybe_collect` を1回呼ぶ。複数の request は1回に畳む。`close_session()` は worker を停止・join（上限 `close_lock_timeout_seconds`+1 秒。超えたら worker を見捨てず `stats.worker_join_timeout` を記録して close 側は続行。worker は `closed` を見て何もせず終わる）してから最終 GC を同期実行する。

### Session close の順序

`ManagedSessionRuntime.close()`: adaptation → 実行 resource → provider → **`journal.flush()` → `adapter.close_session()`** → `close_event_journal`。最終 GC は Journal が開いている間に行い、全 pin を release する前に snapshot を取る（close 時点で pin されている digest も守る）。最終 GC の後で pin は破棄する（次の Session はこの root を使わない）。`close_session()` の結果が skip・pressure でも close は失敗にしない（理由は Trace と `stats` に残る）。adapter が無い構成では従来どおり。

### 失敗時の挙動（fail-closed）

| 状況 | 動作 |
|---|---|
| provider 例外・Journal 破損・Experience 途中破損 | `snapshot_failed`、削除0 |
| Journal 追いつき未了・Experience 末尾切捨て | `snapshot_incomplete`、削除0 |
| snapshot が古い・世代が戻る・Session 違い・世代不一致 | `snapshot_stale`/`session_mismatch`/`generation_mismatch`、削除0 |
| root lock 待ち超過 | `lock_timeout`、削除0 |
| 索引・metadata 破損 | `index_error`、削除0（Collector が何も変えない） |
| 削除先が root 外・link | `safety_error`（Collector が索引を変えずに停止） |
| Protected/live だけで予算超過 | 削除できる分だけ削除し `pressure` を返す。成功扱いにしない |
| Trace 記録失敗 | GC 結果は変えず `trace_errors` を加算 |
| `recording()` 対象の object が無い | `RetentionReferenceError`、Journal へ書かせない |

本番経路は `collect()` を `live_references` 省略で呼ばない。`RuntimeRetentionAdapter` だけが `collect` を呼び、その引数は必ず検証済み snapshot から作る。これを単体試験と `rg "\.collect\(" src` の検査試験（adapter 以外の本番呼出しが無いこと）で固定する。

### 並行性

- lock 順序は **gate → root lock → journal lock** で固定する。`recording()` は gate だけを取り、その中で呼出側が Journal へ append する（gate → journal）。`persist()` は gate を取らず、pin（adapter 内部の短い `_state_lock`）→ admit（root lock）。`_state_lock` は他の lock を保持したまま他 lock を取らない葉の lock。逆順の取得は無い。
- 参照追加と GC の競合: 新しい参照は (a) 保存時に admit 前の pin、または (b) `recording()` 中の gate、のどちらかで GC と直列化される。GC は gate を持つ間に snapshot と collect を行うので、snapshot 後に Journal へ新規参照が入ることは無い。gate 外で Journal に参照を書く経路（規約違反）は、GC 後に `JournalReferenceSource` が増分を読んだ時、削除済み digest を指していれば `dangling_reference_detected` を Trace と `stats` に出す（検出のみ）。
- 他 process（maintenance tool 等）との競合は #789 の root lock が担う。
- 同じ adapter の GC は `_run_lock` で常に1本。
- hot path への影響: `request()` は flag を立てて `notify` するだけ（O(1)、blocking なし）。`recording()` は GC 実行中だけ待つ。GC の長さは削除上限と lock timeout で有界で、`gate_wait_ms` を Trace に出す。

## 代替案と不採用理由

- **snapshot を version 比較だけで守り lock を使わない**: snapshot 後に古い digest を新しく参照する event が入ると検出前に削除し得る。gate で直列化する。
- **毎 trigger で Journal 全件を読む**: Issue が禁止（O(total history)）。sequence 増分の索引にする。
- **Collector に削除件数上限の引数を足す**: #164 の削除アルゴリズムを変えずに済むよう、live を増やす方向（安全側）で作業量を絞る。
- **Store root を game 単位で共有**: 他 Session の参照が見えず誤削除し得る。Session 単位にする。
- **hot path で同期 GC**: Decision を待たせる。背景 worker＋coalesce にする。
- **`on_artifact_persisted` の引数を Issue どおりにする**: admit 完了から pin までの窓で新規 entry が退避され得る。admit 前に pin する `token` を加える。

## テスト（受入条件と1対1）

| # | Issue の試験項目 | 試験（`tests/test_runtime_retention_adapter.py` ほか） |
|---|---|---|
| T1 | live ref は TTL 切れ・budget 超過でも残る | TTL 0相当の期限切れ entry と予算超過 Store で、Journal が参照する digest が残り `blocked_refs` に数えられる |
| T2 | ref が外れた次回 snapshot では削除可能 | Pin を release／Experience から外した後の次回 GC で削除される |
| T3 | snapshot 失敗/partial/stale では削除ゼロ | provider 例外・`complete=False`・古い `created_at_ms`・世代逆行・Session 違い・世代不一致・Journal 破損行で、索引と object が不変、`SkippedRetention.reason` が各値 |
| T4 | Session close 時に有効 ref を守ったまま collect | `close_session()` で Journal 参照・pin 中 digest が残り、それ以外の期限切れが消える。skip/pressure でも `ManagedSessionRuntime.close()` が成功 |
| T5 | concurrent persist/collect | 複数 thread の `persist()`＋`recording()`＋Journal append と背景 GC を並行させ、終了後に Journal の全参照 digest の object が存在する（dangling 0）。gate を外すと失敗することを手元で確認 |
| T6 | over-budget だが全て Protected/live | 削除0、`pressure="over_budget"`/`over_hard_limit`、Trace `outcome=pressure` |
| T7 | trigger coalescing で per-frame GC しない | 1,000回の `request()`/`maybe_collect(persist_count)` を短時間に呼び、`min_interval_ms` 内の GC は1回、他は `coalesced`/`not_due`。`request()` が blocking しない（疑似 clock） |
| T8 | integration path で RetentionCollector が実際に呼ばれる | `tests/test_runtime_retention_integration.py`: `create_session_runtime` → `persist`＋Journal 記録 → close で Collector の `collect` が検証済み live 付きで呼ばれ、Journal に `retention.gc/v1` が残る |
| T9 | 作業上限 | 削除候補 300件で1回目は 256件、`deferred_artifacts=44`、次回で残りが消える。上限で残した digest は `blocked_refs` に入らない |
| T10 | Journal 増分読出し | `artifact_refs_after` の順序・limit・破損行・closed。adapter の source が2回目以降は増分だけを読む（読出し件数を計測） |
| T11 | production deletion は snapshot 必須 | adapter 以外の本番コードに `.collect(` が無いことの検査、adapter が `live_references` 省略で呼ばないこと |
| T12 | Trace の項目 | GC 回数・removed bytes・blocked refs・各 latency・over-budget が payload にあり、digest・path が無い。Trace 記録失敗で GC 結果が変わらない |
| T13 | `recording()` | object が無い digest を拒否し Journal に書かれない |

## Issue 完了条件との対応

| 完了条件 | 設計 | 試験 |
|---|---|---|
| 本番Sessionから#164 RetentionCollectorへ接続される | Session composition で adapter を生成し close と背景 worker から `collect` | T4, T8 |
| live reference snapshotなしではproduction deletionを実行しない | adapter だけが検証済み snapshot から `collect` を呼ぶ | T3, T11 |
| snapshot失敗時fail-closed | 失敗時の表 | T3 |
| bounded trigger policyがある | `RetentionTriggerPolicy`・背景 worker・作業上限 | T7, T9 |
| Journal/Experienceの参照を自動収集できる | `JournalReferenceSource`・`ExperienceReferenceSource` | T1, T2, T10 |
| concurrent persist/collectで参照整合性を壊さない | pin・gate・root lock・lock 順序 | T5, T13 |
| GC count/removed bytes/blocked refs/latency/over-budgetをTraceできる | `retention.gc/v1` | T12, T6 |
| runtime integration testがある | 統合試験 | T8 |

## 完了条件

- [ ] 上表の T1〜T13 が通る。`finish_task.bat` 全段 pass。
- [ ] `doc/architecture/retention_core.md` の「同時実行は呼出側が直列化」「runtime 接続は未実装」の記述を本実装に合わせて更新。
- [ ] data inventory に `session_artifacts` を追加し、data inventory の走査試験が通る。

## 未決事項（技術的に決めたもの）

1. **Spec-Status を BLOCKED にした**: 依存 #789 が未マージ。API（`coordination_lock`、`admit`）は PR #821 の設計どおりに使う。マージ時に名前が変われば本書を直してから実装する。
2. **Store root は Session 単位**: 他 Session の参照を snapshot に含められないため。終了済み Session の root の掃除は後続Issue（本PRで起票しない。実装PRで起票する）。
3. **既定は有効**: Session root にはこの adapter 経由の保存しか入らないため、既定有効でも他データを消さない。policy の既定値は Experience 用途の保守値（soft 1GiB/hard 2GiB、TTL なし）で、data inventory の型別 TTL（#224 `ttl_ms_by_artifact_type()`）を `ttl_ms_by_type` へ渡す。
4. **Experience source は設定時のみ**: 本番 Session に Experience writer が無いため。producer 追加時に `experience_paths` を設定する。
5. **trigger 既定値**: 5秒・64件・5分・256件は、#164 の索引上限（10万件）と Windows の rmtree 1件数 ms 程度から、1回の GC を概ね1秒以内に収める値として決めた。実測は実装時に Trace で確認し設計書へ記録する。
