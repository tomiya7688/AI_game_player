# Retention Core（budget / Protected / TTL / exact dedup）

## 背景と目的

Experienceの重いpayload（frame・video segment・model output等）は[Content-addressed Artifact Store](artifact_store.md)へSHA-256参照で保存されます。Storeは保存と整合性検査だけを担い、削除しないため、長時間運用では容量が無制限に増えます。Issue #164（親 #18）は、高度なvalue-aware GC（#165）の前に、容量予算・削除禁止・期限・完全重複除去だけで安全な基本retentionを成立させます。

本資料の読者は、Experience/Artifactの保存・削除を扱う実装者と、将来のRust Core（#661）で同じ契約を再実装する作業者です。この変更による通常利用者の操作変更はありません。

## 構成

| モジュール | 責務 | 副作用 |
|---|---|---|
| `retention_index.py` | 保持記録`RetentionEntry`と索引JSONの厳格な復元・直列化 | なし |
| `retention_policy.py` | `RetentionPolicy`（soft budget・hard limit・TTL・件数上限） | なし |
| `retention_planner.py` | `plan_retention`：記録・policy・時刻・外部参照から削除計画を決める純粋関数 | なし |
| `retention_collector.py` | `RetentionCollector`：Storeへの登録・受入判定・GC適用 | 索引置換とobject削除（差替可能なFS経由） |

時計（`clock`: UNIXエポックmsを返す関数）とFS操作（`RetentionFileSystem`: 索引の読込・原子的置換・object削除）は注入できます。計画は時刻と入力だけで決まり、入力順に依存しません。

## データ契約（言語非依存）

- 索引: `<Store root>/retention-index.json`。形は[`config/retention_index.schema.json`](../../config/retention_index.schema.json)。
- policy: [`config/retention_policy.schema.json`](../../config/retention_policy.schema.json)。
- 時刻はタイムゾーン解釈の差が出ないよう、整数のUNIXエポックms（UTC）です。`expires_at_ms: null`は無期限です。
- `entry_id`は一意、同じ`artifact_sha256`を指す記録は同じ`size_bytes`を持つ必要があります。保存時は`entry_id`順に並べ、出力を決定的にします。
- 上限: 索引32MiB、記録100,000件（policyの`max_entries`はこれ以下）。超過・未知キー・版不一致・型不正は部分結果なしで拒否します（fail-closed）。

## 使用量の数え方

使用byteは、索引記録のdigestごとに1回だけ数えたpayload byte（content-addressedなので同一内容は1 object）と、索引にない既存object（orphan）のmetadata上のbyteの合計です。orphanは容量に数えますが、Retention Coreは削除しません（`find_orphans`と同じく報告だけ）。各digestのmetadataは16KiB上限で読み、欠落・破損・索引とのsize不一致があれば計測自体を拒否します。

## 削除順序（正本）

`plan_retention`は次の順で判断します。

1. **exact duplicate**: 同じdigestを指す記録をまとめます。Protected記録があればProtectedを全て残し、非Protectedを外します。なければ最も長く保持される1件（無期限 → 期限が遅い順 → 作成が古い順 → `entry_id`順）を残します。期限内の重複記録があるのに、古い記録のTTLで内容が失われることを防ぐためです。外した記録には`alias_of`（同じ内容を持ち続ける記録ID）を付けます。objectは代表が持つので削除されません。
2. **TTL**: `expires_at_ms <= now`の非Protected記録を期限順・`entry_id`順に外します。外部参照中のdigestは削除せず`expired_but_referenced`としてblockedに記録します。TTLは容量に関係なく働きます。
3. **budget**: 使用量がsoft budgetを超える場合だけ、非Protectedかつ外部非参照の記録を作成が古い順（同時刻は`entry_id`順）に外し、soft budget以下になったら止めます。
4. **物理削除対象**: 外した記録のdigestのうち、残存記録にも外部参照にも含まれないものだけです。

Protected記録はどの段階でも外しません。Protectedと外部参照だけで予算を超える場合は`over_budget`/`over_hard_limit`を立てて報告し、削除で解決しようとしません。

## hard limitとGC trigger

- `can_admit(size_bytes, sha256=None)`: Storeへ書く前に、書込み後もhard limit以内かを判定します。既存digestなら追加容量0です。呼出側は`put`の前に呼びます。
- `register(reference, entry_id=..., protected=False)`: put済みArtifactを索引へ追加します。put済みobjectは既に使用量に含まれるため、合計がhard limitを超えていれば`RetentionCapacityError`で拒否します。件数上限・ID重複も拒否します。TTL期限はpolicy（型別TTL優先、なければ既定TTL）で登録時刻から決めます。
- `plan(live_references)`: `collect`が適用するのと同じ計画を副作用なしで返します。`needs_collection(live_references)`は、その計画に外す記録が1件以上あればTrueです。Protectedや参照中の記録だけで予算を超えている状態はGCで解決しないためFalseで、`plan().over_budget`/`over_hard_limit`で検出します。
- `set_protected(entry_id, protected)`: 既存記録のProtectedを切り替えます。

