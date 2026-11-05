Spec-Status: BLOCKED
Depends-On: #164, #167, #789（PR #821 HEAD 351d4c4 のAPIを前提。マージ後に実装開始。API変更時は本書を先に直す）
Decision-Gaps: none
Parent: #18

# Issue #790 Orphan Reconciliation（crash由来の未索引Artifactの安全な回収）

## 現状調査

- `artifact_store.py`（#167）: `put`は`shard/.pending-*`へ書いてから`os.rename`で公開。killされると`.pending-*`が残る。`find_orphans`/`_iter_digests`はsymlinkのshard・entryを**黙って飛ばす**（junctionは見ない）。metadataの`created_at`はput時刻。
- `retention_collector.py`（#164、PR #821で拡張）: `measure()`は未索引objectも容量に数え、1件でもmetadataが壊れていれば`RetentionIndexError`。`discard_unindexed_object`はlock内で索引を再確認して`shutil.rmtree`する。rmtree途中で停止すると半壊objectが残り、以後`measure`（＝全admit）が止まる。
- `retention_planner.plan_retention`はsoft budgetから未索引byteを差し引く（93行）。orphanだけで予算超過していると、`collect`は**正常な索引entryを代わりに消す**。
- `retention_root_lock.py`/`artifact_admission.py`（#789、PR #821）: root単位のOS lock（再入可、既定5秒で期限切れ）。pending recordはput直前に書かれ、lockを取れた時点で既存recordは全て中断済み。`inspect_pending`/`recover_pending`が`orphan_created`等に分類・回収する。`created_object=False`のobjectは#789では消さず#790に委ねる。
- `data_inventory.py`: `class_for_artifact_type`でartifact_type→data classを引ける（#225のaudit用）。
- live referenceの自動収集・runtimeからの`collect`呼出しは未実装（retention_core.md）。

## 成果物

- 新規 `src/ai_game_player/orphan_reconciler.py`: `OrphanReconciler`、状態・計画・報告・audit型、`LiveReferenceSource`、`reclaim_budget`。
- `retention_collector.py`（最小）: `_safe_object_path`を公開名`safe_object_path`でも呼べるようにする（挙動不変）。
- 新規 `tests/test_orphan_reconciler.py`。
- 文書: `doc/architecture/retention_core.md`にorphan回収節、`doc/versions.md`、`generated/repo_map.json`。AST走査で新しい永続化箇所と判定された場合だけ`data_inventory.json`の`writers`へ`data_class: null`で追加。

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

### 型

- `LiveReferenceSnapshot(source: str, references: tuple[ArtifactReference, ...], complete: bool)`。`LiveReferenceSource.snapshot() -> LiveReferenceSnapshot`（Protocol）。Journal/Experience/Replay等の保持者が実装する。
- `OrphanReconcilerConfig`: `grace_ms=86_400_000`（24h、60秒〜30日）、`future_skew_ms=300_000`、`max_candidates=10_000`、`max_verify_bytes_per_run=1 GiB`、`max_delete_count=1_000`、`periodic_min_interval_ms=600_000`。範囲外は`ValueError`。
- `ReconcileTrigger`: `startup`/`session_close`/`budget`/`explicit`/`periodic`。
- `OrphanCandidate(sha256, state, reason, size_bytes, artifact_type|None, sensitive|None, last_activity_ms|None, fingerprint)`。`fingerprint`はobject dir・metadata・payloadの`(st_ino（Windowsはfile index）, size, mtime_ns, ctime_ns)`。
- `OrphanReconcilePlan(state_version, evaluated_at_ms, trigger, candidates, targets, stale_staging, trash_residue, blocked_reason|None, truncated)`。`targets`は`safe_orphan`のdigest昇順で`max_delete_count`件まで。
- `OrphanDeleteAudit(sha256, artifact_type, data_class|None, sensitive, size_bytes, reason="safe_orphan", trigger, deleted_at_ms)`。`to_dict()`はJSON化可能でpath・payloadを含まない（#225がそのまま保存できる）。data classは注入可能なresolver（既定は`load_data_inventory().artifact_types`）で引き、未登録は`None`。
- `OrphanReconcileReport(plan_version, applied_version, recovered_pending: AdmissionRecoveryReport|None, deleted, deleted_bytes, skipped: tuple[(digest, reason)], corrupt, audits, staging_removed, trash_swept, trash_residual_bytes, blocked_reason|None)`。

### API

