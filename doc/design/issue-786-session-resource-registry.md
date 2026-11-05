Spec-Status: READY（今回のスライス。pressure発生源との接続は#425/#546待ちの残件）
Depends-On: none（今回のスライス）。pressure発生源の接続は #425 / #546、実ownerの接続は #571 / #534 / #334
Decision-Gaps: none（pressure入力の型は本Issueで消費側の最小契約として決定。根拠は「未決事項と決定」）
Parent: #785

# Issue #786 Session Resource Lease Registry / Cleanup Coordinator

Session内で確保・借用されるruntime resourceの所有者・lease・解放経路を1か所に列挙し、release / task終了 / generation切替 / Stop / error / pressure / 期限切れの全てを同じ解放経路で処理する`SessionResourceRegistry`と`CleanupCoordinator`の設計。各resourceの実際の解放処理（#571 Frame/SharedMemory、#534 GPU cache、#334 GDI）は再実装せず、owner APIをAdapterとして登録する。

## 現状調査

### 既存の解放経路（再利用し、置き換えない）

| 資源 | 現在の所有者と解放 | 本Issueでの扱い |
|---|---|---|
| Pipeline / Provider / Light Adaptation / Event Journal | `runtime/session_composition.py`の`ManagedSessionRuntime.close()`。adaptation → 実行資源 → provider → journalの順。失敗は集めて`RuntimeError`、成功した部品だけ閉じた扱いにして再試行可能 | 順序と再試行の意味は変えない。Registryの`close_session()`をこの列の**先頭**（journalが開いている間）に足す |
| Session停止 | `game_session.py`の`GameSessionController.stop()/_finish()/_fail()`が`run_control.stop()`の後に`_close_runtime()`を呼ぶ。step実行中のstopはstep完了後にclose | 変更しない。Stop/error/完了はこの既存経路で`ManagedSessionRuntime.close()`に届く |
| 世代 | `run_control.py`の`RunController.rearm_token`（明示start/re-armでだけ増加。古いtokenの推論結果は`ExecutionCancelled`で破棄） | Registryのgenerationとして使う（新しい世代番号を作らない） |
| GDI DC/Bitmap | `screen_capture.py`の`_capture_unrestricted`が1回のcapture内で`finally`解放（#334が順序の正本） | 1呼出し内で閉じるためRegistryに登録しない（二重解放を避ける） |
| WGC session / Native Runtime | `WindowsScreenCapture.close()`（冪等、失敗時は送出） | Session所有で使う場合は`ClosableOwnerAdapter`で登録できる |
| Planner作業directory / worker process | `bounded_planner.py`の`_planning_directory`（`TemporaryDirectory`）と`_stop_owned_process`。1回の`plan()`内で`finally`解放 | 1呼出し内で閉じるため登録しない |
| Frame配送 | `shared_frame_producer.py`の`source_generation`、`frame_subscriber.py`の`FrameSubscriber.close()` | 実Frame lease/TTLは#571の正本。Subscriberを長寿命で持つ場合はAdapterで登録できる |
| Provider close規約 | `runtime/composition.py`の`_close_provider`（closeが無ければshutdown）、`LifecycleHandle` | `ClosableOwnerAdapter`は同じ規約を使う |
| 資源圧 | `light_adaptation_budget.py`の`LightResourceSample`（Light更新のadmission専用）。#425/#546のpressure snapshotは未実装 | 流用しない（意味が違う）。消費側の最小契約を本Issueで定める |
| 安全lease | `fail_safe_runtime.py`の観測lease（Safetyの期限付き許可） | 別概念。名前の衝突を避けるため本Issueの型は全て`Resource`接頭辞を付け、fail-safeには触れない |

### 不足していること

- Session/generation/taskの単位で「今いくつ何が生きているか」を列挙する場所が無い。#787が測る対象（active/failed lease数）も無い。
- 解放失敗は`RuntimeError`に連結されるだけで、資源単位の状態・件数・所要時間が残らない。
- stale generationの資源をconsumerへ渡さない検査、二重解放の検出、pressure時の上限付き解放順序が無い。

