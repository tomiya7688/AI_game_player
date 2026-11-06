Spec-Status: READY
Depends-On: #164, #167, #789（PR #821 は main 7bd8500 にマージ済み。設計時に参照した 351d4c4 と main の `src/` 差分なし）
Decision-Gaps: none
Parent: #18

# Issue #790 Orphan Reconciliation（crash由来の未索引Artifactの安全な回収）

## 利用者の操作

利用者の操作はありません。本Issueはライブラリ API（`OrphanReconciler`・`reclaim_budget`）だけを追加し、アプリの起動・終了・定期処理からの呼出し（runtime 配線）は後続 Issue（#225 の Privacy lifecycle）で行います。配線後も自動保守として動き、画面操作は追加しません。開発者は `reconcile(trigger, dry_run=True)` で削除予定を確認できます。

## 現状調査

- `artifact_store.py`（#167）: `put`は`shard/.pending-*`へ書いてから`os.rename`で公開。killされると`.pending-*`が残る（metadata・payloadが無い場合もある）。`find_orphans`/`_iter_digests`はsymlinkのshard・entryを**黙って飛ばす**（junctionは見ない）。metadataの`created_at`はput時刻。objectは不変（既存digestへの`put`は書き換えない）。
- `retention_collector.py`（#164、#789で拡張）: `measure()`は未索引objectも容量に数え、1件でもmetadataが壊れていれば`RetentionIndexError`。`collect`と`discard_unindexed_object`は`LocalRetentionFileSystem.remove_object`（`shutil.rmtree`）で消す。rmtree途中で停止すると半壊objectがshardに残り、以後`measure`（＝全admit・collect）が止まる。#789の補償と`recover_pending`も`discard_unindexed_object`経由なので同じ問題を持つ。
- `retention_planner.plan_retention`はsoft budgetから未索引byteを差し引く（93行）。orphanだけで予算超過していると、`collect`は**正常な索引entryを代わりに消す**。
- `retention_root_lock.py`/`artifact_admission.py`（#789）: root単位のOS lock（同一thread再入可、既定5秒で期限切れ）。lock取得時に root と `.retention.lock` を作る。pending recordはput直前に書かれ、lockを取れた時点で既存recordは全て中断済み。`inspect_pending`/`recover_pending`が`orphan_created`等に分類・回収する。`created_object=False`のobjectは#789では消さず#790に委ねる。`require_plain_directory`がlink・junctionを拒否する。
- `data_inventory.py`: `class_for_artifact_type`でartifact_type→data classを引ける（#225のaudit用）。
- live referenceの自動収集・runtimeからの`collect`呼出しは未実装（retention_core.md）。

## 成果物

- 新規 `src/ai_game_player/artifact_trash.py`: `ArtifactTrash`（crash安全な削除。intent record・rename・掃除・ack・残存byte）。
- 新規 `src/ai_game_player/orphan_reconciler.py`: `OrphanReconciler`、状態・計画・報告・audit型、`LiveReferenceSource`、`reclaim_budget`。
- `retention_collector.py`（最小）:
  - `LocalRetentionFileSystem.remove_object(path)`を「事前検査（現行どおり）→`ArtifactTrash.discard`（intent→rename→rmtree）」に変える。署名・事前検査・失敗注入点（`remove_object`の差替え）は不変なので、`collect`・`discard_unindexed_object`・#789の補償/`recover_pending`が全てcrash安全になる。
  - `_measure_usage`が`ArtifactTrash.residual_bytes()`を加える（`StoreUsage.trash_bytes`を追加、`total_bytes`に含める）。
  - `_safe_object_path`を公開名`safe_object_path`でも呼べるようにする（挙動不変）。
- 新規 `tests/test_artifact_trash.py`、`tests/test_orphan_reconciler.py`。
- 文書: `doc/architecture/retention_core.md`にorphan回収とtrashの節、`doc/versions.md`、`generated/repo_map.json`。`data_inventory.json`の`writers`へ`artifact_trash:ArtifactTrash`（`data_class: null`、auditはpayloadを持たない旨）を追加（AST走査testが要求する場合）。