## 参照整合性とcrash安全性

- `collect(live_references)`へ、Experience/Event Journal等が現在参照している`ArtifactReference`を渡します。参照中のdigestはTTL・budgetのどちらでも削除しません。
- 実行順は「計画 → 全削除先の安全検査 → 索引の原子的置換 → object削除」です。削除途中で停止しても、索引から外れたobjectがorphanとして残るだけで、索引や外部参照が存在しないobjectを指す状態は作りません。
- 削除先は、Store配置（`root/先頭2文字/digest`）と実体pathが一致し、root・shard・object directoryのいずれもsymlinkでない場合だけ許可します。Windowsのdirectory junctionもsymlinkと同様に拒否します（Python 3.12以降は`os.path.isjunction`、3.10/3.11はreparse point属性で判定）。1件でも外れれば`RetentionSafetyError`で、索引を変更せず何も削除しません。`LocalRetentionFileSystem.remove_object`は削除直前にも同じ検査を繰り返しますが、検査と削除の間の差替え（TOCTOU）を完全には排除できないため、管理rootは同じユーザーの信頼できるprocessだけが書く前提です。
- 索引にあるのにobjectやmetadataが欠落・破損している場合、計測段階で`RetentionIndexError`となり、GCは停止したままです。自動修復はせず、Reader/Recovery（#168）または手動確認で索引を直します。
- 削除は`.orphan-trash/`経由（intent→rename→rmtree、[Orphan Reconciliation](#orphan-reconciliationissue-790)）なので、途中で停止してもshardに半壊objectは残りません。crashで取り残された未索引object（orphan）は`OrphanReconciler`が回収します。trashに残るbyteも使用量（`StoreUsage.trash_bytes`）に含めます。
- 同じrootへの`register`/`set_protected`/`collect`は、root直下の`.retention.lock`（OS advisory lock＋process内mutex、同一threadの再入可、`lock_timeout_seconds`で有界待ち）の中で実行されます（Issue #789）。`plan`/`can_admit`/`needs_collection`は助言用の読取でlockを取りません。`FileSystemArtifactStore.put`を直接呼ぶ書込みはlockの対象外です。

## 非対象と残り

- near-duplicate、prioritized replay、clustering/prototype、value-aware compactionは #165。
- orphan回収のruntime配線（startup・session close・periodicからの呼出し、auditの永続化）は#225のPrivacy lifecycleで行います。Experience/Event Journalからの`live_references`収集と、どのSessionがいつ`collect`を呼ぶかは、Issue #788の[Runtime接続](#runtime-retention-adapterissue-788)で実装しました。

## Artifact Admission Transaction（Issue #789）

`ArtifactAdmissionCoordinator.admit(request, writer)`が`can_admit → put → register`を1つのroot lock内の transactionにします。詳細は[設計](../design/issue-789-artifact-admission.md)。

1. root lock取得（期限超過は`RetentionLockTimeoutError`でtransaction未開始）。
2. 索引とStore使用量を再計測（破損はfail-closed）。entry_id重複・件数上限・hard limitを**putの前**に判定。既存digest（`expected_sha256`が索引または未索引objectにある）は追加容量0。
3. writerは`AdmissionPutPort.put(bytes)`だけを使えます（Storeやpathは渡さない。1回限り、サイズ・digest・shard directoryを検査）。putの直前に`root/.admission-pending/<id>.json`を書きます。
4. 検証後に`register`。失敗時は、このadmissionが新規作成したobject（`created_object`）だけを、索引・他のpending recordが参照しない場合に削除。既存digest・索引登録済みobjectは消しません。補償できない場合はpending recordを残し`ArtifactAdmissionError(pending_retained=True)`を送出します。
5. `inspect_pending()`が`registered`/`indexed_by_other_entry`/`no_object`/`preexisting`/`orphan_created`に分類し、`recover_pending()`が安全に確認できる範囲で回収します（#790の入力）。
6. `AdmissionTraceEvent`/`stats`でlock待ち・transaction時間・rollback数を観測できます。

## Runtime Retention Adapter（Issue #788）

`runtime/retention_adapter.py`の`RuntimeRetentionAdapter`が、本番Sessionから`RetentionCollector.collect`を呼ぶ唯一の経路です。削除の判断は`plan_retention`のままで、adapterは「いつ・どの参照集合で・どれだけ」GCするかだけを決めます。詳細は[設計](../design/issue-788-runtime-retention-adapter.md)。

- **root**: `<game_directory>/session_artifacts/<session_id>/`。Sessionごとに分け、そのSessionのJournal参照だけでGCできるようにします。rootは最初の`persist()`でできるまで作りません（無ければ`store_absent`で見送り）。
- **参照snapshot**: `JournalReferenceSource`（`EventJournal.artifact_refs_after`で前回位置からの増分だけ）、`ExperienceReferenceSource`（設定時のみ。writer契約: append-onlyかatomic replaceで、参照付き書込みを`recording(target="experience:<name>")`内で行う）、`PinnedReferenceSource`（保存途中・Replay中）を合成します。1つでも不完全・失敗・別Session・古い・世代不一致ならGCは**削除0件**で見送ります（`SkippedRetention.reason`）。
- **競合**: `persist()`はadmit前にdigestをpinし、Journalへ参照を書く側は`recording()`のgate内で書きます。`EventJournal.set_reference_guard`により、gate外の参照付きappendは拒否されます。lock順序は gate → root lock → journal lock です。
- **trigger**: `RetentionTriggerPolicy`の最短間隔（5秒）・保存件数（64件）・推定使用量・周期（5分）で、背景worker（初回の保存で開始する1本のthread）から起動します。`session_close`と`maintenance`は間隔と条件を迂回します。1回の物理削除は256件までで、残りは`deferred_artifacts`として次回に回します。
- **close**: `ManagedSessionRuntime.close()`がJournalを閉じる前に最終GCを1回行い、結果を`retention_close_result`へ残します。skipやpressureでもcloseは失敗しません。
- **Trace**: GC結果はJournalに`retention.gc/v1`として記録します（GC回数・削除bytes・blocked refs・各latency・over-budget。digestとpathは含めません）。
- **既知の制約**: GC 1回の計算量は索引全件（上限`max_entries`）を読む`plan`に依存します。走査量の分割は#164側の課題です。終了済みSessionのrootの掃除は後続Issueです。

## Orphan Reconciliation（Issue #790）

`orphan_reconciler.py`の`OrphanReconciler`が、索引にも参照にも無い未索引Artifact（crash由来のorphan）を状態分類し、安全なものだけを`artifact_trash.py`の`ArtifactTrash`経由で回収します。詳細は[設計](../design/issue-790-orphan-reconciliation.md)。

- **状態**: `active_pending`（pending recordが参照）・`live_referenced`・`recent_unindexed`（grace未満・未来時刻）・`safe_orphan`・`corrupt_or_unknown`。`safe_orphan`だけを削除し、metadata破損・余分なfile・link・size/digest不一致・検証未実施は消さず診断として報告します。
- **API**: `plan(trigger)`（dry-run。何も変更しない）→`apply(plan)`（再検査して削除）、`reconcile(trigger, dry_run=)`、`pending_audits()`/`acknowledge(ids)`、予算超過時の`reclaim_budget`（orphanの上乗せで正常な索引entryを消さないよう、先にorphanを回収し、回収できなければ`orphan_pressure`で`collect`しない）。
- **安全**: live sourceの取得とpayloadのSHA-256検証はroot lock外、lock内は読取りと削除のみ（`max_lock_hold_ms`で有界）。`apply`は索引・pending・liveを取り直し、fingerprint（inode・size・mtime・ctime）とgraceを再確認し、計画外の対象は追加しません。不明（索引/pending/trash不正・live不完全・時計の巻き戻し）は削除0です。
- **crash安全**: `ArtifactTrash.discard`は`pending` intent→`os.rename`→`renamed`（論理削除の完了点）→`rmtree`→`swept`の順で、各停止点から`sweep`が再開します。`LocalRetentionFileSystem.remove_object`もこの経路を使うので、`collect`・#789の補償・`recover_pending`の途中停止でもshardに半壊objectが残らず、`measure`を止めません。
- **audit**: 削除ごとにintentから`OrphanDeleteAudit`（digest・artifact_type・data class・byte・理由・契機。pathとpayloadなし）を再生成でき、呼出元が永続化して`acknowledge`するまで残ります。
- **配線は未実装**: startup/session close/periodicからの呼出しと`LiveReferenceSource`の実装（#788のLiveReferenceProviderをそのまま渡せる）は#225で行います。壊れた未索引objectは引き続き`measure`を止め、隔離（quarantine）は後続です。
