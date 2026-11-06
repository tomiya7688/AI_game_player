Spec-Status: READY
Depends-On: #164（済）, #261（済）, #789（済、PR #821 / 7bd8500）
Decision-Gaps: none
Parent: #18

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
- #789（PR #821、main 取込済み 7bd8500）: `retention_root_lock.retention_root_lock(root, *, timeout_seconds)`（root単位の OS advisory lock＋process内 `RLock`、同一threadは深さを増やすだけで再入可、取得待ちmsを返す。期限超過は `RetentionLockTimeoutError`（`TimeoutError`）、root/lock file が link なら `RetentionRootUnsafeError`）。`RetentionCollector(store, policy, *, lock_timeout_seconds=...)` の `coordination_lock()` は同じ lock をコンストラクタの期限で取る。`register`/`set_protected`/`collect`/`discard_unindexed_object` は `@_serialized` で同じ lock 内、`plan`/`needs_collection` は lock を取らない読取。`measure() -> RetentionSnapshot(entries, usage)`。`ArtifactAdmissionCoordinator(collector, *, trace=None, records=None).admit(request: ArtifactAdmissionRequest, writer: Callable[[AdmissionPutPort], ArtifactReference]) -> ArtifactReference`（request は `payload_size`、`expected_sha256: str | None`、`artifact_type`、`media_type`、`entry_id`、`protected`、`source_ref`、`sensitive=False`）。失敗は `ArtifactAdmissionError`。**本Issueは admission・pending record（`PendingAdmissionStore`）・補償・`inspect_pending`/`recover_pending` を再実装せず、これらを呼ぶだけ**。
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
- GC 1回の走査量の分割（#164 の計画が索引全件を読むため。必要なら #164 側の Issue で扱う）。

## 成果物

| ファイル | 内容 |
|---|---|
| `src/ai_game_player/runtime/retention_adapter.py`（新規） | `RetentionReferenceSnapshot`、`LiveReferenceProvider`、`JournalReferenceSource`、`ExperienceReferenceSource`、`PinnedReferenceSource`、`CompositeReferenceProvider`、`RetentionTrigger`、`RetentionTriggerPolicy`、`SkippedRetention`、`RuntimeRetentionReport`、`RetentionTraceEvent`、`RuntimeRetentionAdapter`、`RetentionWorker` |
| `src/ai_game_player/event_journal.py` | `artifact_refs_after(sequence, *, limit)` と `set_reference_guard(guard)` を追加。`append` は guard 設定時かつ `artifact_refs` が空でない時だけ guard を呼ぶ（それ以外の挙動は不変） |
| `src/ai_game_player/runtime/session_composition.py` | `SessionRetentionConfig`、adapter 生成・pipeline 引数 `retention_adapter`（受け付ける factory だけ）・`ManagedSessionRuntime.close()` への組込み |
| `src/ai_game_player/assets/privacy/data_inventory.json` | `session_artifacts/<session_id>/` を保存先として追加 |
| `tests/test_runtime_retention_adapter.py`（新規） | 単体・並行・trigger |
| `tests/test_runtime_retention_integration.py`（新規） | `create_session_runtime` 経由で Collector が実際に呼ばれる統合試験 |
| `tests/test_event_journal.py` | `artifact_refs_after`・`set_reference_guard` の試験追加 |
| 文書 | `doc/architecture/retention_core.md`（runtime 接続節）、`doc/architecture/session_event_journal.md`（参照読出し）、`doc/versions.md` |

## 入出力（API・データ形式）

### RetentionReferenceSnapshot（frozen dataclass）

| field | 型 | 契約 |
|---|---|---|
| `session_id` | str | adapter の Session と一致しなければ stale |
| `generation` | int | 参照を持つ変化（Journal の `artifact_refs` 付き event の取込み、Experience の内容変化）を source が観測するたびに adapter が単調増加させる値。snapshot 時点の値。参照の無い event による sequence 前進と pin の増減は含めない（後述「generation と線形化点」） |
| `created_at_ms` | int | UNIXエポックms（Collector と同じ clock） |
| `artifact_sha256s` | frozenset[str] | 参照中 digest（小文字SHA-256、検証済み） |
| `references` | tuple[ArtifactReference, ...] | `collect` へ渡す値（digest 昇順・重複除去） |
| `source_positions` | Mapping[str, str] | source名→位置。例 `journal: "seq=1234"`、`experience:<name>: "size=…;mtime_ns=…;sha256=…"`、`pins: "version=17"` |
| `complete` | bool | 全sourceが最後まで読めた時だけ True |
| `incomplete_reasons` | tuple[str, ...] | `complete=False` の理由（例 `journal_catchup_pending`、`experience_recovered_tail`） |