## 入出力

### 状態（`OrphanState`、str定数）

| 状態 | 条件 | 削除 |
|---|---|---|
| `active_pending` | いずれかのpending recordが同digestを参照 | しない |
| `live_referenced` | live snapshotに含まれる | しない |
| `recent_unindexed` | 最終活動時刻からgrace未満、または時刻が未来（時計ずれ） | しない |
| `safe_orphan` | 索引・pending・liveに無く、grace経過、整合性検査に合格 | **する** |
| `corrupt_or_unknown` | metadata破損・path/link異常・余分なfile・snapshot不完全・検証未実施 | しない（diagnostic） |

索引に載ったdigestは候補にしない。判定順は上表の上から（索引→pending→live→整合性→grace）。

### ArtifactTrash

- 置き場 `root/.orphan-trash/`。作成時と**毎回の使用前**に`require_plain_directory`で実directory（symlink・junctionでない、root直下）であることを確認し、違反は`RetentionRootUnsafeError`で何もしない。2桁hexでないので`_iter_digests`の対象外。
- intent record `<id>.intent.json`（`id`=32桁hex、16KiB上限、`stage_json_write`でfsync）: `schema_version`、`id`、`kind`（`object`/`staging`）、`sha256`（stagingはnull）、`source`（`<shard>/<digest>`または`<shard>/.pending-*`）、`size_bytes`（rename前にstatで合計）、`artifact_type`・`sensitive`・`data_class`（objectのみ、不明はnull）、`reason`（`safe_orphan`/`stale_staging`/`retention_remove`）、`trigger`（null可）、`state`（`pending`/`renamed`/`swept`）、`deleted_at_ms`（`renamed`以降）。trash entry名は`<id>`。
- `discard(source_dir, intent)`（root lock内で呼ぶ）: intent(`pending`)を書く → `os.rename(source_dir, trash/<id>)` → shardとtrashをfsync → intentを`renamed`＋`deleted_at_ms`へ原子更新（ここが論理削除の完了点）→ entryを`remove_object`相当の検査付き`rmtree` → intentを`swept`。rename失敗はintentを消して例外。rmtree失敗は例外にせず残存（次回`sweep`）。
- `sweep(*, deadline, max_entries)`（root lock内）: `pending`でentry無し→intent削除（renameしていない。元は無傷）。`pending`でentry有り→`renamed`へ更新。`renamed`でentry有り→entryが実directoryで中身が`payload`/`metadata.json`の通常fileの部分集合、objectで`metadata.json`が残っていればsha256一致、のときだけrmtreeして`swept`。intentの無いentry・検査不合格・link・file→消さずdiagnostic（名前だけでは消さない）。
- `swept`の扱い: `reason=retention_remove`のintentは`swept`後に削除。`safe_orphan`/`stale_staging`のintentは**呼出元がauditを永続化して`acknowledge(ids)`するまで残す**（audit再生成の正本）。
- `residual_bytes()`: `pending`・`renamed`のintentの`size_bytes`合計（entryが残る可能性のある分）。`measure`がこれを使用量に含めるので、trash残存中もhard limitを守る。intentが不正なら`RetentionIndexError`（fail-closed、`measure`と同じ扱い）。
- 上限: intent 10,000件。超えたら新規`discard`を`RetentionCapacityError`で拒否（未ackのauditが溜まり続けるのを防ぐ）。

### 型