`OrphanReconciler(collector, admission, live_sources, *, config=..., clock=collector.clock, file_system=None, data_class_resolver=None, declared_no_live_sources=False)`

- `plan(trigger) -> OrphanReconcilePlan`: dry-run。**Store・pending record・trashを一切変更しない**（読取専用）。
- `apply(plan, *, recover_pending=True) -> OrphanReconcileReport`: 変更はすべてここで行う（trash掃除→`recover_pending`→削除）。`deleted`は常に`plan.targets`の部分集合で、件数は`max_delete_count`以下（再検査で外れた分を補充しない）。
- `reconcile(trigger, *, dry_run=False)`: `plan`→（dry_runでなければ）`apply`の便宜関数。dry_runは`plan`のみ。
- `reclaim_budget(collector, reconciler, live_references) -> BudgetReclaimReport`: 予算超過時の順序制御（後述）。

`live_sources`が空なら`declared_no_live_sources=True`を明示しない限り`ValueError`（参照者がいないことを黙って仮定しない）。

### エラー

| 状況 | 結果 | Store変更 |
|---|---|---|
| lock期限切れ・root link | `RetentionLockTimeoutError`/`RetentionRootUnsafeError`を送出 | なし |
| 索引不正・pending record不正・live sourceが例外または`complete=False` | `blocked_reason`付きの計画（targets空）。applyも削除0 | planはなし。applyは手順の(1)(2)で行った変更（報告に記載）だけ |
| 個別候補の検査失敗 | その候補を`corrupt_or_unknown`、他は継続 | なし |
| rename/削除失敗 | `skipped`に理由、他の対象は継続。索引は書き換えない | 失敗した対象は不変 |
| `periodic`が`periodic_min_interval_ms`以内 | `blocked_reason="rate_limited"` | なし |

## 手順

### 計画（plan）

1. **lock内（短時間、読取のみ）**: (a) trashの残存を列挙（掃除しない）。(b) `recover_pending`は行わない（applyの手順(2)）。(c) 索引を読む（`collector.load_entries`）、`admission.inspect_pending()`、全live sourceの`snapshot()`を取る。(d) shardを自前で走査し（symlink/junctionのshard・entryも**飛ばさず**`corrupt_or_unknown`として列挙）、索引外digestを`max_candidates`件まで候補にする（超えたら`truncated=True`）。(e) 各候補の軽量検査: shard・object dirが通常dir、名前=digest・先頭2文字=shard、中身が`payload`と`metadata.json`の通常fileだけ、metadataが解析でき`sha256`一致、payloadのstat sizeがmetadataと一致。(f) 状態を仮決定し`fingerprint`を採る。(g) `.pending-*` staging dirを列挙。lock解放。
2. **lock外**: 仮`safe_orphan`のpayloadを64KiB単位で逐次SHA-256し、digestと一致しなければ`corrupt_or_unknown`。合計が`max_verify_bytes_per_run`を超える分は`corrupt_or_unknown(reason="verification_deferred")`。objectは不変なので、lock外の検証はfingerprint不変を条件に有効（lock保持を短くし、admitの5秒期限を妨げない）。
3. graceを判定し`targets`を決め、`state_version`を計算する。`state_version`=正規化JSONのSHA-256（索引file bytesのhash、pending (id,digest)昇順、live digest昇順とsource名、候補 (digest,state,size,fingerprint) 昇順、config）。評価時刻は含めない。

最終活動時刻 = `max(metadata.created_at, object dir/metadata/payloadのmtime)`（最も新しいもの＝最も若い年齢で保守的に判定）。`now - 最終活動 < grace` または `最終活動 > now + future_skew_ms` は`recent_unindexed`。

### 適用（apply）

lock内で: (1) trash掃除（後述）。掃除できない残存があれば以後の新規移動はせず`blocked_reason="trash_residue"`。(2) `recover_pending=True`なら`admission.recover_pending()`（#789のcrash leftover回収。lockを取れた時点で全recordは中断済み。結果は`recovered_pending`。失敗は`blocked_reason`で削除0、既に回収された分は#789の契約どおり確定）。(3) 索引・pending・liveを取り直す（liveが不完全なら削除0）→ 現在の`state_version`を計算（payloadの再hashはfingerprint不変なら省略）。

- 一致: `plan.targets`をそのまま対象にする。
- 不一致: `plan.targets`の各digestを個別に再検査し、まだ`safe_orphan`のものだけ対象（**計画外のdigestは決して追加しない**）。外れたものは`skipped`に`indexed`/`pending`/`live`/`changed_since_plan`/`within_grace`等の理由。