`artifact_sha256s` と `references` の digest 集合は一致しなければ不正（`ValueError`）。Issue の API に `references` と `incomplete_reasons` を追加したのは、`collect` が `ArtifactReference` を要求するためと、Trace に理由を残すため。

### 参照 source

`LiveReferenceProvider.snapshot() -> RetentionReferenceSnapshot`。本番は `CompositeReferenceProvider` が次の source を合成する（1つでも例外・不完全なら `complete=False`）。

- `JournalReferenceSource(journal)`: 前回の位置から `journal.artifact_refs_after(last_sequence, limit=journal_batch_limit)` で差分だけ読み、digest→参照数の索引を process 内に持つ（Journal は append-only なので削除は起きない）。1回の snapshot で読むのは最大 `journal_batch_limit`（既定 10,000 event）。追いつかなければ `journal_catchup_pending` で不完全。毎 trigger の計算量は「前回からの増分」で、全履歴は Session 開始後の初回（既存 Journal を開いた場合）に batch で1回だけ読む。 cursor は「直前の batch の `last_sequence`（参照の無い event も含めて読んだ最後の sequence）」で、`artifact_refs_after` が返す `last_sequence` へだけ進める。読取は `journal._lock` 内の1回の SELECT なので、batch の途中に append が割り込むことはない。参照の無い event は削除可否に影響しないため、cursor の後に追加されても snapshot の正しさを損なわない（次回の batch で読み飛ばされるだけ）。`source_positions["journal"]` は snapshot に使った cursor。
- `ExperienceReferenceSource(path, *, max_bytes)`: **writer 契約**: Experience へ参照を書く writer は append-only または atomic replace（同じ path への in-place 書換え禁止）で、参照付きの書込みを Journal と同じく `recording(references, target="experience:<name>")` の中で行う。adapter は `target` 付きの context を例外なく抜けるたびにその source の `content_version` を1増やす。source は `content_version` が前回読取時と同じで、かつ `(st_dev, st_ino, size, mtime_ns)` も同じ時だけ前回の集合を再利用する（stat は補助。内容変化の検出は writer 契約の version で行い、mtime 粒度に依存しない）。読み直す時は1つの file handle から上限 `max_bytes`（既定 64MiB）まで bytes を一度に読み、メモリ上で `JsonlExperienceReader` と同じ規則で解析する（append 途中の末尾は `experience_recovered_tail`、atomic replace ならどちらかの版全体が読まれる）。読取前後の stat が違えば `experience_changed_during_read` で不完全。`ExperienceRecoveryError`・上限超過は例外として不完全。GC は gate を持つので、GC 中に writer が参照を追加することはない。本番 Session には Experience writer がまだ無いので、`SessionRetentionConfig.experience_paths` が空なら登録しない。writer を追加する PR は、この契約に従うことを本書へ追記してから `experience_paths` を設定する。
- `PinnedReferenceSource`: 保存途中・Replay 再生中など、Journal/Experience にまだ書かれていないが参照中の digest を token 単位で保持する（`pin(sha256, reason) -> PinToken`、`release(token)`、`version`）。

### EventJournal.artifact_refs_after

`artifact_refs_after(sequence: int, *, limit: int) -> JournalReferenceBatch(last_sequence: int, refs: tuple[ArtifactReference, ...], exhausted: bool)`。`self._lock` 内で `SELECT sequence, envelope_json FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?` を実行し、各行を既存の `EventEnvelope.from_dict` で検証して `artifact_refs` だけを返す。破損行は `EventJournalCorruptionError`（source は不完全扱い）。closed なら `EventJournalClosedError`。`limit` は 1〜100,000。

### RuntimeRetentionAdapter