## 成果物

1. 新規 `src/ai_game_player/runtime/resource_lifecycle.py`
   - 型: `ResourceScope`、`ResourceState`、`ResourceLease`、`ResourceReleaser`（Protocol）、`ClosableOwnerAdapter`、`ResourceBorrow`、`ResourcePressureSnapshot`、`CleanupReport`、`ResourceLifecycleSnapshot`、`ResourceLifecycleError`、`ResourceTraceSink`（Protocol）
   - `SessionResourceRegistry`（登録・借用・解放・scope一括解放・snapshot）
   - `CleanupCoordinator`（pressure時と期限切れの上限付き解放）
2. 変更 `src/ai_game_player/runtime/session_composition.py`（必要最小限）
   - `ManagedSessionRuntime`がRegistryを1つ所有し、`resources`属性で公開。`close()`の先頭で`close_session()`。`run()/run_and_execute()`を`task_scope`で包み、step開始時にgenerationを`RunController.rearm_token`へ同期。
   - `_managed_session`は既に受け取っている`controller`をRuntimeへ渡し、Trace sinkをPipelineのruntime log / event journalへ接続する。
3. 新規 `tests/test_runtime_resource_lifecycle.py`、変更 `tests/test_session_composition.py`
4. 文書: 新規 `doc/architecture/runtime_resource_lifecycle.md`（契約・状態遷移・Adapterの書き方）、`doc/architecture/README.md`の索引、`doc/versions.md`、`generated/repo_map.json`（`tools/generate_docs.py`）

## 入出力

### 型

```text
ResourceScope = SESSION | GENERATION | TASK | PROCESS
ResourceState = ACTIVE | RELEASING | RELEASED | FAILED

ResourceLease(frozen):
  resource_id: str            # ^[A-Za-z0-9_.:-]{1,128}$。Session内のACTIVE/RELEASINGで一意
  kind: str                   # ^[a-z][a-z0-9_.]{0,63}$ 例 "frame.shared", "gpu.model", "capture.wgc"
  owner_scope: ResourceScope
  owner_id: str               # session id / "gen-<n>" / task id（Registryが決める）
  generation: int             # 取得時のcurrent generation（全scopeで記録）
  estimated_bytes: int | None # 0以上。None=unknown（0扱いで成功にしない）
  created_monotonic_ns: int
  expires_monotonic_ns: int | None
  pinned: bool
  evictable: bool
  priority: int               # 0..1000、既定500。小さいほど先にevict
  provenance: str             # 0..128文字。取得箇所の固定ラベル（例 "pipeline.ocr_cache"）
```

- `releaser_ref`はleaseの値に含めない。Registry内部の`_Entry`が`ResourceReleaser`への参照を持ち、snapshot・Trace・Journalへは出さない（pickle/serializeしない）。
- `ResourceReleaser`（Protocol）: `release(resource_id: str) -> None`。失敗は例外で通知。Registryは**1回のacquire（内部entry）につき**最大1回だけ呼ぶ。releaserは有限時間で戻ること（owner側の責務。Registryは同期呼出しを中断できない）。
- 内部entryは単調増加の`lease_seq`を持つ。終了（RELEASED/FAILED）後は同じ`resource_id`を新しいentryとして再取得できる。`release(resource_id)`は現在ACTIVE/RELEASINGのentryだけを対象にし、終了済みの旧entryへは作用しない（終了履歴は`lease_seq`で区別）。
- `ClosableOwnerAdapter(owner)`: `release`で`owner.close()`、無ければ`owner.shutdown()`を呼ぶ（`_close_provider`と同じ規約）。どちらも無ければ構築時に`TypeError`。
- owner側Adapter（#571 `release(ref)`、#534 `evict(model_key)`、#334相当のclose）は各Issueのowner APIを1行で呼ぶ`ResourceReleaser`として各Issue側に置く。本Issueはfake ownerでこの3形状を試験する。

### SessionResourceRegistry