各対象: `collector.safe_object_path(digest)`で配置を再確認し、fingerprintが計画時と一致することを確認 → trash意図record `root/.orphan-trash/<name>.intent.json`（digest・size・artifact_type・trigger、`stage_json_write`でfsync）を書く → object dirを`root/.orphan-trash/<name>`（`<name>=<digest>.<16hex>`）へ`os.rename`（同一volume、原子的）→ shardとtrashをfsync → この時点で論理削除完了としてauditを確定（rename失敗ならintentを消してskipped）→ trash entryを`rmtree`し、最後にintentを消す。

stale staging（`.pending-*`）: 2桁hex shard直下の通常dir（link/junctionでない）で、中身が`payload`/`metadata.json`の通常fileの部分集合だけ、dir・fileの最新mtimeがgrace経過、のすべてを満たすものだけ同じ手順で消す（lock保持中なのでadmission中のstagingは存在しない）。1つでも外れれば保持して`corrupt`へ記録。

### crash・再試行（idempotent）

- rename前に停止: objectは無傷で次回再評価。
- intent書込み後・rename前に停止: intentだけ残る。次回の掃除で「同名entryが無いintent」は削除する（objectはshardに無傷で残り再評価される）。
- rename後・rmtree途中: trashに残る。次回のapply手順(1)で、**対応するintentがあり**、entryが通常dir（link/junctionでない）、中身が`payload`/`metadata.json`の部分集合、`metadata.json`が残っていればそのsha256とintentのdigestが一致、のときだけ`rmtree`する。intentの無いentry・名前不一致・link・file → 消さずdiagnostic（名前の形だけでは削除しない）。半壊objectがshard内に残ることはない（`measure`を止めない）。
- trashは`_iter_digests`の対象外なので、掃除前のtrash byteは`measure`に数えない。ディスク総量はreconcilerで増えないが、計測上の使用量だけ減る。この差を有界にするため、残存があるapplyでは新規移動をしない（差は直前1回のapplyの`deleted_bytes`以下）。残存byteは`trash_residual_bytes`で毎回報告する。

### 予算超過時の順序（reclaim_budget）

lock内で: (1) `measure()`でsoft budget超過かつ未索引byte>0なら`reconcile(BUDGET)`を先に実行。(2) 索引だけで`plan_retention(entries, unindexed_bytes=0)`と実際の`plan`を比較し、実際の計画が**orphan分の上乗せで追加のentryを消す**なら`collect`しない（`blocked_reason="orphan_pressure"`と残りorphan byteを報告）。(3) そうでなければ`collect(live_references)`。直接の`collect`の挙動（#164）は変えない。

### 並行性

admit・register・collect・recover・plan(手順1)・applyは同じroot lockで直列化（#789の契約）。lock外の手順2の間にadmissionが走っても、applyで索引・pending・liveを取り直し、fingerprintを照合するので、新しく登録・pendingになったobjectは消えない。objectは不変（`put`は既存digestを書き換えない）が契約で、fingerprintにfile id・ctimeを含めるので差替え（rename置換・再作成）は検出できる。既存objectの中身をその場で同size・同mtimeへ書き戻す改変、`Store.put`を直接呼ぶ（admission外の）書込み、snapshot後に未索引objectへ新たに張られた参照は契約外（#789と同じ）で、graceとapply時のlive再取得で緩和する。live sourceはlock内で順に取得し、各sourceは自分の参照集合を1時点で返す責務を持つ（source間の同時性は要求しない。未索引objectを新たに参照する経路が契約外のため）。`reclaim_budget`は外側でroot lockを取り、`measure`/`apply`/`collect`は同一threadの再入で同じlockを使う。lock期限切れは例外で返し、予算回収は何も変更しない。

### fail-closed方針

不明は消さない: 索引/pending/live不完全→全体削除0、候補個別の異常→その候補を残しdiagnostic、時刻が未来→保持、計画外は削除しない、所有者推定（「今回の失敗物」）で消さない（判定は状態だけ）、削除失敗で索引を書き換えない。

## 代替案と不採用理由

- `discard_unindexed_object`（rmtree直）を使う: 途中停止で半壊objectが残りadmitを止め、再試行がidempotentでない。trash rename方式にする。
- 全工程をlock内: 大きいpayloadのhashでlockが数秒以上になり、admitが期限切れになる。
- `state_version`不一致なら常に削除0: 稼働中は索引が頻繁に変わり、回収が進まない。計画の部分集合だけ許す。
- pending recordを経過時間で失効: Issueが禁止。lockで中断を確定し`recover_pending`に委ねる。
- 予算時にplannerのorphan差引きを変える: #164の挙動変更になる。入口`reclaim_budget`で順序を制御する。