- `LiveReferenceSnapshot(source: str, references: tuple[ArtifactReference, ...], complete: bool)`。`LiveReferenceSource.snapshot() -> LiveReferenceSnapshot`（Protocol）。Journal/Experience/Replay等の保持者が実装する。
- `OrphanReconcilerConfig`: `grace_ms=86_400_000`（24h、60秒〜30日）、`future_skew_ms=300_000`、`max_candidates=10_000`、`max_verify_bytes_per_run=1 GiB`（1件目は上限を超えても検証する）、`max_delete_count=1_000`（object・stagingの合計）、`max_lock_hold_ms=1_000`、`live_snapshot_timeout_ms=2_000`、`periodic_min_interval_ms=600_000`。範囲外は`ValueError`。
- `ReconcileTrigger`: `startup`/`session_close`/`budget`/`explicit`/`periodic`。
- `OrphanCandidate(sha256, state, reason, size_bytes, artifact_type|None, sensitive|None, last_activity_ms|None, fingerprint)`。`fingerprint`はobject dir・metadata・payloadの`(st_ino（Windowsはfile index）, size, mtime_ns, ctime_ns)`。
- `OrphanReconcilePlan(state_version, evaluated_at_ms, trigger, candidates, targets, stale_staging, trash_residual_bytes, scan_start, scan_next, blocked_reason|None, truncated)`。`targets`は`safe_orphan`と削除可能なstale stagingで、合計`max_delete_count`件まで。
- `OrphanDeleteAudit(audit_id, sha256|None, kind, artifact_type, data_class|None, sensitive, size_bytes, reason, trigger, deleted_at_ms, recovered: bool)`。intentから作る（`audit_id`=intent id）。`to_dict()`はJSON化可能でpath・payloadを含まない（#225がそのまま保存できる）。data classは注入可能なresolver（既定は`load_data_inventory().artifact_types`）で引き、未登録は`None`。
- `OrphanReconcileReport(plan_version, applied_version, recovered_pending: AdmissionRecoveryReport|None, deleted, deleted_bytes, skipped: tuple[(id, reason)], corrupt, audits, trash_swept, trash_residual_bytes, blocked_reason|None, stopped_by_lock_budget)`。

### API

`OrphanReconciler(collector, admission, live_sources, *, config=..., clock=collector.clock, data_class_resolver=None, declared_no_live_sources=False)`

- `plan(trigger, *, extra_live=()) -> OrphanReconcilePlan`: dry-run。objects・索引・pending record・trash・scan cursorを変更しない。rootが無ければlockを取らず空の計画を返す。root lock取得に伴う`.retention.lock`の作成（#789の協調file、Store dataではない）だけは許す。
- `apply(plan, *, recover_pending=True, extra_live=()) -> OrphanReconcileReport`: 変更はすべてここで行う。`deleted`は常に`plan.targets`の部分集合で、件数は`max_delete_count`以下（再検査で外れた分を補充しない）。
- `reconcile(trigger, *, dry_run=False, extra_live=())`: `plan`→（dry_runでなければ）`apply`。
- `pending_audits() -> tuple[OrphanDeleteAudit, ...]`／`acknowledge(audit_ids)`: 未ackのaudit（crash後の分も`recovered=True`で）を取得し、永続化後にackする。呼出元は`audit_id`で重複を除く。
- `reclaim_budget(reconciler, *, extra_live=()) -> BudgetReclaimReport`: 予算超過時の順序制御（後述）。

`live_sources`が空なら`declared_no_live_sources=True`を明示しない限り`ValueError`（参照者がいないことを黙って仮定しない）。`extra_live`は呼出時に**1回だけ**`tuple`へ実体化し、live集合へ合流させる（generatorの2回目が空になる問題を防ぐ）。

### エラー

| 状況 | 結果 | 変更 |
|---|---|---|
| lock期限切れ・root/trashのlink | `RetentionLockTimeoutError`/`RetentionRootUnsafeError`を送出 | なし |
| 索引不正・pending record不正・trash intent不正・live sourceが例外/`complete=False`/期限超過 | `blocked_reason`付き（targets空）。applyも削除0 | planはなし。applyは手順(1)(2)の変更（報告に記載）だけ |
| apply時に`now < plan.evaluated_at_ms - future_skew_ms`（時計の巻き戻し） | `blocked_reason="clock_rollback"`、削除0 | 手順(1)(2)のみ |
| 個別候補の検査失敗 | その候補を`corrupt_or_unknown`、他は継続 | なし |
| rename失敗 | `skipped`に理由、他の対象は継続。索引は書き換えない | 失敗した対象は不変 |
| `max_lock_hold_ms`超過 | 残りは次回、`stopped_by_lock_budget=True` | 済んだ分のみ |
| `periodic`が`periodic_min_interval_ms`以内 | `blocked_reason="rate_limited"` | なし |

