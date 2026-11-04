# StageEnvelope v1：stage間の中間データ契約

[動作と配置の見取り図へ戻る](README.md)

Issue [#150](https://github.com/tomiya7688/AI_game_player/issues/150)（親 [#58](https://github.com/tomiya7688/AI_game_player/issues/58)）で定義する、Pipeline各stageの境界で中間データを外部拡張（Hook・Provider・Replay）へ渡すための版付き契約です。Core内部のPython classをそのまま公開契約にせず、JSONの形と検証規則だけを契約にします。Qt + Rust Core（#661）へ移行しても、同じJSON文書と適合ケースで実装を照合できます。

| 正本 | 役割 |
|---|---|
| `schemas/stage_envelope/v1/stage_envelope.schema.json` | JSON Schema（draft 2020-12）。field・型・字面・件数上限と、Schemaで表せない規則・上限（`x-kadoka-limits`・`x-kadoka-validator-rules`） |
| `schemas/stage_envelope/v1/conformance.json` | 言語共通の適合ケース。各ケースの期待結果（`valid`またはエラーcode）とSchema単独の判定結果 |
| `schemas/stage_envelope/v1/examples/` | Core出力とHook入力の有効例 |
| `src/ai_game_player/stage_envelope.py` | Pythonの参照validator（`StageEnvelope`、`StageEnvelopeError`） |
| `src/ai_game_player/stage_envelope_codec.py` | 上限付きJSON復号・canonical符号化・版移行点 |

## 文書の形

全fieldが必須です。値がない任意項目は省略せず`null`にします。最上位の未知fieldは拒否します。拡張用の任意データは`metadata`に入れ、Coreは中身を解釈せず保存と受け渡しだけ行います。

| 区分 | field | 内容 |
|---|---|---|
| 版 | `schema_version` | Envelope契約の版。v1では`1`だけ |
| 所在 | `envelope_id`, `stage`, `boundary` | Envelope ID、stage ID、stageへの入力側（`input`）か出力側（`output`）か |
| Session | `session_id`, `frame_id`, `turn_id` | 所属Sessionと任意のframe・turn ID |
| 時刻 | `timestamp_utc`, `monotonic_ns` | 末尾`Z`のUTC時刻（小数は1-9桁）と任意の単調時刻（ns、0以上） |
| trace | `trace_id`, `correlation_id`, `parent_envelope_id` | 一連の判断を結ぶtrace、任意の相関ID、直接の元Envelope |
| provenance | `producer_kind`, `producer_id`, `producer_version`, `derived_from` | 作成者の種類（`core`/`hook`/`provider`/`replay`）・ID・版と、入力にした元Envelope ID（最大16件、重複なし） |
| payload | `payload_schema`, `payload_schema_version`, `payload` | payload自身の契約IDと版、JSON object |
| 確信度 | `confidence` | 0以上1以下の数値、または`null`（不明） |
| 拡張 | `metadata` | 名前空間付きkey（例 `plugin.example`）のJSON object |
| 参照 | `artifact_refs` | Artifact Storeへの参照（最大64件）。形は[Session Event Journal](session_event_journal.md)の`ArtifactReference`と同じ |

v1の公式stage IDは`capture`、`perception`、`state`、`reasoning`、`decision`、`safety`、`execution`、`outcome`、`evaluation`です。Pluginが独自に定義するstageは`ext.`で始まり、続く部分が小文字英字で始まるIDにします（例 `ext.plugin.example.filter`。`ext.`・`ext..x`・`ext.1x`は拒否）。Envelope契約の版と`payload_schema_version`は別々に上げます。payloadの形を変えるだけならEnvelope v1のまま`payload_schema_version`を上げます。

ID文字列の規則は次のとおりです。

- 外部生成ID（`envelope_id`・`session_id`・`trace_id`等）: 英数字で始まり、英数字と`_ . : -`だけの1-128文字。UUIDも使えます。
- 名前空間付きID（`stage`・`producer_id`・`payload_schema`・`metadata`のkey）: 小文字英字で始まり、小文字英数字と`_ . -`だけの1-128文字。
- `producer_version`: 英数字で始まり、英数字と`. + _ -`だけの1-64文字。

## 上限と厳密な検証

| 規則 | 値 | 違反時のcode |
|---|---|---|
| 文書のUTF-8 byte数 | 1,048,576以下。BOMなし | `limit_exceeded`（BOM・不正UTF-8は`malformed`） |
| objectと配列の入れ子深さ | 32以下。Envelope rootを1、`payload`自身を2と数え、scalarは数えない | `limit_exceeded` |
| `derived_from` / `artifact_refs`の件数 | 16 / 64以下 | `limit_exceeded` |
| 数値 | 整数はint64範囲、数値は有限。`NaN`・`Infinity`は拒否。小数はIEEE 754 binary64として読み、無限大へのoverflowと0以外の値の0へのunderflowは拒否（通常の丸めは許可） | `malformed` |
| 整数field | `schema_version`・`monotonic_ns`・`payload_schema_version`は小数点・指数のない整数tokenで書く（`1.0`は拒否） | `malformed` |
| 文字列 | 対のないsurrogate（例 `\ud800`）を値・keyに含めない | `malformed` |
| 時刻 | 暦上存在する日時。閏秒（秒60）はv1では表さない | `malformed` |
| object key | 重複keyを拒否（後勝ちの解釈差をなくす） | `malformed` |
| 自己参照 | `parent_envelope_id`と`derived_from`に自身の`envelope_id`を含めない | `malformed` |
| 版 | `1`以外は`unsupported_version`。boolや文字列の版は`malformed` | `unsupported_version` |

入れ子深さは、byte数の確認後、JSONを解析する前に文字列の外の`{`と`[`を数えて判定します。深い文書で再帰parseが失敗する前に拒否するためです。JSON Schemaでは表せない規則（byte数、深さ、payload内の数値範囲、重複key、暦上の日付、自己参照）は`x-kadoka-validator-rules`に列挙しています。他言語の実装はSchemaの検証に加え、これらの規則も実装します。Schemaの検証に通っただけでは契約に適合したことになりません。適合の条件は`conformance.json`の全ケースで期待どおりの結果になることです。`schema_valid: true`かつ期待値がエラーのケースが、この追加規則の対象です。

Schemaの`pattern`は、末尾の改行を許さないためにECMA-262の先読み`(?![\s\S])`で終わります。Rustの`regex` crateのように先読みに対応しない正規表現エンジンでは、同じ規則を文字列全体の一致（Rustなら`^...$`ではなく`\A...\z`）として実装します。

エラーは`StageEnvelopeError`として送出され、`code`は`malformed`・`unsupported_version`・`limit_exceeded`のいずれかです。Hook runtime（#151）は、このcodeを見て継続するか安全に停止するかを決められます。

## 符号化と往復

`encode_stage_envelope()`は、key昇順・区切りの空白なし・非ASCII文字をそのまま・`NaN`禁止のcanonical JSONをUTF-8で出力します。同じEnvelopeからは常に同じbytesが得られ、`decode → encode → decode`で値とbytesが一致します。浮動小数の字面は言語ごとに異なる場合があります。そのため他言語の実装はbytesではなく、値が等しいことで比べます。

`StageEnvelope`は不変です。`payload`と`metadata`は読み取り専用のmappingに凍結され、`to_dict()`は元と共有しない複製を返します。Hookが変更した結果は`dataclasses.replace()`で新しいEnvelopeとして作り、作成時に再検証します。

## 版の移行点

古い版の文書は`migrate_stage_envelope_document()`だけで現行版に進めます。版Nの文書を版N+1にする関数を版ごとに登録し、順番に適用します。v1は最初の公開版なので、既定の移行表`STAGE_ENVELOPE_MIGRATIONS`は空です。現行版より新しい版、移行関数のない古い版、版を1つ進めない移行関数は、すべて`unsupported_version`で拒否します。移行した文書も、最後にv1の厳密な検証を通します。

## 後続Issueとの境界

- **Hook runtime（#151）**: stageの前後（`boundary`）でEnvelopeを受け取ります。read/modify/injectの結果は`producer_kind: "hook"`と`parent_envelope_id`を付けた新しいEnvelopeとして作り、`decode_stage_envelope()`またはコンストラクタで再検証します。実行順序・timeout・latency計測は#151の範囲です。
- **Capability（#152）**: `stage`・`boundary`・`payload_schema`の組を、権限の対象範囲（stage/data access scope）として参照できます。`producer_id`・`producer_version`は監査の記録に使います。Envelopeのfieldは権限を与えません。
- **Replay（#153）**: `producer_kind: "replay"`、`trace_id`・`derived_from`で、記録したEnvelopeとの対応を保ちます。
- **Safety**: `stage: "safety"`のEnvelopeは観測用のデータです。この契約には、Safety Gateを無効にしたり迂回したりするfieldはありません。

Hook runtimeと最初の接続点（`DecisionPipeline`の`perception`/`output`）は[Processing Hook Runtime](hook_runtime.md)にあります。他のstageからEnvelopeを生成する処理はまだありません。この資料は契約と参照validatorだけを定義します。