```text
SessionResourceRegistry(session_id: str, *, clock_ns=time.monotonic_ns,
    trace_sink: ResourceTraceSink | None = None, initial_generation: int = 0,
    max_active: int = 4096, terminal_history: int = 256)
    # max_active: 1..65536（ACTIVE+RELEASINGの件数）、terminal_history: 0..4096。範囲外はValueError

acquire(resource_id, kind, releaser, *, owner_scope, estimated_bytes=None,
        ttl_ms=None, pinned=False, evictable=False, priority=500,
        provenance="", generation=None) -> ResourceLease
borrow(resource_id, *, expected_generation: int) -> ResourceBorrow   # context manager
release(resource_id) -> ResourceState | None
task_scope(task_id: str) -> ContextManager[None]
close_task(task_id) -> CleanupReport
advance_generation(generation: int) -> CleanupReport
close_generation(generation: int) -> CleanupReport
close_session(*, budget_ms: float = 2000.0) -> CleanupReport
snapshot() -> ResourceLifecycleSnapshot
current_generation: int（property）
```

- `acquire`の`generation`省略時はcurrent。指定値がcurrentと違えば`stale_generation`。`owner_id`はscopeから決める（SESSION=session_id、GENERATION=`gen-<n>`、TASK=現在の`task_scope`のtask id、無ければ`no_active_task`）。
- `ttl_ms`は1以上の有限値。`expires = created + ttl`。`pinned=True`と`ttl_ms`/`evictable=True`の同時指定は`invalid_lease`（矛盾する宣言を受け付けない）。
- `acquire`は「owner自身が資源を作った後、consumerへ渡す前」に呼ぶ。例外時はownerが自分で解放する（未登録の資源を残さない責任はownerにある。docstringと文書に明記）。
- `release`: ACTIVE→解放してRELEASED/FAILEDを返す。RELEASING/RELEASED/FAILED中の再呼出しは何もせず現在状態を返し`double_release_total`を数える。未知（または履歴から押し出された）IDは`None`。例外を送出しない（冪等）。
- `ResourceBorrow`: `with registry.borrow(id, expected_generation=g) as lease:`。ACTIVE以外は`not_active`、GENERATION/TASK scopeで`lease.generation != g`は`stale_generation`（`stale_rejected_total`を数える）。SESSION scopeはgenerationを跨いで借用できる。

### ResourceLifecycleError.code（固定語彙）

`registry_closed`、`generation_closed`、`stale_generation`、`duplicate_resource_id`、`not_active`、`invalid_lease`、`no_active_task`、`scope_not_supported`、`capacity_exceeded`

### CleanupCoordinator

```text
CleanupCoordinator(registry, *, clock_ns=time.monotonic_ns)
on_pressure(snapshot: ResourcePressureSnapshot) -> CleanupReport
cleanup_expired(*, max_items: int = 64, budget_ms: float = 20.0) -> CleanupReport

ResourcePressureSnapshot(frozen):           # 消費側の最小契約 v1
  schema_version: int = 1
  level: "none" | "elevated" | "critical"
  reclaim_target_bytes: int | None          # None=件数上限まで
  kinds: frozenset[str] | None              # None=全kind
  observed_monotonic_ns: int
  source: str                               # 例 "budget.admission"（#546）
```

- 構築時検証: 未知version・未知level・負値・非int・非有限は`ValueError`（fail-closed。誤ったsnapshotで解放しない）。`cleanup_expired`の`max_items`（1..1024）・`budget_ms`（有限・正）も範囲外は`ValueError`。
- levelごとの上限: `none`=`reclaim_target_bytes`に関係なく何もしない、`elevated`=最大8件/10ms、`critical`=最大32件/50ms。
- `reclaim_target_bytes=None`は件数・時間上限まで解放する。指定時は解放済み推定bytesが目標以上になった時点で止める（目標を超える追加解放はしない）。候補が尽きても目標未達なら`target_met=False`を返す（`truncated`は上限で止めた場合だけ）。