## 手順

### 計画（plan）

1. **lock外**: 全live sourceの`snapshot()`を順に取り、`extra_live`と合わせて1つのdigest集合にする。各sourceの所要時間を測り、例外・`complete=False`・合計が`live_snapshot_timeout_ms`超過なら不完全（削除0）。lockを持たないので、遅いsourceがadmitを待たせない。
2. **lock内（短時間、読取のみ）**: (a) trash intentを読み`trash_residual_bytes`を求める（掃除しない）。(b) 索引（`collector.load_entries`）と`admission.inspect_pending()`を読む。(c) scan cursor（`.orphan-trash/scan-cursor.json`、無ければ先頭）を読み、shardを自前でdigest順に走査し、cursorの次から末尾→先頭へ1周するまで索引外digestを`max_candidates`件まで候補にする（symlink/junctionのshard・entryも**飛ばさず**`corrupt_or_unknown`として列挙）。最後に見たdigestを`scan_next`とする。(d) 各候補の軽量検査: shard・object dirが通常dir、名前=digest・先頭2文字=shard、中身が`payload`と`metadata.json`の通常fileだけ、metadataが解析でき`sha256`一致、payloadのstat sizeがmetadataと一致。(e) 状態を仮決定し`fingerprint`を採る。(f) `.pending-*` staging dirを列挙。lock解放。
3. **lock外**: 仮`safe_orphan`のpayloadをcursor順に64KiB単位で逐次SHA-256し、digestと一致しなければ`corrupt_or_unknown`。検証量が`max_verify_bytes_per_run`を超えたら以降は`corrupt_or_unknown(reason="verification_deferred")`（1件目は大きくても検証するので、cursorと合わせて全objectがいずれ検証される）。objectは不変なので、lock外の検証はfingerprint不変を条件に有効。
4. graceを判定し`targets`を決め、`state_version`を計算する。`state_version`=正規化JSONのSHA-256（索引file bytesのhash、pending (id,digest)昇順、live digest昇順とsource名、候補 (digest,state,size,fingerprint) 昇順、config）。評価時刻は含めない。

最終活動時刻 = `max(metadata.created_at, object dir/metadata/payloadのmtime)`（最も新しいもの＝最も若い年齢で保守的に判定）。`now - 最終活動 < grace` または `最終活動 > now + future_skew_ms` は`recent_unindexed`。

stale staging（`.pending-*`）は、2桁hex shard直下の通常dir（link/junctionでない）で、中身が`payload`/`metadata.json`の通常fileの部分集合（空でもよい）だけ、dir・fileの最新mtimeがgrace経過、のすべてを満たすものだけを`targets`に入れる。1つでも外れれば保持して`corrupt`へ記録。

### 適用（apply）

1. lock外: live snapshotを取り直す（計画の手順1と同じ規則。不完全なら削除0）。
2. lock内（開始から`max_lock_hold_ms`で打切り。各操作の間で時刻を確認）:
   1. trashの`sweep`（期限内で）。
   2. `recover_pending=True`なら`admission.recover_pending()`（#789のcrash leftover回収。lockを取れた時点で全recordは中断済み。削除は`remove_object`経由なのでtrash経由になる）。結果は`recovered_pending`。失敗は`blocked_reason`で削除0、既に回収された分は#789の契約どおり確定。
   3. 索引・pendingを読み直し、現在の`state_version`を計算（payloadの再hashはfingerprint不変なら省略）。
   4. 一致なら`plan.targets`を、不一致なら`plan.targets`の各項目を個別に再検査してまだ削除可能なものだけを対象にする（**計画外は決して追加しない**）。いずれの場合も各対象で、索引・pending・live集合に無いこと、fingerprint一致、**現在時刻でgrace経過・未来時刻でないこと**を再確認する（時計が戻っても計画時の判定を流用しない）。外れたものは`skipped`に`indexed`/`pending`/`live`/`changed_since_plan`/`within_grace`等の理由。
   5. 各対象: `collector.safe_object_path(digest)`（stagingは同等のshard直下検査）で配置を確認し、audit項目を入れたintentで`ArtifactTrash.discard`。
   6. `scan_next`を`scan-cursor.json`へ保存。
