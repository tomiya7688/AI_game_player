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
- 削除途中で停止して残ったorphanは、未索引objectとして容量に数え続けますが自動削除しません（orphan cleanupは残件）。
- 同じrootへの同時`register`/`collect`は呼出側が直列化します（プロセス間lockは本Issueの範囲外）。

## 非対象と残り

- near-duplicate、prioritized replay、clustering/prototype、value-aware compactionは #165。
- orphanの自動削除、Experience/Event Journalからの`live_references`自動収集、プロセス間lockは未実装です。runtimeへの接続（どのSessionがいつ`collect`を呼ぶか）も本Issueでは行いません。