### CleanupReport / Snapshot（Trace・#787の入力）

```text
CleanupReport(trigger, attempted, released, failed, deferred, skipped_pinned,
              skipped_in_use, freed_estimated_bytes, unknown_bytes_count,
              skipped_not_evictable, latency_ms, truncated: bool, target_met: bool | None)
  trigger: release | task | generation | session | pressure | expired

ResourceLifecycleSnapshot(schema_version=1, session_id, session_state: OPEN|CLOSING|CLOSED,
  current_generation, active_count, releasing_count, pinned_count,
  active_by_kind: {kind: {count, known_bytes, unknown_count}},
  counters: {acquired, released, evicted, expired, release_failed,
             stale_rejected, double_release, deferred, trace_failed},
  last_cleanup: CleanupReport | None, max_cleanup_latency_ms,
  recent_failures: [{resource_id, kind, error_type}] (最大16件))
```

- Trace event（`ResourceTraceSink.record(event, payload)`）: `resource.cleanup/v1`（CleanupReportの値。trigger=releaseの単発解放は出さず件数だけ数える）、`resource.release_failed/v1`（resource_id、kind、owner_scope、generation、error_type）。例外messageはpathや利用者データを含み得るため出さない。payloadはID・種別・量・状態・時間・例外型名だけ。
- sink自体の例外は捕捉して`trace_failed`件数に数え、cleanupを止めない（Traceの失敗で資源を残さない）。

## 手順（責務境界・状態遷移・失敗時の挙動）

### 状態遷移

```text
acquire ─> ACTIVE ──release/scope close/evict/expire──> RELEASING ──releaser成功──> RELEASED
                                                       └─releaser例外──> FAILED
ACTIVE（借用中）──解放要求──> RELEASING(deferred) ──最後のborrow終了──> releaser呼出し
```

- RELEASINGは内部で`pending`（解放待ち。打ち切りで残ったもの）・`running`（releaser実行中）・`deferred`（借用終了待ち）を区別する。releaserを呼ぶ権利はlock内で`pending→running`へ変えた1者だけが得る。再度の`close_session`・pressure・`release`は`pending`だけを拾い、`running`/`deferred`には触れない。
- RELEASING以降へ進んだleaseは借用できず、releaserは二度と呼ばれない（二重解放の防止）。FAILEDは自動再試行しない（二重解放を避ける）。失敗はTraceと`recent_failures`に残し、`close_session`の結果として呼出元へ返す。
- 状態変更は1つの`threading.Lock`内で行い、releaserは**lockの外**で呼ぶ。releaserが別の資源を`release`しても自己deadlockしない。RELEASINGへの遷移をlock内で先に済ませるので、並行した2つの解放要求でもreleaserは1回だけ。
- RELEASED/FAILEDになった資源は活動中の表から外し、件数と直近256件の終了記録（ID・kind・状態）だけを残す。Registryのmetadataは「活動中の数＋固定長履歴」で上限があり、10k回の取得/解放でも増え続けない。

### scope別の一括解放

1. `task_scope(task_id)`: 入るとcontextvarに現在taskを設定し、`finally`で`close_task(task_id)`。正常終了・例外・`ExecutionCancelled`・timeout例外は全て同じ`finally`を通る。同じtask idの入れ子は`invalid_lease`。
2. `advance_generation(g)`: `g > current`でなければ`stale_generation`。1回のlock内で「currentを`g`へ更新」と「`generation < g`のGENERATION/TASK資源を全て`pending`へ変更」を同時に行い、lockを出てから解放する。このためlockを出た時点で旧世代資源は借用不能、以後の`acquire`は新世代にだけ入り、旧世代のcleanupが新世代資源を閉じることはない。SESSION scopeは残す。
3. `close_generation(g)`: その世代のGENERATION/TASK scopeを解放し、世代をclosedにする。以後その世代への`acquire`は`generation_closed`（`advance_generation`で次へ進むまでcurrentへのacquireも拒否）。
4. `close_session()`: 状態をCLOSINGにし、以後の`acquire`/`borrow`は`registry_closed`。TASK → GENERATION（新しい世代から） → SESSIONの順、同じ群の中は取得の逆順（後から取った依存資源を先に解放）で解放する。全件が終われば CLOSED。
5. 解放順序はclock・hash順に依存せず、(scope群, 取得seq)だけで決まる（決定的）。
6. `pinned`はpressure・期限切れによるevictionだけを禁止する。所有scopeの終了（task/generation/session close）ではpinnedでも解放する（scopeより長生きさせると暗黙のglobal化になるため）。