```text
RuntimeRetentionAdapter(collector, admission, provider, policy, *, session_id, clock, trace)
  reserve(sha256, *, reason) -> PinToken
  persist(request, writer) -> PersistedArtifact            # reserve → admission.admit → on_artifact_persisted
  on_artifact_persisted(token, reference, entry_id, protected=False) -> None
  recording(references, *, target="journal", timeout_seconds=1.0) -> ContextManager[None]  # 参照を書く間の gate
  release(token) -> None
  maybe_collect(trigger) -> RuntimeRetentionReport | SkippedRetention
  request(trigger) -> None                                 # 非同期。hot path 用
  close_session() -> RuntimeRetentionReport | SkippedRetention
  pressure -> Literal["none", "over_budget", "over_hard_limit"]
  stats -> RetentionStats
```

- `on_artifact_persisted` は Issue の `(reference, entry_id, protected)` に `token` を加えた。admit 完了から pin までの間に GC が新規 entry を budget 退避する窓を無くすため、digest を **admit 前に** pin しておく必要があるから。`token` の digest と `reference.sha256` が違えば `ValueError`。`persist()` はこの順序を内部で守る本番用の口で、`request.expected_sha256` を必須にする（無ければ `ValueError`。#789 の容量判定でも既存 digest の追加容量0判定に使える）。
- `recording(references, *, target="journal", timeout_seconds=1.0)`: `target` は `journal` か `experience:<name>`。呼出側は Journal（または Experience）へ `artifact_refs` を append する間この context に入る。gate（後述）を期限付きで取り（超過は `RetentionLockTimeoutError`。何も書かずに返るので、呼出側は後で再試行するか、参照を付けずに記録して `release(token)` する）、各 digest の object が Store に存在するか（`store.object_path(d).is_dir()`）を確認し、無ければ `RetentionReferenceError` を送出して append させない（消えた Artifact への参照を作らない）。context を**例外なく**抜けた時だけ（= append が commit 済み）該当 pin を「Journal 記録済み」として外す。例外で抜けた場合は pin を残す（append の成否が不明でも安全側）。gate 保持中に commit された event は、次の GC が gate を取った後の snapshot で必ず読まれる（Journal は commit 順の sequence で、source は前回位置から読む）ため、pin を外しても snapshot から漏れない。
- **gate 必須の強制**: adapter は生成時に `journal.set_reference_guard(adapter.assert_recording)` を設定する。`EventJournal.append` は `artifact_refs` が空でない時にだけ guard を呼び、呼出 thread がその digest を含む `recording()` の中にいなければ `RetentionReferenceError` で append を拒否する（gate 外で参照を書く経路を API 境界で無くす）。guard 未設定の Journal（adapter 無効・既存テスト）は従来どおり。
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
- 例外は外へ出さない（hot path・close を止めない）。ただし `recording()` の `RetentionReferenceError`/`RetentionLockTimeoutError` と `persist()` の #789 由来の例外は呼出側へそのまま返す。
- `close_session()` の結果は `adapter.last_close_result` に保持し、`ManagedSessionRuntime.retention_close_result` で呼出側が参照できる。skip・pressure だった root は削除せずそのまま残し、後続の maintenance tool（範囲外）が同じ Collector 契約で再実行する。

### RetentionTraceEvent（Journal へ記録）

`event_type="retention.gc/v1"`、`payload`: `trigger`、`outcome`（`collected`/`pressure`/`skipped`）、`skip_reason`、`gc_count`（累積）、`removed_entries`、`deleted_artifacts`、`removed_bytes`（`bytes_before - bytes_after`）、`bytes_after`、`blocked_refs`（`blocked_entries` のうち deferral 由来を除いた数）、`deferred_artifacts`、`over_budget`、`over_hard_limit`、`snapshot_generation`、`source_positions`、`lock_wait_ms`、`snapshot_ms`、`collect_ms`、`total_ms`、`gate_wait_ms`。digest 一覧・path は載せない。`artifact_refs` は空。Trace の記録は gate と root lock を解放した後に行い、記録失敗は `stats.trace_errors` に数えるだけで GC 結果を変えない。削除判断に進まなかった skip（`not_due`/`coalesced`/`already_running`/`closed`/`store_absent`）は Journal に記録せず `stats.skipped` にだけ数える（毎 frame の呼出しで Journal を膨らませず、保存の無い Session の Journal を変えないため）。

## 手順

### 責務境界

