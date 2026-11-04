Spec-Status: READY
Depends-On: #150（StageEnvelope v1、merged）, #151（Hook Runtime、merged）
Decision-Gaps: なし（下記「判断済み事項」はいずれも保守側＝拒否側の既定で、owner判断なしに後から緩められる）
Parent: #58（関連: #42 Optional Parent、#235 Isolation、#224 Data Inventory）

# Issue #152 設計：Plugin Capability Manifest / Enforcement Contract

今回のスライスは「宣言の契約（schema・参照validator・適合ケース）」と「in-process Hook runtimeでのdeny-by-default判定」まで。隔離・配布・data class照合は範囲外（末尾）。

## 現状調査

- `src/ai_game_player/hook_runtime.py`（#151）: `HookRuntime(authorizer=...)`は登録時に`HookRegistration`だけを受け取る。入力Envelopeのpayload契約や出力を判定する口はない。出力はcanonical JSON往復で再検証し、producer/parent/session/trace、modifyの固定field、injectのsafety/execution禁止を既に検査している。
- `src/ai_game_player/hook_registration.py`: `HookMode`、予算上限（`HOOK_MAX_TIMEOUT_NS`=1s、出力1MiB、`HOOK_MAX_INJECTED_PER_HOOK`=16）。Manifestの予算範囲はこれと同一にする。
- `src/ai_game_player/stage_envelope*.py`（#150）: 名前空間ID・版表記の字面、`CORE_STAGE_IDS`、`ext.`拡張stage、上限付きJSON復号（UTF-8/BOM/深さ32/重複key/NaN/binary64）。Manifestの復号はこの実装を再利用する（private関数を同package内でimport）。
- `schemas/stage_envelope/v1/conformance.json`: `expect`＋`schema_valid`の適合ケース形式。同じ形式を採用する。
- `src/ai_game_player/runtime/manifest.py`（#219）: Optional Featureの`CapabilityManifest`。名前衝突を避け、本Issueは`PluginCapabilityManifest`とする。
- #224は並行開発中。未マージコードには依存しない。

## 成果物

| file | 内容 |
|---|---|
| `schemas/plugin_capability_manifest/v1/plugin_capability_manifest.schema.json` | JSON Schema 2020-12。`x-kadoka-limits`・`x-kadoka-permissions`・`x-kadoka-validator-rules` |
| `schemas/plugin_capability_manifest/v1/conformance.json` | Manifest検証の言語共通ケース（全エラーcode＋valid） |
| `schemas/plugin_capability_manifest/v1/enforcement_conformance.json` | 判定の言語共通ケース（全拒否code＋allow＋incompatible/conflict） |
| `schemas/plugin_capability_manifest/v1/examples/perception_ocr_hint.json` | 有効例（perception/outputのread＋inject） |
| `src/ai_game_player/plugin_capability_manifest.py` | `PluginCapabilityManifest`・`HookCapability`等の不変値、`decode_plugin_capability_manifest()`、`PluginManifestError` |
| `src/ai_game_player/plugin_capability_policy.py` | `PluginCapabilityPolicy`、`PluginCapabilityDenied`、`PluginCapabilityDecision` |
| `src/ai_game_player/hook_runtime.py`（変更） | `HookAccessPolicy` Protocolと`access_policy`引数 |
| tests | `test_plugin_capability_manifest.py`、`test_plugin_capability_policy.py`、`plugin_capability_vectors.py`（適合ケース評価helper） |
| 文書 | `doc/architecture/plugin_capability_manifest.md`、`hook_runtime.md`/`stage_envelope.md`/README/`versions.md`の追記、CIのschema検査対象に1行 |

## 入出力

### Manifest v1（全field必須・未知field拒否）

| field | 型・制約 |
|---|---|
| `manifest_version` | int `1`のみ（bool/文字列は`malformed`、他の整数は`unsupported_version`） |
| `plugin_id` | 名前空間ID `[a-z][a-z0-9_.-]{0,127}` |
| `plugin_version` | `[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}`。登録の`hook_version`と一致必須 |
| `compatibility.stage_envelope_schema_versions` | 正の整数1-16件、重複なし |
| `permissions` | 権限ID 1-16件、重複なし |
| `hooks[]` | 1-32件。`hook_id`（`plugin_id`自身か`<plugin_id>.`接頭辞、重複なし）、`mode`、`stage`、`boundary`、`input_payload_schemas`（1-16件、`payload_schema`重複なし、`versions` 1-16件）、`inject_targets`（injectは1-16件・他は0件、safety/execution不可、(stage,boundary,payload_schema)重複なし）、`budget`（`timeout_ns` 1..1e9、`max_output_bytes` 1..1048576、`max_injected` injectは0..16・他は0）、`data_classes`（0-32件、名前空間ID、重複なし、監査用） |
| （`data_classes`補足） | v1では非強制の監査metadata。字面が名前空間IDなら#224の語彙にない値も受け入れる。#224の語彙と照合する時は、未知値の扱いをManifestの版を上げて定める（v1文書の意味は変えない） |
| `audit` | `publisher` 1-128文字、`description` 0-512文字、制御文字・対のないsurrogate不可 |