### pressure / 期限切れ（CleanupCoordinator）

1. 評価対象はACTIVEで`kinds`に一致する資源（`kinds=None`は全件）。そのうちpinnedは`skipped_pinned`、`evictable=False`は`skipped_not_evictable`、借用中は`skipped_in_use`に数え、残りだけを解放候補とする。走査は活動中の表だけ（終了履歴は見ない）で、pressureはhot pathではない。PROCESS scopeは存在しない（下記）。
2. 順序（v1、利用価値の推定はしない）: 期限切れ → `priority`の小さい順 → `created_monotonic_ns`の古い順 → `resource_id`。
3. 上限: levelの件数・時間上限、または解放済み推定bytes ≥ `reclaim_target_bytes`で止める。unknown bytesの資源は0bytesとして数え、件数上限の対象。時間はreleaser呼出しの間でだけ判定する（同期呼出しを途中で中断できないため）。上限で止めた場合は`truncated=True`。
4. 評価対象のうち候補外は1.の区分で数えるだけで解放しない。
5. 期限切れは`now >= expires_monotonic_ns`。`cleanup_expired`は期限切れかつ借用数0の資源を、evictableに関係なく解放する（TTLはownerが宣言した寿命のため。pinnedは期限を持てない）。借用中は`skipped_in_use`に数え、次回に回す。期限は`expires_monotonic_ns`を最小heapで持ち、解放済みの項目は取り出し時に捨てる。heapが活動中件数の2倍＋64を超えたら作り直す（全履歴を走査しない）。

### fail-closed

- 引数・宣言の不正、閉じたscopeへの取得、stale generationの借用は全て例外で拒否し、資源をconsumerへ渡さない。
- 1件のreleaser例外は他の資源の解放を止めない。`close_session`は時間上限（既定2000ms）で打ち切り、未処理分はRELEASING待ちのまま残して`truncated=True`を返す。Registryのclose結果に関係なく、既存のadaptation → 実行資源 → provider → journalのcloseは**中断せず続けて**実行する（Registryの打ち切りで後続の解放を止めない）。`ManagedSessionRuntime.close()`は`failed`・`deferred`・`truncated`のいずれかがあれば既存の`errors`に加えて最後に`RuntimeError`にし、`GameSessionController`は既存どおり`FAILED / runtime shutdown failed`にする。再度`close()`すると残りを続きから処理する（既存の再試行可能なclose契約と同じ）。
- Python GC/finalizerは解放契約に使わない（`__del__`・`weakref.finalize`を使わない）。close後に残ったactive件数はsnapshotで見える。
- `PROCESS` scopeはSession Registryでは`scope_not_supported`で拒否する。Sessionを跨ぐ資源（例: #534のwarm model）はowner側のprocess寿命で管理し、暗黙にSession Registryへglobal登録しない（親#785の「明示的に昇格し、暗黙にglobal化しない」）。process単位のRegistryは残件。
- `max_active`（既定4096）を超える`acquire`は`capacity_exceeded`（metadataの上限）。

### 並行性

- 全公開メソッドはthread-safe（1 lock）。lock内ではdict/heap/カウンタ操作だけを行い、releaser・Trace sinkはlock外。
- `close_session`とpressure解放が並行しても、RELEASINGへの遷移がlock内なので各資源のreleaserは1回だけ。
- 借用中の資源への解放要求はRELEASING(deferred)にし、最後の`ResourceBorrow.__exit__`のthreadでreleaserを呼ぶ。`close_session`の時点で借用が残る場合は`deferred`件数として返す（Stop後もconsumerが資源を握っている異常として表に出す）。
- `GameSessionController`はstep実行中のstopではcloseを延期するため、通常のStopでは借用0でcloseされる。