3. lock外: `pending_audits()`をreportの`audits`に入れて返す（今回分と、crash前に`renamed`になっていた未ack分を含む）。

### crash・再試行（idempotent）

- intent書込み前に停止: 何も変わらない。
- intent(`pending`)後・rename前: 次回`sweep`がentry無しを確認してintentを消す。元のobjectはshardに無傷で残り再評価される。
- rename後・`renamed`更新前: 次回`sweep`がentry有りを確認して`renamed`へ進める（audit欠落なし）。
- `renamed`後・rmtree途中: 次回`sweep`が検査してrmtree。shardに半壊objectは残らない（`measure`を止めない）。
- 未ackのauditはintentから毎回再生成される。ack後に停止しても再送は`audit_id`で除ける。

### 予算超過時の順序（reclaim_budget）

1. live集合を1回だけ作る（`reconciler`のsnapshot＋`extra_live`を実体化）。
2. 外側でlockを取らずに`reconciler.reconcile(BUDGET, extra_live=その集合)`を呼ぶ（hash検証はlock外のまま）。ただし事前の`measure()`がsoft budget以内、または未索引byteが0なら省略。
3. lock内で: `measure()`を取り直し、同じ`entries`・`policy`・`now_ms`・live集合で`plan_retention(..., unindexed_bytes=実際)`と`plan_retention(..., unindexed_bytes=0)`を作る。実際の計画が0の計画に無いentryを消すなら（＝orphan分の上乗せで正常dataを消す）、`collect`せず`blocked_reason="orphan_pressure"`と残りの未索引byteを報告。そうでなければ同じlock内で`collect(その集合)`。直接の`collect`の挙動（#164）は変えない。

### 並行性

admit・register・collect・recover・plan(手順2)・apply(手順2)は同じroot lockで直列化（#789の契約）。lock外の間にadmissionが走っても、applyで索引・pending・liveを取り直し、fingerprintを照合するので、新しく登録・pendingになったobjectは消えない。lock保持は`max_lock_hold_ms`で有界で、payloadのhashとlive snapshotはlock外なので、admitの5秒期限を妨げない。objectは不変が契約で、fingerprintにfile id・ctimeを含めるので差替え（rename置換・再作成）は検出できる。既存objectの中身をその場で同size・同mtimeへ書き戻す改変、`Store.put`を直接呼ぶ（admission外の）書込み、snapshot後に未索引objectへ新たに張られた参照は契約外（#789と同じ）で、grace（24h）とapply直前のlive再取得で緩和する。各live sourceは自分の参照集合を1時点で返す責務を持つ（source間の同時性は要求しない）。

### fail-closed方針

不明は消さない: 索引/pending/trash intent/live不完全→全体削除0、候補個別の異常→その候補を残しdiagnostic、時刻が未来・時計の巻き戻し→保持、計画外は削除しない、所有者推定（「今回の失敗物」）で消さない（判定は状態だけ）、削除失敗で索引を書き換えない、trashがlinkなら何もしない、名前だけでtrash entryを消さない。

## 実装で確定した点（設計との差分）

実装中に次を確定し、上の本文より優先します。