上限: 文書64KiB、深さ32（StageEnvelopeと同じ判定）。整数fieldは整数token（`1.0`・`5e7`不可）。

### 権限ID

| 区分 | ID | 扱い |
|---|---|---|
| in-process | `hook.read` `hook.modify` `hook.inject` | 使うmodeと過不足なく一致（不足`undeclared_permission`、余剰`malformed`） |
| 隔離必須 | `model.invoke` `network.connect` `filesystem.read` `filesystem.write` | Manifestとしては有効。Policyが登録時に`permission_unavailable` |
| 禁止 | `input.direct` `safety.override` | `forbidden_permission`。safety/executionでのread以外、そこへのinjectも同code |
| その他 | — | `unknown_permission` |

### エラーcode

- `PluginManifestError.code`: `malformed` / `unsupported_version` / `limit_exceeded` / `unknown_permission` / `forbidden_permission` / `undeclared_permission`、Policy登録時の`incompatible` / `conflict`。
- `PluginCapabilityDenied.code`: `manifest_missing` / `version_mismatch` / `permission_unavailable` / `mode_not_granted` / `point_not_granted` / `budget_exceeded` / `payload_schema_not_granted` / `output_not_granted` / `artifact_ref_not_granted`。
- `HookExecutionRecord`: `failure_kind="capability_denied"`、`error_code`=上記拒否code、判定の障害は`policy_error`。

### API

```python
decode_plugin_capability_manifest(data: bytes | str) -> PluginCapabilityManifest
PluginCapabilityManifest.from_dict(doc) / .to_dict() / .digest() -> str  # SHA-256 hex
PluginCapabilityPolicy(manifests=(), *, stage_envelope_schema_version=1, max_decisions=256)
  .add(manifest) / .authorize_registration(reg) / .authorize_input(reg, env)
  .authorize_output(reg, output, source) / .decisions -> tuple[PluginCapabilityDecision, ...]
HookRuntime(..., access_policy: HookAccessPolicy | None = None)
```

## 手順

1. 復号: byte上限→UTF-8/BOM→深さ→JSON（重複key・NaN拒否）→`from_dict`。検査順は`manifest_version`→field過不足→`plugin_id`→`plugin_version`→`compatibility`→`permissions`（先頭から字面→重複→未知→禁止）→`hooks`（先頭から）→権限とmodeの一致→`audit`。最初の違反でcode確定（決定論的）。正規化: 権限・版・data class昇順、hooksは`hook_id`順。
2. 登録（`Policy.add`）: `to_dict→from_dict`で再検証（直接構築も検証）→runtimeのEnvelope版が互換範囲外なら`incompatible`→`plugin_id`既登録なら`conflict`→各`hook_id`（`hook_id`順）が既登録なら`conflict`（後勝ちで権限を差し替えない。途中で拒否した場合は何も登録しない）。
3. Hook登録（`authorize_registration`）: hook_idの宣言→版→mode→接続点→隔離必須権限の有無→予算（inject以外は`max_injected`を比較しない）。結果を`decisions`（上限件数のdeque、payloadなし、manifest_sha256付き）へ記録。拒否時は例外で登録しない。
4. 呼び出し前（`authorize_input`）: 登録値で宣言を引き直し、Envelopeのstage/boundaryとpayload契約・版を照合。拒否時はHookを呼ばず`capability_denied`を記録。Hookの失敗ではないので連続失敗数・quarantineは変えない。`halt`方針なら境界を停止。
5. 出力後（`authorize_output`）: runtime既存の再検証・provenance検査の後に、出力1件ずつ実行する。再検証・provenance検査・権限判定はいずれも副作用のない純粋な検査で、全件が通るまでHook出力は後段（`HookDispatchResult`）へ採用されない。modifyは入力と同じ範囲（接続点→payload契約の順に判定）。injectは`inject_targets`一致（不一致は`output_not_granted`）を先に、次にArtifact参照を判定する。Artifact参照は出力Envelopeの`artifact_refs`の各要素が、入力Envelopeの`artifact_refs`のいずれかと全field（`artifact_id`・`sha256`・`media_type`・`sensitive`）で等しいこと（不一致は`artifact_ref_not_granted`）。拒否時はそのHookの出力を全件捨て、連続失敗数に数える。
6. fail-closed: `access_policy`のどの例外も拒否扱い。拒否codeとして採用するのは`ValueError`系かつ`[a-z][a-z0-9_]{0,63}`の`code`のみ、他は`policy_error`。`access_policy=None`は従来どおり無判定（既定、権限確認の存在を意味しない）。
7. 位置づけ: 同一process内のpolicy check。sandboxではない（Pluginはimportで任意APIを使える）ため、隔離必須権限を宣言したPluginは登録しない。強制できるのはHook runtime経由の登録・入力・出力だけで、`input.direct`の禁止は「権限として宣言・付与できない」という宣言上の分離である。Plugin codeがOS入力APIを直接importして使うことは、この契約では防げない（実際の阻止は#235の隔離境界の要件）。入力の実行経路は従来どおりDecision VerifierとAction Safety Evaluatorを通り、Hookへはexecutorや座標を渡さない（#151）。
8. deny-by-defaultの適用範囲: `access_policy`に`PluginCapabilityPolicy`を渡したruntimeでは、宣言のないHookは登録されない。`access_policy=None`（#151互換の既定）は無判定であり、権限の確認があることを意味しない。現時点で設定やUIからPluginを読み込む経路はなく、`HookRuntime`自体も既定で無効。第三者Pluginを読み込む経路を追加するIssue（#99・設定/UI有効化）は、`PluginCapabilityPolicy`の指定を必須とすることを受入条件に含める（本設計の残件として明記）。