### session_compositionへの接続

- `ManagedSessionRuntime(pipeline, provider, adaptation=None, *, resources=None, run_control=None)`: `resources`省略時は`SessionResourceRegistry(uuid4().hex)`を内部で作る（既存の呼出し・テストはそのまま動く）。
- `run()/run_and_execute()`: 開始時に`run_control.rearm_token`がcurrentより大きければ`advance_generation`。その後`resources.task_scope(f"step-{n}")`内で既存処理を行う。
- `close()`: 先頭で`resources.close_session()`を呼び、続けて既存のadaptation → 実行資源 → provider → journalの順。Registryの失敗は既存の`errors`へ加える。journalより前なのでTraceはjournalへ書ける。
- Trace sink: `pipeline.runtime_log`があれば`write(event, message, payload)`、無ければ`pipeline.event_journal.append("runtime.log", ...)`（`record_shutdown_failure`と同じ書き方）。どちらも無ければsink無し。

### #787（長時間安定性Gate）との接続点

- #787は`ResourceLifecycleSnapshot`（active/failed件数、kind別推定bytes、counters、`max_cleanup_latency_ms`）と`resource.cleanup/v1`をmetricの入力に使う。測定・判定・閾値は#787の範囲で、本Issueはsnapshotの値とschema_versionを固定するだけ。
- fake ownerで「1 lease/step解放しない」状態は`active_count`の単調増加として見える（#787の必須負例の入力）。

## テスト（受入条件と1対1）

全てネットワーク・実Windows APIを使わず、偽clock（ns）とfake owner（呼出し順を記録）で決定的に行う。`tests/test_runtime_resource_lifecycle.py`。

