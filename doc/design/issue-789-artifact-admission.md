Spec-Status: READY
Depends-On: #164, #167
Decision-Gaps: none
Parent: #18

# Issue #789 Artifact Admission Transaction

## 現状調査

### 既存実装（再利用するもの）

- `src/ai_game_player/artifact_store.py`（#167）: `FileSystemArtifactStore.put`はdigest既存なら再書込せず参照を返し、無ければ`.pending-*`へ書いてから`os.rename`で公開する（atomic put）。ただし`rename`後の`_fsync_directory`が例外を出すと「objectは公開済みだが呼出元には例外」になり得る。`read_metadata`/`object_path`は#164で追加済み。shard directory（`root/<2桁>`）がsymlinkでもputは追従する。
- `src/ai_game_player/retention_collector.py`（#164）: `RetentionCollector.can_admit/register/collect/set_protected`。`can_admit`と`register`の間は呼出側任せで、同時writerがhard limitを超え得る。`register`はput済みobjectを含めた合計だけを比較する。`collect`は索引を先に置換してからobjectを削除する。docstringが「同じrootへの同時実行は呼出側が直列化する」と明記しており、本Issueがその直列化を提供する。
- `src/ai_game_player/retention_index.py`: 索引の厳格な検証。破損時は`RetentionIndexError`でfail-closed。
- `src/ai_game_player/cross_process_lock.py`: `msvcrt.locking`/`fcntl.flock`による有界待ちのadvisory lock（#794で導入）。Issueのv1方式と同一の機構なので再利用する。
- `src/ai_game_player/atomic_json.py`: `stage_json_write`（fsync+同一directory内置換）。pending recordの保存に再利用する。

### 現状の欠陥

1. `can_admit()`→`put()`→`register()`が別々の操作で、2 writerが同時に判定を通るとhard limitを超える。
2. `put`成功後に`register`が失敗すると、未索引objectが残る（orphan）。呼出側が消すべきか判断する材料が無い。
3. `collect`と`register`が別processで同時に走ると、索引のlost updateや、GCが登録途中のobjectを消す競合が起き得る。

## 成果物

- `src/ai_game_player/retention_root_lock.py`（新規）: root単位の協調lock。`.retention.lock`のOS advisory lock＋同一process内の`RLock`。同一threadの再入可。待ちは単調時計の期限で有界。
- `src/ai_game_player/admission_pending_store.py`（新規）: pending admission record（`PendingAdmission`）の厳格なschemaと、`root/.admission-pending/<admission_id>.json`への保存・列挙・削除。
- `src/ai_game_player/artifact_admission.py`（新規）: `ArtifactAdmissionRequest`、`AdmissionPutPort`（writerに渡す制限付きput口）、`ArtifactAdmissionCoordinator.admit`、`inspect_pending`/`recover_pending`、`AdmissionTraceEvent`/`AdmissionStats`。
- `src/ai_game_player/retention_collector.py`（最小追加）:
  - `register`/`set_protected`/`collect`を同じroot lock内で実行（`lock_timeout_seconds`をコンストラクタに追加。既定5秒）。`coordination_lock()`を公開。
  - `measure()`: 索引と使用量を検証付きで返す読取API（`can_admit`の内部計測を公開）。`_StoreUsage`を`StoreUsage`として公開（旧名は別名で残す）。
  - `discard_unindexed_object(digest)`: lock内で「索引に無い」ことを再確認して安全にobjectを削除。
  - `_is_junction`は`retention_root_lock.is_junction`へ移し、collector側は別名で再公開（既存テストのpatch先を維持）。
- `tests/test_artifact_admission.py`（新規）、`tests/test_retention_root_lock.py`（新規）。
- 文書: `doc/versions.md`（Unreleased 1件）、`generated/repo_map.json`再生成。

## 入出力

### ArtifactAdmissionRequest（frozen dataclass）

