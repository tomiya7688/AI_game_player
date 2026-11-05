# Plugin Capability Manifest v1：Plugin権限の宣言と判定

[動作と配置の見取り図へ戻る](README.md)

Issue [#152](https://github.com/tomiya7688/AI_game_player/issues/152)（親 [#58](https://github.com/tomiya7688/AI_game_player/issues/58)）で定義する、PluginがどのHook接続点で何をするかを宣言する版付き契約と、その宣言だけを根拠にHookを許可する判定です。宣言のないHookは登録しません（deny-by-default）。Manifestが不正・未知の版・未知の権限なら、そのPluginは1つも登録しません（fail-closed）。

Optional Featureの依存と代替を表す[Optional Capability Manifest](capability_manifest.md)（#219、`runtime/manifest.py`）とは別の契約です。こちらはPlugin（Hook）の権限だけを扱います。

| 正本 | 役割 |
|---|---|
| `schemas/plugin_capability_manifest/v1/plugin_capability_manifest.schema.json` | JSON Schema（draft 2020-12）。field・型・字面・件数上限、権限IDの一覧（`x-kadoka-permissions`）、Schemaで表せない規則（`x-kadoka-validator-rules`） |
| `schemas/plugin_capability_manifest/v1/conformance.json` | Manifest検証の言語共通の適合ケース（67件）。期待値（`valid`またはエラーcode）とSchema単独の判定結果 |
| `schemas/plugin_capability_manifest/v1/enforcement_conformance.json` | 権限判定の言語共通の適合ケース（29件）。Manifest・Hook登録・入力/出力Envelopeの組と期待値（`allow`または拒否code） |
| `schemas/plugin_capability_manifest/v1/examples/perception_ocr_hint.json` | `perception`/`output`でread・injectするPluginの有効例 |
| `src/ai_game_player/plugin_capability_manifest.py` | 参照validator（`PluginCapabilityManifest`、`decode_plugin_capability_manifest()`、`PluginManifestError`） |
| `src/ai_game_player/plugin_capability_policy.py` | 参照の判定（`PluginCapabilityPolicy`、`PluginCapabilityDenied`、監査記録`PluginCapabilityDecision`） |

## 文書の形

全fieldが必須で、最上位とHook・予算等の各objectは未知fieldを拒否します。

| field | 内容 |
|---|---|
| `manifest_version` | Manifest契約の版。v1では`1`だけ |
| `plugin_id`・`plugin_version` | Pluginの名前空間ID（StageEnvelopeの`producer_id`と同じ字面）と版。Hook登録の`hook_version`はこの版と一致させます |
| `compatibility.stage_envelope_schema_versions` | 対応する[StageEnvelope](stage_envelope.md)契約の版（1-16件） |
| `permissions` | 要求する権限ID（1-16件、重複なし） |
| `hooks` | Hookごとの宣言（1-32件） |
| `audit.publisher`・`audit.description` | 監査用の公開者名（1-128文字）と説明（0-512文字）。制御文字は不可 |

Hookの宣言は次のとおりです。

| field | 内容 |
|---|---|
| `hook_id` | `HookRegistration.hook_id`。`plugin_id`自身か`<plugin_id>.`で始まるID。Manifest内で重複不可 |
| `mode` | `read`・`modify`・`inject`のどれか1つ |
| `stage`・`boundary` | 接続するstage境界。`safety`・`execution`では`read`だけ |
| `input_payload_schemas` | 受け取ってよいpayload契約（`payload_schema`と`versions`）の一覧（1-16件） |
| `inject_targets` | `inject`が作ってよい派生Envelopeの`stage`・`boundary`・`payload_schema`・`versions`（injectは1-16件、他は空）。`safety`・`execution`は不可 |
| `budget` | 登録が要求してよい上限（`timeout_ns`・`max_output_bytes`・`max_injected`）。範囲はHook runtimeの上限と同じ。inject以外の`max_injected`は0 |
| `data_classes` | 扱うdata classの宣言（0-32件）。[#224](https://github.com/tomiya7688/AI_game_player/issues/224)（Data Inventory）の語彙を想定しますが、v1では字面だけを検証し、判定には使いません（監査用） |

## 権限ID

| 区分 | 権限ID | v1での扱い |
|---|---|---|
| in-processで判定できる | `hook.read`・`hook.modify`・`hook.inject` | Hookが使うmodeと過不足なく一致させます。使うmodeが未宣言なら`undeclared_permission`、使わないmodeの宣言は`malformed` |
| 隔離runtimeが必要 | `model.invoke`・`network.connect`・`filesystem.read`・`filesystem.write` | 宣言は有効です（監査と将来の隔離のため）。ただし同じprocessでは制限できないため、`PluginCapabilityPolicy`はこれを宣言したPluginのHookを登録時に`permission_unavailable`で拒否します（[#235](https://github.com/tomiya7688/AI_game_player/issues/235)まで） |
| 付与しない | `input.direct`・`safety.override` | どの実行形態でも付与しません。宣言したManifestは`forbidden_permission`で拒否します |

上記以外のIDは`unknown_permission`です。Plugin独自の権限IDは定義できません。直接の入力（OS入力API・executor）とSafety Required経路の無効化・迂回を表す権限はありません。`input.direct`と`safety.override`は、要求されたことを明確に拒否するためだけに予約しています。これは宣言上の分離です。Plugin codeがOS入力APIを直接importして使うことは防げません（「sandboxではない」を参照）。

## 検証とエラーcode

`decode_plugin_capability_manifest()`は、64KiBを超える入力をparse前に拒否し、UTF-8・BOMなし・入れ子深さ32以下・重複key・`NaN`の規則をStageEnvelope v1と同じ実装で確認してから、`PluginCapabilityManifest.from_dict()`で検証します。整数fieldは整数token（`1.0`・`5e7`は不可）です。

| code | 意味 |
|---|---|
| `malformed` | 形・型・字面・範囲・重複・名前空間の違反 |
| `unsupported_version` | `manifest_version`が1以外（boolや文字列は`malformed`） |
| `limit_exceeded` | byte数・深さ・配列件数の上限超過 |
| `unknown_permission` | v1にない権限ID |
| `forbidden_permission` | 付与しない権限、`safety`・`execution`でのread以外、`safety`・`execution`へのinject |
| `undeclared_permission` | Hookのmodeに対応する`hook.*`権限がない |

検査は`manifest_version`→fieldの過不足→`plugin_id`→`plugin_version`→`compatibility`→`permissions`→`hooks`→権限とmodeの一致→`audit`の順で行い、配列は先頭から調べ、最初の違反でcodeが決まります。他言語の実装はSchemaの検証に加え`x-kadoka-validator-rules`を実装し、`conformance.json`の全ケースで期待どおりの結果になることを適合の条件とします（`schema_valid: true`かつ期待値がエラーのケースが、Schemaだけでは表せない規則です）。

`PluginCapabilityManifest`は不変で、権限・版・data classを昇順、Hookを`hook_id`順に正規化します。`digest()`はkey昇順・空白なしのUTF-8 JSONのSHA-256で、監査記録で同じ宣言を特定するのに使います。

## 判定（PluginCapabilityPolicy）

```python
policy = PluginCapabilityPolicy([decode_plugin_capability_manifest(data)])
runtime = HookRuntime(enabled=True, access_policy=policy)
```

`HookRuntime`の`access_policy`に渡すと、次の3か所で判定します。拒否は`PluginCapabilityDenied`（`ValueError`）で、`code`は決定論的です。

| 時点 | 確認する内容 | 拒否時の動作 |
|---|---|---|
| `register()` | `hook_id`の宣言（なければ`manifest_missing`）、`hook_version`と`plugin_version`（`version_mismatch`）、mode（`mode_not_granted`）、stage・boundary（`point_not_granted`）、隔離が必要な権限（`permission_unavailable`）、timeout・出力byte数・inject件数（injectのみ）が宣言以内か（`budget_exceeded`） | 例外を送出し、登録しません |
| Hookを呼ぶ前 | 入力Envelopeの`schema_version`がruntimeとManifestの互換範囲の両方に一致するか（`envelope_version_not_granted`。出力にも同じ検査）、stage・boundary（`point_not_granted`）とpayload契約・版（`payload_schema_not_granted`） | Hookを呼ばず、`failure_kind: "capability_denied"`・`error_code`に拒否codeを記録します。Hookの失敗ではないため連続失敗数は増やさず、quarantineしません。`halt`方針のHook（modifyの既定）なら境界を停止します |
| Hookの出力後 | modifyは入力と同じ範囲か。injectは`inject_targets`のどれかに一致するか（`output_not_granted`）、Artifact参照が入力Envelopeにあるものだけか（`artifact_ref_not_granted`） | その出力を全件捨て、`capability_denied`として記録します（他の不正出力と同じく連続失敗数に数えます） |

`access_policy`が拒否以外の例外（小文字の`code`属性を持つ`ValueError`系以外）を送出した場合も拒否として扱い、`error_code: "policy_error"`を記録します（判定の障害で許可せず、通常の拒否とは区別します）。拒否の判定は、出力の再検証（[Processing Hook Runtime](hook_runtime.md)）のprovenance・境界規則の後に行います。

`add()`は、Manifestを`to_dict()`→`from_dict()`の往復で再検証します（直接構築した値も検証を通します）。runtimeのStageEnvelopeの版が`compatibility`にない場合は`incompatible`、`plugin_id`または`hook_id`が登録済みなら`conflict`で拒否します（後から登録したManifestで権限を差し替えません）。`decisions`は登録判定の監査記録（入力・出力の拒否はEnvelopeごとに起きるため、ここではなく`HookDispatchResult.records`の`error_code`に残します）（Hook ID・版・mode・接続点・許可/拒否code・`plugin_id`・`plugin_version`・`manifest_sha256`）を直近256件まで保持します。payloadは記録しません。

`access_policy`を渡さないruntime（既定）は判定をしません。これは権限の確認があることを意味しません。設定やUIからPluginを読み込む経路はまだありません。その経路を追加する時は`PluginCapabilityPolicy`の指定を必須にします。

## sandboxではない

`PluginCapabilityPolicy`は同じprocess内の方針の判定（policy check）であり、sandboxではありません。

- 判定するのは、runtimeがHookへ渡すEnvelope、Hookが返したEnvelope、Hookの登録値だけです。
- Plugin codeはPythonのimportでfile・network・OS入力API・Core内部objectを直接使えます。判定はそれを防げません。そのため、それらの権限を宣言したPluginは登録しません。宣言しないPluginがそれらを使うことも検出できません。
- CPU・memory・timeoutしたthreadは制限しません（[Hook runtimeの「隔離しないもの」](hook_runtime.md#隔離しないもの)）。

信頼できないPluginを動かす前提ではありません。subprocess/IPCによる隔離と、隔離境界でのcapabilityの強制は[#235](https://github.com/tomiya7688/AI_game_player/issues/235)の範囲です。同じManifestと`enforcement_conformance.json`を、隔離runtimeの判定でも使えるようにしています。

## 版と互換性

- `manifest_version`は1だけです。2以上・0は`unsupported_version`で拒否します。版を上げる場合は、StageEnvelopeと同様に版ごとの移行関数を追加します（v1では移行表を持ちません）。
- `compatibility.stage_envelope_schema_versions`に、runtimeのStageEnvelope版（現在1）がないManifestは登録しません（`incompatible`）。runtime側の版が変わった場合も同じ判定で、未対応のPluginを登録しません。
- 権限IDを追加する場合は、旧版の検証器が`unknown_permission`で拒否するため、Manifestの版を上げる必要はありません（新しい権限を要求するPluginは旧runtimeで登録されません）。

## 未実装（#152の残り）

- Manifestの配布・発見（fileからの読み込み、署名、Catalog #99）と、設定・UIからのPlugin有効化。
- `data_classes`と#224のData Inventoryの照合、remote送信や保存のpolicyとの接続。
- 隔離runtime（#235）での`model`・`network`・`filesystem`権限の付与と強制。