- adapter: 「いつ・どの参照集合で・どれだけ」GC するか。計画と削除は Collector、保存は #789 の coordinator。
- source: 参照の読み出しだけ。raw payload（Artifact 本体）は読まない。Journal/Experience の記録 JSON だけを読む。
- Session composition: 生成・寿命・close 順序。

### Store root と生成

- root は `<game_directory>/session_artifacts/<session_id>/`。Session ごとに分ける。理由: 他 Session の Journal はこの adapter の snapshot に入らないため、root を共有すると他 Session の参照中 Artifact を消し得る。dedup は Session 内に限られる（容量増は許容し、範囲外の後続で扱う）。
- root は最初の `persist()` で #789 の lock が作る。GC 時に root が無ければ `store_absent` で skip。
- `SessionRetentionConfig(enabled=True, policy=RetentionPolicy(soft_budget_bytes=1 GiB, hard_limit_bytes=2 GiB), trigger=RetentionTriggerPolicy(), experience_paths=())`。`enabled=False` なら adapter を作らず、どの経路も `collect` を呼ばない。
- 生成順序（`create_session_runtime` 内、`EventJournal` 生成の直後・pipeline factory の前）: `FileSystemArtifactStore(root)` 相当の参照 → `RetentionCollector` → `ArtifactAdmissionCoordinator` → provider → adapter → `journal.set_reference_guard`。どれもディスクへ書かない（root は作らない）。policy 不正などの構成エラーは Session 開始を失敗させ、既存の初期化失敗時の後始末（pipeline・Journal・provider の close）に合流する。pipeline factory への `retention_adapter` は受け付ける factory だけに渡す（既存 `_accepted_pipeline_arguments`）。背景 worker は、Artifact を一度も保存しない Session に idle thread を残さないよう、最初の `persist()`/`request()` で開始する（実装時に変更。生成は adapter の初期化時、開始は遅延）。`close_session()` が停止・join する。

### GC 1回の流れ（`maybe_collect`）

1. `closed`（open でない）なら `closed`。`_run_lock` を非 blocking で取り、取れなければ `already_running`（coalesce）。
2. trigger 判定: `session_close`/`maintenance` は `min_interval_ms`・条件判定を**無条件に迂回**して実行する（直前の GC 直後でも実行）。それ以外は `min_interval_ms` 未満なら `coalesced`、条件（件数・推定使用量・周期）を満たさなければ `not_due`。
3. **gate** を取る（process 内 `threading.Lock`。`recording()` と共有。待ち時間を `gate_wait_ms`）。
4. root lock を取る: `coordination_lock()` は期限がコンストラクタ固定のため、外側は `retention_root_lock(Path(store.root), timeout_seconds=<trigger 種別の期限>)` で直接取る（同じ root key の lock なので、続く `collect` の `@_serialized` は同一threadの再入として深さを増やすだけ）。root が無ければ lock を取る前に `store_absent`（lock は root を作るため、先に存在を確認する）。期限切れは `lock_timeout`。
5. `provider.snapshot()`（`CompositeReferenceProvider` は source の例外をそのまま伝え、adapter が `snapshot_failed` にする）。例外→`snapshot_failed`、`complete=False`→`snapshot_incomplete`、`session_id` 不一致→`session_mismatch`、`clock() - created_at_ms > snapshot_max_age_ms` または `generation < last_seen_generation`→`snapshot_stale`。collect の直前に source の増分読取をもう1回行い（gate 保持中なので通常は0件）、参照付き event が増えていれば `generation_mismatch`。いずれも削除しない。
6. live = snapshot の参照 ∪ その時点の pin（`_state_lock` 内で写す）。gate を持っていても `persist()` の pin は増え得るが、その digest の admit は root lock を待つため、GC が古い object を消した後に admit が新しく作り直す（参照切れにならない）。このため pin は generation に含めない。
7. 作業上限: `collector.plan(live)` を求め、`deleted_artifacts` が上限以下ならそのまま。超えるなら `deleted_artifacts` の先頭（計画の決定的順序）から上限件を「今回分」とし、索引上の他の全 digest を live に加えた（= 今回分以外は削除不可にした）上で次へ進む。追加する参照は、Collector が `sha256` しか使わないため、`store.read_metadata` を索引全件に対して読まず、digest だけが意味を持つ固定の参照（`retention.deferred`、`application/octet-stream`）にした（実装時に変更。メタデータ読取の O(N) 増加を避ける）。live を増やすことは削除を減らす方向にしか働かないので安全。この絞った live で手順8の `collect` が計画を作り直し、索引置換→object削除の既存の原子的順序で適用する（索引と削除対象は常に同じ計画から作られ、「索引から外したのに消さない」「消したのに索引に残る」は起きない）。加えた件数を `deferred_artifacts` とし、0 でなければ次回を `persist_count` 相当として即 `request()` する。
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