| field | 型 | 契約 |
|---|---|---|
| `payload_size` | int | 0以上。writerが書くpayloadのbyte数（予約量） |
| `expected_sha256` | str \| None | 小文字SHA-256。指定時は事前に「既存digestなら追加容量0」を判定でき、writerのpayloadがこのdigestでなければput前に拒否 |
| `artifact_type` | str | `[a-z][a-z0-9_.-]*` |
| `media_type` | str | `ArtifactReference`と同じMIME形式 |
| `entry_id` | str | 索引のentry_id規則 |
| `protected` | bool | 索引登録時のProtected |
| `source_ref` | str | 診断用の識別子（`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`）。pathや秘密を入れない |
| `sensitive` | bool = False | Issueの欄に無いが`ArtifactReference.sensitive`を保存するために必要な追加欄（既定False） |

### admit

`ArtifactAdmissionCoordinator(collector, *, trace=None, records=None)`。`admit(request, writer) -> ArtifactReference`。

`writer: Callable[[AdmissionPutPort], ArtifactReference]`。`AdmissionPutPort.put(payload: bytes) -> ArtifactReference`は、requestの`media_type`/`artifact_type`/`sensitive`をStoreへ固定して渡す唯一の口で、pathやStoreそのものはwriterへ渡さない。1回のadmitで`put`は1回だけ呼べる。putの直前に次を検査する: payload長が`payload_size`と一致、digestが`expected_sha256`と一致、shard directoryがsymlink/junctionでない。いずれか違反ならStoreへ書かない。

### エラー

| 状況 | 例外 | Store変更 |
|---|---|---|
| request不正 | `ValueError` | なし |
| root/lock/pending directoryがsymlink・junction | `RetentionRootUnsafeError` | なし |
| lock待ち期限超過 | `RetentionLockTimeoutError`（`TimeoutError`） | なし（transaction未開始） |
| 索引・metadata不正（判定不能） | `RetentionIndexError` | なし |
| entry_id重複・件数上限 | `RetentionIndexError`/`RetentionCapacityError` | なし（put前に検出） |
| hard limit超過 | `RetentionCapacityError` | なし（put前に拒否） |
| writer/put/検証/register失敗 | `ArtifactAdmissionError`（`__cause__`に原因、`rolled_back`、`pending_retained`、`admission_id`） | rollback済み、または補償不能ならpending record残存 |

## 手順

### 責務境界

- coordinatorは「容量判定・put・register・補償・pending record」を担当する。容量計算・索引更新・object削除の実処理はCollector（既存）に委ね、Store写しを持たない。
- すべての書込み経路（admit/register/set_protected/collect/recover）は同じroot lockを取る。読取専用の`plan`/`can_admit`/`needs_collection`はlockを取らない（助言用で、admit内で再計測する）。

### Transaction（admit）

1. request検証（lock前）。
2. root lock取得: root（無ければ作成）・`.retention.lock`がsymlink/junctionなら拒否。同一process内`RLock`（期限付き）→ OS advisory lock（`cross_process_file_lock`、残り時間で有界）。期限超過は`RetentionLockTimeoutError`でtransactionを開始しない。lockは`mtime`等で奪わない。
3. `collector.measure()`で索引とStore使用量を再計測（索引・全metadataを検証、不正はfail-closed）。
4. entry_id重複・件数上限をput前に確認（register失敗によるrollbackを避ける）。
5. 容量判定: `expected_sha256`が索引または未索引の既存objectなら追加容量0、それ以外は`payload_size`。`使用量+追加 > hard_limit_bytes`なら`RetentionCapacityError`で拒否（pending recordもobjectも作らない）。`expected_sha256`無しの場合は存在を事前に知れないため`payload_size`で保守的に判定する（`can_admit(sha256=None)`と同じ）。
6. writer実行。`AdmissionPutPort.put`内でdigestとobject既存有無（`preexisting`）を求め、**put直前**にpending recordを原子的に書く（admission_id、digest、`created_object = not preexisting`、entry_id、request要約、開始時刻、pid、source_ref）。その後`store.put`。
7. put結果の検証: 参照のdigest/media_type/sensitiveがputしたpayloadと一致、`store.verify`のmetadataの`artifact_type`と`size_bytes`がrequestと一致。不一致はcommitしない。
8. `collector.register`（同lock内の再入）。失敗時は索引を再読込し、**同じ`entry_id`かつ同じdigest**の記録が既に有るなら（索引公開後の例外。entry_idは手順4で重複を除いてあるため、有れば今回のregisterが公開したものと一意に判定できる）成功とみなして手順9へ進み、無ければ補償へ。
9. 成功: pending recordを削除し、結果を返す。削除失敗はadmissionを失敗にせず、traceに`pending_cleanup_failed`を残す（後で`recover_pending`が「索引に有る」として記録だけ消す）。
10. lock解放（`finally`）。