## 範囲外・残件

- runtimeへの接続（startup/session close/periodicの呼出し元）とJournal/Experience/Replayの`LiveReferenceSource`実装: retentionのruntime配線が未実装のため後続Issue（#225のPrivacy lifecycleから呼ぶ）。
- `corrupt_or_unknown`の隔離（quarantine）: 壊れた未索引objectは引き続き`measure`を止める。自動削除しない方針のため、運用診断と手動対応手段を後続Issueで扱う。
- `collect`のrmtree途中停止で生じる半壊object（#164側）。value-aware compaction、delete/export UI（#225）、remote storage。

## テスト

`tests/test_orphan_reconciler.py`（実FS、決定論的clock、mtimeは`os.utime`で設定。link作成権限が無い環境はskip）。

| # | 受入条件/Issueの試験 | 試験 |
|---|---|---|
| T1 | 状態分類・SAFEだけ削除 | 5状態の候補を1つずつ置き、applyで`safe_orphan`だけ消える |
| T2 | pending保持 | pending recordが指す未索引object（古いmtime）は`active_pending`で残る（`recover_pending=False`） |
| T3 | live保持 | live sourceが参照するobjectは残る。sourceが例外/`complete=False`なら全体削除0 |
| T4 | grace保持 | grace未満・未来時刻は残る |
| T5 | crash leftover | `put`後・register前に中断（pending record有）→ `reconcile`で回収（`recovered_pending`に記録）。`created_object=False`のrecord回収後のobjectもgrace後に回収。残った`.pending-*`もgrace後に回収 |
| T6 | 索引追加後除外 | planとapplyの間にadmitで登録 → `skipped=indexed`で残る |
| T7 | corrupt不削除 | metadata破損・digest不一致・余分なfile・payload size不一致・shard/object/payloadのsymlink・junction → 残り`corrupt`に出る |
| T8 | idempotent | intent後・rename後・rmtree途中の各停止を差替えFSで発生 → 再実行でtrashが空になり、二重audit・索引変更なし。intentの無いtrash entry・digest不一致entryは消さない。trash掃除失敗時は新規移動0で`trash_residual_bytes`が出る |
| T9 | 予算で正常data保護 | orphanだけで超過 → `reclaim_budget`が索引entryを消さずorphanを回収。回収不能orphanだけ残る場合は`orphan_pressure`で`collect`しない |
| T10 | dry-run/apply一致 | 不変ならtargetsとdeletedが一致。変化時はdeletedが計画の部分集合。`plan`の前後でStore・pending・trashのfile一覧とbytesが不変 |
| T11 | report/audit | `deleted_bytes`・skipped理由・corrupt一覧・auditのdata class/bytes/ref/理由、`to_dict`にpathが無い |
| T12 | 並行の負例 | 別threadのadmit・collectと同時にreconcileしても、登録済み・pendingのobjectが消えず、hard limitを超えない |
| T13 | 境界 | config範囲外、空`live_sources`、`periodic`の間隔制限、`max_delete_count`（再検査で外れても補充しない）・`max_candidates`・verify byte上限、fingerprint変化（同size再作成）でskipped |
| T14 | 既存不変 | `test_retention_core`・`test_artifact_store`・`test_artifact_admission`・`test_data_inventory`が通る |

## 完了条件（Issueとの対応）

| Issue完了条件 | 設計箇所 | 試験 |
|---|---|---|
| orphanを状態分類しSAFE_ORPHANだけ削除 | 状態表・apply | T1 |
| pending/live/graceを保護 | 判定順・fail-closed | T2,T3,T4 |
| #789 crash leftoverを回収 | apply手順(2)・stale staging | T5,T8 |
| orphan bytesを実際にreclaim | apply・reclaim_budget | T1,T5,T9 |
| corrupt/unknownを自動削除しない | 軽量検査・hash検証 | T7 |
| dry-run/report/delete audit | plan・Report・Audit | T10,T11 |
| #225のorphan cleanup acceptance | `reconcile`をlifecycleから呼べる、auditに理由・data class・bytes・ref | T11 |
| concurrent admission/GC/reconcileの負例 | 並行性・apply再検査 | T6,T12 |