- lock 順序は **gate → root lock → journal lock** で固定する。`recording()` は gate だけを取り、その中で呼出側が Journal へ append する（gate → journal）。`persist()` は gate を取らず、pin（adapter 内部の短い `_state_lock`）→ admit（root lock）。`_state_lock` は保持中に他の lock を取らない葉の lock。逆順の取得は無い。
- 参照追加と GC の競合: 新しい参照は (a) 保存時に admit 前の pin、または (b) `recording()` 中の gate、のどちらかで GC と直列化される。GC は gate を持つ間に snapshot と collect を行い、Journal は guard により gate 外で参照を受け付けないので、snapshot 後に Journal へ新規参照が入ることは無い。
- persist の pin と GC: GC は **root lock を取った後**（手順4の後）に pin を写す（手順6）。(i) それ以前に付いた pin は live に入る。(ii) それ以後に付いた pin の admit は GC の root lock 解放を待つ。admit は lock 取得後に既存有無を判定する（#789 手順2〜6）ので、GC が古い object を消していれば新しく作り直し、消していなければ既存 digest として登録する。どちらでも admit が返す参照の object は存在する。この順序を T5b で固定する。
- generation と線形化点: snapshot の線形化点は「gate と root lock を両方持った状態で source を読み終えた時点」。参照の無い event による sequence 前進は削除可否に影響しないので generation を進めない。複数 source を順に読む間の変化は gate により参照付き Journal event では起きず、Experience も writer 契約で gate 内でしか参照を追加しない。参照の無い Journal event が snapshot 中に増えても、cursor は読んだ batch の `last_sequence` までしか進まず、削除対象の判定に影響しない。
- 他 process（maintenance tool 等）との競合は #789 の root lock が担う。
- 同じ adapter の GC は `_run_lock` で常に1本。
- hot path への影響: `request()` は flag を立てて `notify` するだけ（O(1)、blocking なし）。`recording()` と `persist()`（root lock）は GC 実行中だけ待ち、待ちは `timeout_seconds` で有界（超過時は何も書かずに例外）。GC 1回の計算量は #164 の Collector が索引全件と全 metadata を読むため O(索引件数)（上限 `policy.max_entries`）で、削除数は上限で、頻度は `min_interval_ms` で抑える。走査量を1回ごとに分割するには #164 の計画アルゴリズムの変更が必要なため本Issueでは行わない（範囲外に記載）。`gate_wait_ms`・`lock_wait_ms`・`collect_ms` を Trace に出し、実測を実装時に記録する。

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
| T5b | persist の pin と GC の順序 | GC が root lock を持った後に同じ digest を `persist()` し、GC が古い object を消した場合も消さなかった場合も、`persist()` の返す参照の object が存在し索引に entry がある |
| T5 | concurrent persist/collect | 複数 thread の `persist()`＋`recording()`＋Journal append と背景 GC を並行させ、終了後に Journal の全参照 digest の object が存在する（dangling 0）。gate を外すと失敗することを手元で確認 |
| T6 | over-budget だが全て Protected/live | 削除0、`pressure="over_budget"`/`over_hard_limit`、Trace `outcome=pressure` |
| T7 | trigger coalescing で per-frame GC しない | 1,000回の `request()`/`maybe_collect(persist_count)` を短時間に呼び、`min_interval_ms` 内の GC は1回、他は `coalesced`/`not_due`。`request()` が blocking しない（疑似 clock） |
| T8 | integration path で RetentionCollector が実際に呼ばれる | `tests/test_runtime_retention_integration.py`: `create_session_runtime` → `persist`＋Journal 記録 → close で Collector の `collect` が検証済み live 付きで呼ばれ、Journal に `retention.gc/v1` が残る |
| T9 | 作業上限 | 削除候補が上限を超える時、1回目は上限件数だけ消し、`deferred_artifacts` に残りを数え、次回で残りが消える。上限で残した digest は `blocked_refs` に入らない（試験は Collector の計画が索引全件の metadata を読むため 30件・上限25件・見送り5件に縮小。分岐は 300件/256件/44件と同じ） |
| T10 | Journal 増分読出し | `artifact_refs_after` の順序・limit・破損行・closed。adapter の source が2回目以降は増分だけを読む（読出し件数を計測） |
| T11 | production deletion は snapshot 必須 | adapter 以外の本番コードに `.collect(` が無いことの検査、adapter が `live_references` 省略で呼ばないこと |
| T12 | Trace の項目 | GC 回数・removed bytes・blocked refs・各 latency・over-budget が payload にあり、digest・path が無い。Trace 記録失敗で GC 結果が変わらない |
| T13 | `recording()` | object が無い digest を拒否し Journal に書かれない。gate 外の `append(artifact_refs=...)` は guard で拒否。例外で抜けた context は pin を残す。gate 待ち超過で `RetentionLockTimeoutError`（何も書かれない） |
| T14 | close 時の間隔迂回 | 直前の GC 直後に `close_session()` しても最終 GC が実行される |
| T15 | Experience の変化検出 | `recording(target="experience:x")` 後は stat が同じでも読み直す。読取前後で stat が変わると `snapshot_incomplete`（削除0）。参照の無い Journal event が snapshot 中に追加されても cursor・削除結果が変わらない |