### 補償（rollback）

writer/put/検証/register のどの段階で例外が出ても（`BaseException`でも）、次の順に行い、元の例外を`ArtifactAdmissionError`で包んで再送出する（`BaseException`はそのまま再送出）。

- pending recordが未作成（putへ到達前）: 何も消さない。
- `created_object`がFalse（既存digest）: objectは**絶対に消さない**。recordだけ消す。
- `created_object`がTrue: 索引を再読込し、(a)digestが索引にある、(b)他のpending recordが同じdigestを参照している、(c)root外/symlink、のいずれかなら消さない。そうでなければ`collector.discard_unindexed_object`で削除し、recordを消す。
- 削除またはrecord削除が失敗、または他recordの読取が不正（fail-closed）: **recordを残す**（`pending_retained=True`）。silent successにしない。

### 例外の分類と順序

| 発生位置 | 外へ出る例外 | `rolled_back` | trace `outcome` |
|---|---|---|---|
| put前（lock・検証・容量・entry_id・計測） | 元の例外（`RetentionCapacityError`等） | - | `rejected_capacity`/`rejected_invalid`/`lock_timeout` |
| writerがputを呼ぶ前に例外、またはportの2回目のput、payload不一致 | `ArtifactAdmissionError`（`__cause__`に原因） | True（record未作成で取り消す対象が無い） | `failed_rolled_back` |
| put・検証・registerで例外 | `ArtifactAdmissionError` | 補償が完了すればTrue、補償不能ならFalseで`pending_retained=True` | `failed_rolled_back`/`failed_pending_retained` |
| `BaseException`（`KeyboardInterrupt`等） | **元の例外をそのまま**再送出（包まない） | 補償は`Exception`と同じ順序で最善努力。補償できなければrecordが残る | 同上 |

trace eventとstatsは、`admit`の`finally`で成功・失敗・`BaseException`のすべての経路について**ちょうど1回**確定する。補償の完了または失敗は例外を再送出する前に確定する。補償はI/Oなので、補償中にさらに中断された場合は記録（pending record）が残り、`recover_pending`が回収する。

### 容量の不変条件

hard limitの比較対象は、索引登録済みobjectと**未索引objectの両方**を含むdisk上の実使用量（`measure()`）である。中断したadmissionが残した未索引objectも次のadmissionの計測に含まれるため、残骸があっても合計はhard limitを超えない（超過する見込みなら拒否される）。pending recordそのものは容量の予約を表さない（予約はlock保持と、lock内でput直前まで続く再計測で成立する）。残骸を減らしたい場合は`recover_pending`を呼ぶ（admitは自動では回収しない。破損recordが1件あるだけで全admitが止まるのを避けるため）。

`expected_sha256`が無い場合は存在を事前に知れないため、既存digestでも`payload_size`分を追加として判定する（保守的に拒否側へ倒す）。この場合も、putの直前にlock内で既存有無を判定して`created_object`を決めるため、補償が既存objectを消すことはない。