| # | Issueのテスト項目 / 完了条件 | 試験 |
|---|---|---|
| T1 | normal release | acquire→release→RELEASED、releaser 1回、snapshotのactive 0、counters.released 1 |
| T2 | double release | 2回目のreleaseは状態を返しreleaser追加呼出し無し、`double_release` 1。並行2threadのreleaseでもreleaser 1回 |
| T3 | task cancel / timeout / exception | `task_scope`内で`ExecutionCancelled`・`TimeoutError`・`ValueError`を送出→いずれもtask資源がRELEASED、例外はそのまま伝播。scope外でTASK取得は`no_active_task` |
| T4 | generation rollover | gen0のGENERATION/TASK資源とSESSION資源を取得→`advance_generation(1)`でgen0分だけ解放、SESSIONは残る。`advance_generation(1)`再呼出しは`stale_generation` |
| T5 | Stop / session close | `close_session`でTASK→GENERATION→SESSION、群内は逆順の解放順をfake ownerの記録で確認。以後のacquire/borrowは`registry_closed`。`ManagedSessionRuntime.close()`がjournal close前にregistryを閉じる（`test_session_composition.py`）。`GameSessionController.stop()`経由でも同じ |
| T6 | pinned resource | pressure criticalでpinned資源は解放されず`skipped_pinned`に数える。scope終了（task/generation/session close）ではpinnedでも解放される。pinned＋ttl/evictableは`invalid_lease` |
| T7 | pressure eviction（bounded） | 期限切れ・priority・作成時刻の異なる10件で解放順が「期限切れ→priority→古い順→id」。elevatedで8件到達は`truncated`、target bytes到達で停止し`target_met=True`、候補不足は`target_met=False`。pinned・非evictable・借用中はそれぞれのskip件数。level noneはtarget指定でも何もしない。不正snapshot（version 2、NaN、負bytes）は`ValueError` |
| T8 | release failure | 3件中2件目のreleaserが例外→FAILED、`resource.release_failed/v1` 1件（例外messageを含まない）、他2件はRELEASED、FAILEDは再試行されない。releaserが時間を消費する偽clockで`close_session(budget_ms)`が打ち切り`truncated=True`、再呼出しで`pending`だけを解放し、実行中（`running`）のentryを別threadの同時closeが二重に呼ばない。Registryが打ち切っても`ManagedSessionRuntime.close()`の後続（provider・journal）は閉じられる |
| T9 | stale lease rejection | gen1でGENERATION資源を取得→`borrow(expected_generation=0)`は`stale_generation`。`advance_generation`と別threadの`acquire`/`borrow`を並行させ、旧世代資源が借用されず新世代資源が解放されない。解放済みの借用は`not_active`。close済み世代への取得は`generation_closed` |
| T10 | 10k acquire/releaseで非有界増加しない | 同じresource_idの再取得を含む10,000回の取得/解放後、活動中の表0件、終了履歴≤256、kind別集計の空kindが消えている、期限heapが上限内 |
| T11 | fake ownerでcleanup順序が決定的 | 同じ操作列を2回実行し、releaserの呼出し順とCleanupReportが一致 |
| T12 | 借用中の解放 | borrow中のreleaseはRELEASING(deferred)、borrow終了でreleaser 1回。close時に借用が残れば`deferred`件数、`ManagedSessionRuntime.close()`は`RuntimeError` |
| T13 | Adapter（#571/#534/#334形状） | `release(ref)`型・`evict(key)`型・`close()`型のfake ownerをAdapter経由で登録し、各scope closeで正しいowner APIが1回呼ばれる。`ClosableOwnerAdapter`はclose優先・shutdown代替・どちらも無しで`TypeError` |
| T14 | metric/Trace | `resource.cleanup/v1`のpayloadが件数・推定bytes・unknown件数・latency（偽clock）を持ち、releaser参照や任意objectを含まない（JSON化できる）。sinkの例外でcleanupが止まらない |
| T15 | hot pathで全履歴scanしない | `snapshot()`・`release()`が終了履歴・期限heapを走査しないことを、履歴を満杯にした状態で活動中1件の操作回数（kind集計の更新回数）で確認 |
| T16 | 既存互換 | `ManagedSessionRuntime(pipeline, provider)`の既存テストが変更なしで通る。generationは`rearm_token`へ同期 |

## Issue完了条件との対応

| 完了条件 | 設計箇所 | 試験 |
|---|---|---|
| typed lease/snapshot contractがある | 入出力の型・`ResourceLifecycleSnapshot` schema_version 1 | T1, T14 |
| Session/Generation/Task scopeで一括cleanupできる | scope別の一括解放 | T3, T4, T5 |
| Stop/error/cancelからcleanup pathへ接続される | `task_scope`、`ManagedSessionRuntime.close()`先頭、既存`GameSessionController`経路 | T3, T5 |
| #425/#546 pressureでbounded eviction要求を出せる | `ResourcePressureSnapshot` v1と`on_pressure`（発生源の接続は残件） | T7 |
| #571/#534/#334のowner APIをAdapter接続可能 | `ResourceReleaser`、`ClosableOwnerAdapter` | T13 |
| stale/double-release/release-failureの負例 | 状態遷移・fail-closed | T2, T8, T9 |
| cleanup件数・推定bytes・latencyをmetric/Traceへ | `CleanupReport`、`resource.cleanup/v1` | T14 |
| hot pathで全履歴scanしない | 活動中の表＋固定長履歴＋kind集計の差分更新＋期限heap | T10, T15 |

Issueの実装契約1〜9: 1→T1/T2、2→T4/T5/T9、3→T3、4→T6/T7、5→T7/T11、6→T8、7→T9/T12、8→「releaser参照・payloadを複製しない」（T14でJSON化可能なID/量だけを確認）、9→fail-closedの「GC/finalizerを使わない」。

## 設計レビュー（packet-01/02、SHA256 705DC50D… / 85D72B1B…）