## Issue 完了条件との対応

| 完了条件 | 設計 | 試験 |
|---|---|---|
| 本番Sessionから#164 RetentionCollectorへ接続される | Session composition で adapter を生成し close と背景 worker から `collect` | T4, T8 |
| live reference snapshotなしではproduction deletionを実行しない | adapter だけが検証済み snapshot から `collect` を呼ぶ | T3, T11 |
| snapshot失敗時fail-closed | 失敗時の表 | T3 |
| bounded trigger policyがある | `RetentionTriggerPolicy`・背景 worker・作業上限・close 迂回 | T7, T9, T14 |
| Journal/Experienceの参照を自動収集できる | `JournalReferenceSource`・`ExperienceReferenceSource` | T1, T2, T10 |
| concurrent persist/collectで参照整合性を壊さない | pin・gate・Journal guard・root lock・lock 順序 | T5, T5b, T13, T15 |
| GC count/removed bytes/blocked refs/latency/over-budgetをTraceできる | `retention.gc/v1` | T12, T6 |
| runtime integration testがある | 統合試験 | T8 |

## 完了条件

- [ ] 上表の T1〜T15（T5b を含む）が通る。`finish_task.bat` 全段 pass。
- [ ] `doc/architecture/retention_core.md` の「同時実行は呼出側が直列化」「runtime 接続は未実装」の記述を本実装に合わせて更新。
- [ ] data inventory に `session_artifacts` を追加し、data inventory の走査試験が通る。

## 未決事項（技術的に決めたもの）

1. **Spec-Status を READY にした**: #789（PR #821）が main に入り（7bd8500）、`retention_root_lock`・`RetentionCollector.coordination_lock`/`collect`・`ArtifactAdmissionCoordinator.admit`・`ArtifactAdmissionRequest` の実 API を原文で照合した。差分は lock 期限の指定方法だけで、手順4を `retention_root_lock` の直接利用に直した。
2. **Store root は Session 単位**: 他 Session の参照を snapshot に含められないため。終了済み Session の root の掃除は後続Issue（本PRで起票しない。実装PRで起票する）。
3. **既定は有効**: Session root にはこの adapter 経由の保存しか入らないため、既定有効でも他データを消さない。policy の既定値は Experience 用途の保守値（soft 1GiB/hard 2GiB、TTL なし）で、data inventory の型別 TTL（#224 `ttl_ms_by_artifact_type()`）を `ttl_ms_by_type` へ渡す。
4. **Experience source は設定時のみ**: 本番 Session に Experience writer が無いため。producer 追加時に `experience_paths` を設定する。
5. **trigger 既定値と実測**: 5秒・64件・5分・256件は、#164 の索引上限（10万件）と Windows の rmtree 1件数 ms 程度から決めた。実装時の実測（このPC、Windows、ローカルdisk）では `RetentionCollector.plan` が 300 entries で約 6.3 秒（1 entry あたり約 21ms、metadata 読取と未索引走査が支配）で、GC 1回は `plan` を2回（上限判定用と `collect` 内）行うため、その約2倍かかる。設計時の「1回の GC を概ね1秒以内」は索引が約 20 件程度までの目安であり、大きい索引では背景 worker の1回が数十秒になり得る。hot path（`request`）は待たないが、`recording()`/`persist()` は GC 実行中の root lock/gate を `timeout` まで待つ。対策（計画の増分化・metadata の cache）は #164 側の課題として後続 Issue に分ける。上限判定のための事前 `plan` を省く最適化（`collect` へ計画を渡す）は Collector の契約変更になるため本 Issue では行わない。