### 所有権（objectが誰のものか）

- `created_object=True`: このadmissionがputで新規作成した。補償・回収で消してよい（索引・他recordが参照しない場合に限る）。
- `created_object=False`: putの時点で既に有った。索引登録済みでも未索引（過去の残骸）でも、このadmissionは所有せず**消さない**。未索引の既存objectの掃除は#790の責務で、`preexisting`のrecordは削除してよい（recordを残しても所有権が増えず、回収対象も増えない）。
- lockはroot lock fileを開いたまま保持し、このfileを削除・renameしない（Windowsで他processのlock取得が共有違反にならない）。

### 異常終了とpending record

processがputの前後で落ちるとrecordが残る。lock所有中だけ変更が進むので、あるprocessがlockを取れた時点で既存recordは全て中断済みと判断できる。`inspect_pending()`は実状態と照合して分類する（読取専用）。

| 分類 | 条件 | `recover_pending`の動作 |
|---|---|---|
| `registered` | 索引に**同じentry_idかつ同じdigest**の記録がある | recordだけ削除 |
| `indexed_by_other_entry` | digestは索引にあるが、このrecordのentry_idの記録が無い（admissionは完了していない） | recordだけ削除（objectは他entryが参照するため消さない。呼出元には成功を返していないので、entryを補完登録はしない） |
| `no_object` | objectが無い | recordだけ削除 |
| `preexisting` | `created_object=False` | recordだけ削除（objectは触らない） |
| `orphan_created` | `created_object=True`、objectあり、索引に無い | 他のpending recordが同digestを参照しなければobjectを安全削除してrecord削除。参照があれば残す |

malformed record・サイズ超過・symlinkはfail-closedで`AdmissionRecordError`（他recordの回収もそこで停止）。#790はこの`inspect_pending`の分類を入力にできる。orphan全般の掃除は#790の責務で、recoverは「admission自身のrecordが指すobject」に限る。

### 不変条件の担保

- hard limit判定とputの間: lock内で再計測し、putまで同じlockを保持。
- pre-existing digestをrollbackで消さない: `created_object`がputの直前にlock内で確定。
- 索引登録済みobjectをrollbackで消さない: 削除直前に索引を再読込。
- malformed index/metadata: `measure`が`RetentionIndexError`。putしない。
- symlink/junction/root外: root・lock・pending dir・shard directoryを検査し、削除は`_safe_object_path`の既存検査を使う。
- digest/size mismatch: put前に拒否、put後はmetadata照合で不一致ならcommitしない。

### Trace

`AdmissionTraceEvent`（`outcome`、`admission_id`、`source_ref`、`entry_id`、`artifact_sha256`、`lock_wait_ms`、`transaction_ms`、`rolled_back`）をadmitごとに`trace`コールバックへ渡す。`outcome`は`committed`/`rejected_capacity`/`rejected_invalid`/`lock_timeout`/`failed_rolled_back`/`failed_pending_retained`。累積は`coordinator.stats`（件数、rollback数、rollback失敗数、lock待ち合計/最大ms、transaction合計/最大ms、`pending_cleanup_failed`数、`trace_errors`数）。traceコールバックの例外はtransactionに影響させず`trace_errors`に数える。

## 代替案と不採用理由

- lockを`mtime`/pidで奪う方式: crash時の誤奪取がありIssueも禁止。OS lockを正本にする。
- lockを`admit`内だけに置き`collect`は対象外: collectとの競合（完了条件）が解消しないため、Collectorの書込み系にも同じlockを取らせる。
- 常にpending recordをlock取得直後に書く: 容量拒否のたびにfsyncが増える。objectを書く直前に書けばrecoverに必要な情報（digest・created）が揃う。
- writerへ`FileSystemArtifactStore`を渡す: 任意pathへ書けてしまう（Issueが禁止）。bytesだけを受けるportにする。
- rollbackでStoreの全orphanを掃除: #790の範囲。自分が作ったobjectだけ。