- `TrashIntent`に`acknowledged`を追加。`acknowledge`は、掃除前のintentは確認済みフラグだけ立て（掃除を妨げない）、`swept`済みなら削除します。`residual_bytes()`は「`swept`以外かつtrash entryが実在する」intentの`size_bytes`合計です（`pending`でも未renameなら元objectが未索引として数えられるので二重計上しません）。
- trash sweepの内容検査（`payload`/`metadata.json`の通常fileだけ・sha256一致）は`safe_orphan`/`stale_staging`のintentに適用します。`retention_remove`は従来の`rmtree`と同じ安全性（実directoryであること）のみ要求します。
- `OrphanCandidate`は`location`（`shard/名前`）と`kind`（`object`/`staging`）を持ちます。link・junctionのshardは`location=shard`の`corrupt_or_unknown`として列挙します。
- 計画が`blocked_reason`付きなら`apply`は何も変更しません（sweep・recover_pendingも行わない）。`apply`時の停止（live不完全・巻き戻し・recover失敗）は、手順(1)(2)のsweepとrecover_pendingだけ実行した後に削除0で止まります。
- `recover_pending`で記録が外れた`created_object=False`のobjectは、同じ`reconcile`の計画には`active_pending`として入っているため、**次回の`reconcile`**でgrace後に回収します。
- `state_version`は索引・pending・live・設定と`targets`（位置+fingerprint）のhashです。`applied_version`は適用時に`plan.targets`を再検査した結果で同じ式から求めるので、対象が不変なら一致します。
- live sourceは`complete`と`references`（tuple）を持つ値を返せばよく、#788の`LiveReferenceProvider`（`RetentionReferenceSnapshot`）をそのまま渡せます。
- `reclaim_budget`は`RetentionCollector.collect`を呼ぶため、#788の不変条件テスト「本番コードで`.collect(`を呼ぶのはadapterだけ」の許可リストへ`orphan_reconciler.py`を追加しました。`complete=True`のsnapshotが得られない場合は`collect`しません。runtime配線（adapter経由でGCする）は後続です。
- 走査cursorは`.orphan-trash/scan-cursor.json`（`{"schema_version":1,"last":"shard/名前"}`）です。壊れていれば先頭から走査します。

## 代替案と不採用理由

- `discard_unindexed_object`/`remove_object`のrmtree直を残す: 途中停止で半壊objectが残りadmitを止め、再試行がidempotentでない。`remove_object`の中身をtrash経由に変え、全経路をcrash安全にする。
- trash byteを計測しない: trash残存中に計測上の空きが増えhard limitを超え得る。intentの`size_bytes`で数える。
- 全工程をlock内: 大きいpayloadのhashや遅いlive sourceでlockが数秒以上になり、admitが期限切れになる。
- `state_version`不一致なら常に削除0: 稼働中は索引が頻繁に変わり、回収が進まない。計画の部分集合だけ許す。
- pending recordを経過時間で失効: Issueが禁止。lockで中断を確定し`recover_pending`に委ねる。
- 予算時にplannerのorphan差引きを変える: #164の挙動変更になる。入口`reclaim_budget`で順序を制御する。
- auditを戻り値だけで返す: 呼出元が保存する前に停止すると失われる。intentを正本にしack制にする。

## 範囲外・残件

- runtimeへの接続（startup/session close/periodicの呼出し元）とJournal/Experience/Replayの`LiveReferenceSource`実装、auditの永続化先: retentionのruntime配線が未実装のため後続Issue（#225のPrivacy lifecycleから呼ぶ）。
- `corrupt_or_unknown`の隔離（quarantine）: 壊れた未索引objectは引き続き`measure`を止める。自動削除しない方針のため、運用診断と手動対応手段を後続Issueで扱う。
- value-aware compaction、delete/export UI（#225）、remote storage。

## テスト

`tests/test_orphan_reconciler.py`・`tests/test_artifact_trash.py`（実FS、決定論的clock、mtimeは`os.utime`で設定。link作成権限が無い環境はskip）。