Codex Luna（2資料とも返答）の指摘を全て原文と照合し、以下を反映した。他の者は省略（evidence.md）。
採用: generation更新と旧世代のpending化を同じlock内で行う（並行acquire/borrowとの競合）、RELEASINGの`pending/running/deferred`区別とreleaser実行権の一意取得（打ち切り後の再開と二重解放）、Registryの打ち切り後も既存close列を継続、pressureの`none`はtargetを無視・目標超過の追加解放なし・`target_met`、評価対象と解放候補の区別（`skipped_not_evictable`を追加）、ID再利用は内部`lease_seq`で区別・容量引数の範囲、期限境界`now >= expires`と`cleanup_expired`引数検証、`trace_failed`カウンタ、`release_failed`から例外messageを除外。
確認のみ: 借用中の解放（`deferred`）は資料2の並行性節に既にある。PROCESS scopeは拒否と明記済み。

## 代替案と不採用理由

- **`contextlib.ExitStack`を共有するだけ**: LIFO解放はできるが、資源単位の状態・pressure時の部分解放・stale検査・件数が持てない。
- **`weakref.finalize`で漏れを自動回収**: 解放時期がGC依存で非決定的。Issue契約9に反する。
- **releaserを別threadでtimeout付き実行**: 同期releaserを外から止められず、timeout後も実行が続く資源の状態が不定になる。v1は時間上限を呼出しの間で判定し、遅いowner自身の修正を促す（`max_cleanup_latency_ms`で見える）。
- **FAILEDの自動再試行**: owner側で部分的に解放済みの場合に二重解放になる。再試行はowner Adapterの判断に任せる。
- **#425/#546のsnapshot完成を待つ**: Registry・scope cleanup・負例は発生源と独立に作れる。消費側の最小型を先に決めれば、後から発生源を接続しても本Issueの型は変わらない。

## 未決事項と決定（技術的に決まるため自分で決めた）

1. pressure入力の型: 本Issueで`ResourcePressureSnapshot` v1（level・target bytes・kind・時刻・source）を**消費側の最小契約**として定める。#425/#546は自分のsnapshotからこの型への純粋な写像を持つ。根拠: #546本文が「#786へResourcePressure/Admission snapshotを提供し、cleanup policy自体を重複実装しない」と定めており、方針はRegistry側、量の判断は#546側という分担と一致する。levelの上限（8件/10ms、32件/50ms）はstep周期（既定1000ms）を圧迫しない値として決め、定数として文書化する。
2. generationの正本: `RunController.rearm_token`。根拠: 既に「再開前に開始した推論結果を破棄する」世代境界として使われており、別の番号を作ると二重の世代が食い違う。
3. PROCESS scope: Session Registryでは拒否。根拠は親Issueの「暗黙にglobal化しない」。process単位Registryは必要になった時点で別Issue。
4. 1呼出し内で`finally`解放済みの資源（GDI、planner作業directory/process）は登録しない。根拠: 寿命がtaskより短く、登録すると同じ資源に2つの解放経路ができる。
5. `close_session`の既定時間上限2000ms: Stop後のUI応答を長く止めない値。打ち切りは失敗として返し、再closeで続行できる。

## 範囲外・残件

- pressure発生源の接続（#425/#546が`ResourcePressureSnapshot`を作り`on_pressure`を呼ぶ）。
- 実owner（#571 Shared Buffer、#534 GPU cache）のAdapter実装と登録（各Issue側）。GDIの解放順序（#334）。
- 長時間の増加検出・閾値・report（#787）。実機30分run（#250）。
- process単位のRegistry、Experience永続StorageのGC（#18）、allocator自作、OS全体のmemory manager。
- `light_adaptation_budget.LightResourceSample`の置換（意味が違うため触らない）。

## 完了条件

- 上表T1〜T16が通り、`finish_task.bat`が全段passする。
- 既存の`tests/test_session_composition.py`・`tests/test_game_session.py`が変更なしで通る（追加試験を除く）。
- 文書（architecture・versions・repo_map）を同じPRで更新し、Issue #786へ実装PRと残件（pressure発生源接続、実owner Adapter）をコメントする。