## 範囲外・残件

- orphan全般の掃除・一覧・Issue #790のreconciliation本体（本Issueはrecordと分類まで）。
- value-aware retention、Remote object store transaction、Experience payload schema変更。
- Store自体を経由しない書込み（`FileSystemArtifactStore.put`の直接呼出し）はlockの対象外。admission契約を使わない呼出しの競合は防げない（docstringで明記）。
- 本番経路への接続: 現在`can_admit`/`register`を呼ぶ本番コードは無い（#164時点でライブラリのみ）ため、呼出側の置換は発生しない。

## テスト

`tests/test_artifact_admission.py`（受入条件ごと）:

| 条件 | 試験 |
|---|---|
| 2 writer同時でhard limit不超過 | 2 coordinator×thread、予算が1件分のとき1件のみ成功、他は`RetentionCapacityError`。Store使用量≦hard |
| same digest同時 | 同payloadを別entry_idで同時admit、object1個、entry2件、使用量=1件分 |
| digest既存は容量0 | 満杯のStoreで既存digestの再admit（別entry_id）が成功 |
| hard limitはput前 | 超過時にobject・pending record・shardが増えない |
| put失敗 | writerが例外→objectなし・record無し・`rolled_back` |
| put後の例外（rename後fsync失敗相当） | 公開済みobjectをrollbackで削除 |
| register失敗 | 索引書込みを失敗させる→新規objectを削除・record無し・例外はsilentにならない |
| index write失敗 | 同上（`RetentionFileSystem.write_index`差替え）。索引は元のまま |
| rollback不能 | `remove_object`も失敗→record残存・`pending_retained` |
| entry_id照合 | 同digestが別entry_idで登録済み・自entry_idが無いrecordは`indexed_by_other_entry`となり、object不削除でrecordのみ回収 |
| 例外分類 | writerがputを呼ばず例外・2回目のput・`KeyboardInterrupt`相当（`BaseException`）で、event/statsが1回確定し元の型が保たれる |
| crash fixture | put後register前でプロセス相当の中断→record残存→`inspect_pending`が`orphan_created`→`recover_pending`でobject+record回収 |
| 各分類 | `registered`/`no_object`/`preexisting`のrecover |
| pre-existing不削除 | 既存digestでregister失敗→objectが残る |
| 索引登録済み不削除 | 同digestが他entryで登録済みのときrollbackで消えない |
| collect競合 | collectが走っている間admitは待ち、完了後に整合。逆順も |
| invalid metadata/symlink/junction | 破損索引・破損metadata・root symlink/junction・lock fileがsymlink・shardがsymlinkで拒否（権限が無い環境はskip） |
| digest/size mismatch | writerのpayload長・digest違い→put前拒否、Store不変 |
| writerの口 | 2回目のput拒否、portにStore/pathが露出しない |
| lock timeout | 他processがlock保持中は有界時間で`RetentionLockTimeoutError`、解放後は成功 |
| trace | lock待ち・transaction時間・rollback数が`stats`/eventに出る。traceコールバックが例外でも成功 |
| request検証 | 各欄の不正値 |

`tests/test_retention_root_lock.py`: 再入、期限超過、別process排他、symlink拒否。

## 完了条件

- [ ] admissionが単一APIになりput前hard limitを保証する
- [ ] 同一rootのprocess間writer/GCと競合しない
- [ ] put/register途中失敗を再現・回収できる
- [ ] pre-existing objectを誤削除しない
- [ ] pending admissionが#790から判定可能（`inspect_pending`）
- [ ] concurrent testでorphan/hard-limit逸脱が増えない
- [ ] lock wait/transaction latency/rollback countをTraceできる
- [ ] 既存`tests/test_retention_core.py`と`tests/test_artifact_store.py`が不変で通る