| # | 受入条件/Issueの試験 | 試験 |
|---|---|---|
| T1 | 状態分類・SAFEだけ削除 | 5状態の候補を1つずつ置き、applyで`safe_orphan`だけ消える |
| T2 | pending保持 | pending recordが指す未索引object（古いmtime）は`active_pending`で残る（`recover_pending=False`） |
| T3 | live保持 | live sourceまたは`extra_live`（generatorで渡す）が参照するobjectは残る。sourceが例外/`complete=False`/期限超過なら全体削除0 |
| T4 | grace保持 | grace未満・未来時刻は残る。plan後に時計を戻すと`clock_rollback`またはtargetが`within_grace`で削除0 |
| T5 | crash leftover | `put`後・register前に中断（pending record有）→ `reconcile`で回収（`recovered_pending`に記録）。`created_object=False`のrecord回収後のobjectもgrace後に回収。中身の有無が異なる`.pending-*`（空・payloadのみ・両方）もgrace後に回収、余分なfile・linkのstagingは残る |
| T6 | 索引追加後除外 | planとapplyの間にadmitで登録 → `skipped=indexed`で残る |
| T7 | corrupt不削除 | metadata破損・digest不一致・余分なfile・payload size不一致・shard/object/payloadのsymlink・junction → 残り`corrupt`に出る |
| T8 | idempotent・audit | intent後・rename後・`renamed`更新後・rmtree途中の各停止を差替えFSで発生 → 再実行でtrashが空になり、auditは`pending_audits`に1件ずつ（`recovered=True`）、ackで消え、索引変更なし。intentの無いtrash entry・digest不一致entryは消さない。`recover_pending`/`collect`の削除途中停止でもshardに半壊objectが残らない |
| T9 | 予算で正常data保護 | orphanだけで超過 → `reclaim_budget`が索引entryを消さずorphanを回収。回収不能orphanだけ残る場合は`orphan_pressure`で`collect`しない。orphan 0・live参照entryありで`orphan_pressure`にならない（両計画に同じlive集合） |
| T10 | dry-run/apply一致 | 不変ならtargetsとdeletedが一致。変化時はdeletedが計画の部分集合。`plan`の前後でobjects・索引・pending・trash・cursorのfile一覧とbytesが不変。rootが無ければ何も作らない |
| T11 | report/audit | `deleted_bytes`・skipped理由・corrupt一覧・auditのdata class/bytes/ref/理由、`to_dict`にpathが無い |
| T12 | 並行の負例 | 別threadのadmit・collectと同時にreconcile・`reclaim_budget`しても、登録済み・pendingのobjectが消えず、hard limitを超えない。大きいpayloadの検証中・遅いlive source中もadmitがlock期限内に通る |
| T13 | 境界 | config範囲外、空`live_sources`、`periodic`の間隔制限、`max_delete_count`（再検査で外れても補充しない、stagingを含む）・`max_lock_hold_ms`打切り・`max_candidates`とcursorの1周で後方のorphanに到達・verify上限より大きい1件目の検証、fingerprint変化（同size再作成）でskipped、intent上限で新規discard拒否 |
| T14 | trash安全 | `.orphan-trash`がsymlink・junction・fileなら何も書かず`RetentionRootUnsafeError`。trash残存byteが`measure().usage.total_bytes`に含まれ、残存中のadmitがhard limitを超えない |
| T15 | 既存不変 | `test_retention_core`・`test_artifact_store`・`test_artifact_admission`・`test_data_inventory`が変更なしで通る |

## 完了条件（Issueとの対応）

| Issue完了条件 | 設計箇所 | 試験 |
|---|---|---|
| orphanを状態分類しSAFE_ORPHANだけ削除 | 状態表・apply | T1 |
| pending/live/graceを保護 | 判定順・fail-closed | T2,T3,T4 |
| #789 crash leftoverを回収 | apply手順2-2・stale staging・ArtifactTrash | T5,T8 |
| orphan bytesを実際にreclaim | apply・reclaim_budget・cursor | T1,T5,T9,T13 |
| corrupt/unknownを自動削除しない | 軽量検査・hash検証 | T7 |
| dry-run/report/delete audit | plan・Report・Audit・ack | T8,T10,T11 |
| #225のorphan cleanup acceptance | `reconcile`をlifecycleから呼べる、auditに理由・data class・bytes・ref、ack | T8,T11 |
| concurrent admission/GC/reconcileの負例 | 並行性・apply再検査・lock保持上限 | T6,T12,T14 |