## テスト

| 受入条件 | 試験 |
|---|---|
| capability schemaがある | schemaのfield/上限/権限一覧が参照定数と一致、`jsonschema`単独判定が全ケースの`schema_valid`と一致、有効例がschemaに通る |
| manifest validationできる | `conformance.json`全件（全6code＋valid）を参照validatorが期待どおり判定。往復・digest安定・正規化・不変性・入力型/byte上限 |
| permission不足を判定できる | `enforcement_conformance.json`全件（全9拒否code＋allow＋incompatible/conflict。Artifact参照は受け取った参照の再利用をallow、新しい参照を`artifact_ref_not_granted`）。HookRuntime統合: 未宣言Hookの登録拒否、入力拒否でHookを呼ばずquarantineしない、modify入力拒否でhalt、inject出力拒否、policy例外のfail-closed（codeを持たない例外・`ValueError`以外でcodeを持つ例外・字面が不正なcodeはすべて`policy_error`）、conflictで既存権限が変わらない、decisions上限 |
| direct input capabilityを分離できる（宣言上の分離。手順7） | `input.direct`・`safety.override`が`forbidden_permission`、safety/executionでのmodify/inject宣言とsafety向けinject先が`forbidden_permission` |
| version compatibility testがある | `manifest_version` 0/2/999が`unsupported_version`、Envelope版の非互換（Manifest側[2]、runtime側2）が`incompatible` |

既存`test_hook_runtime`・`test_perception_hook_boundary`が無変更でpassすること（`access_policy=None`の互換）。

## 完了条件

- 上表の試験と`finish_task.bat`がpass（planner timing #729は単体再実行で記録）。
- 外部AIレビュー（設計・実装）の記録、PR上でCodeRabbit依頼、CI green。
- #152は残件があるためcloseしない。

## 代替案と不採用理由

- **既存`authorizer`コールバックだけを使う**: 登録時しか判定できず、payload契約・inject出力を照合できない。→Protocolで入力・出力の判定口を追加。
- **#219の`CapabilityManifest`を拡張**: Optional Featureの依存・fallbackとPlugin権限は変更理由が別。→別契約。
- **model/network/filesystemを宣言だけで許可**: in-processでは制限できず、許可は実質無制限の付与になる。→隔離runtimeまで拒否（後から緩めても互換）。
- **Plugin独自の権限IDを許可**: 判定不能な権限が増え、deny-by-defaultと矛盾。→`unknown_permission`。
- **拒否時にHookのquarantineを進める（入力拒否も）**: 入力拒否はHookの過失ではなく、別payloadで正常なHookを停止させる。→入力拒否は数えず、出力拒否は数える。
- **後勝ちでManifestを差し替え**: 後から登録したものが権限を広げられる。→`conflict`。
- **jsonschemaを実行時依存にする**: 依存追加とSchemaで表せない規則の二重管理。→参照validatorを手書きし、jsonschemaはテスト時の照合だけ。

## 判断済み事項（owner確認不要の保守的既定）

- 隔離必須権限は宣言可・登録拒否。
- `data_classes`は字面検証のみ（#224の語彙確定後に照合）。
- `decisions`は登録判定のみ記録。Envelopeごとの拒否は`HookDispatchResult.records[].error_code`。

## 範囲外・残件

- subprocess/IPC隔離と隔離境界での強制（#235）。OS sandbox。
- Manifestの配布・発見・署名・Catalog（#99）、設定/UIからの有効化。その経路では`PluginCapabilityPolicy`の指定を必須にする（`access_policy=None`のruntimeへ第三者Pluginを登録しない）。
- in-processでのOS入力API・file・networkの直接利用の阻止（#235）。
- `data_classes`と#224 Data Inventoryの照合、保存/remote policy連携（#224, #123）。
- Hook dispatch結果のRecord/Replay（#153）、Flow構成（#432）。