## 実装時の確定事項（2026-11-07）

- **変更点**: 背景 worker の開始を最初の保存/要求まで遅らせた、作業上限の補助 live を固定の参照にした、`CompositeReferenceProvider` は source の例外を伝える、判定に進まなかった skip は Journal へ記録しない。いずれも上記本文へ反映した。
- **Journal 読取の分離**: `artifact_refs_after` は SELECT だけを Journal の lock 内で行い、行の検証（`EventEnvelope.from_dict`）は lock の外で行う（lock 保持時間を短くするため。読み出した行は不変）。
- **`on_artifact_persisted(protected=True)`**: admit が既に `request.protected` で登録しているため、`persist()` は `protected=False` で呼ぶ。外部 producer が後から Protected にしたい時だけ `protected=True` を渡し、`set_protected` を呼ぶ。
- **pin の参照解決**: pin 中でも object がまだ無い digest（admit 前）は、削除対象になり得ないため live への追加を飛ばす。
- **実測**: 上記「未決事項」5。

## 設計レビュー（packet-01 SHA256 5C4DE971…、packet-02 SHA256 8B134C53…）

証跡: `E:/projects/.review-work/AI_GAME_Player/issue-788-design/evidence.md`。返答は Codex Luna（2資料）と Ori（資料1、指摘なし）。Continue・Antigravity・Grok は無応答/stream失敗、Qwen は経路停止、Jules はセッション作成 400 FAILED_PRECONDITION で省略。

Luna の指摘を原典と照合し、全て設計の明確化・強化として反映した。

| 指摘 | 判断 | 反映 |
|---|---|---|
| generation と snapshot の線形化点が不明 | 有効 | generation の定義（参照付き変化のみ）と線形化点、collect 直前の増分再読取 |
| `recording()` の pin 解除時機 | 有効 | 例外なく抜けた時だけ解除、例外時は pin 維持 |
| gate 外の Journal 書込みは検出のみ | 有効 | `EventJournal.set_reference_guard` で gate 外の参照付き append を拒否（API 境界で強制） |
| GC 中に付いた persist の pin | 有効（説明不足） | root lock 取得後に pin を写す順序と admit の再判定で安全な理由を明記、T5b |
| Experience の mtime 粒度・読取中変化 | 有効 | writer 契約（append-only/atomic replace、gate 内で参照追加）と `content_version` で検出、1 handle 読取、T15 |
| 削除上限の適用と原子性 | 有効（説明不足） | 絞った live で collect が計画を作り直し既存の原子的順序で適用 |
| GC の走査量・`recording()` の待ちが有界でない | 一部有効 | 待ちに timeout を追加。走査量は #164 の計画が O(索引件数) で、分割は範囲外として明記 |
| close 結果の保持・再実行 | 有効 | `last_close_result`・root を残し maintenance で再実行 |
| 生成順序・失敗時 | 有効 | 生成順序と構成エラー時の後始末を明記 |
| close が最短間隔で skip し得る | 有効 | 無条件迂回を明記、T14 |

再確認（packet-03 SHA256 4DCC5825…、Luna）: (1) Journal cursor と参照の無い event の関係が不明 → 有効、cursor 規則と1 SELECT 内の読取を明記。(2) stat だけでは同一 stat の更新を検出できない → 有効、writer 契約と `content_version` に変更（`experience_reuse_max_ms` は不要になり削除）。
