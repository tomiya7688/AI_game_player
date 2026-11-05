# Privacy Data Inventory（data class・1.0既定policy）

## 背景と目的

Privacy条件が文章だけだと、Provider・Recorder・Learningで保存や送信の判断がずれます。Issue #224（親 #123）は、Applicationが扱うdata classと1.0の既定policyを、componentが実行時に問い合わせられるmachine-readable inventoryとして正本化します。

本資料の読者は、保存・送信を追加または変更する実装者と、Remote Consent Enforcement（#226）・Retention/Delete/Export（#225）の実装者です。この変更による通常利用者の操作変更はありません。

## 正本と構成

| 対象 | 役割 |
|---|---|
| `src/ai_game_player/assets/privacy/data_inventory.json` | inventoryの正本（同梱asset、`pyproject.toml`のpackage-dataに含む） |
| `config/data_inventory.schema.json` | 言語非依存のJSON Schema（形だけ。相互参照は下記の検証で扱う） |
| `src/ai_game_player/data_class_policy.py` | `DataClassPolicy`：1 classの値と不変条件 |
| `src/ai_game_player/data_inventory.py` | `load_data_inventory()`と`DataInventory`：読込・相互参照検証・問合せ |
| `tests/test_data_inventory.py` | 既定値・検証・問合せと、未分類の保存/送信箇所を検出するcoverage test |

読込は256KiB上限の通常fileだけを対象にし、JSON objectの重複key、版不一致・未知キー・未登録classへの参照・重複は部分結果なしで拒否します（fail-closed）。

## data classの項目

| 項目 | 値 | 意味 |
|---|---|---|
| `sensitivity` | `low` / `medium` / `high` / `forbidden` | `forbidden`は通常data flowに入れてはならないclass（credential） |
| `default_persistence` | `never` / `off` / `transient` / `local_bounded` / `local` | `off`は既定で保存せず明示opt-in時だけ保存、`transient`は処理中だけの一時file、`local_bounded`は端末内に期限付き保存、`local`は利用者が消すまで保存 |
| `default_retention_ms` | 正整数またはnull | 保存する場合の既定TTL（ms）。`local_bounded`では必須 |
| `remote` | `never` / `opt_in` | `opt_in`は利用者の明示同意があれば端末外へ送れるという意味で、既定で送信する値はありません |
| `contains` | class ID配列 | payloadが内包し得る他class（例: `trace`は`decision_context`を含む） |
| `owner_modules` | module名配列 | 生成・保存を担うmodule。`implemented: false`のclassは空 |

検証で拒否する不変条件:

- `forbidden`は`default_persistence: never`かつ`remote: never`。
- `high`は無期限の`local`保存にしない。
- `local_bounded`はTTL必須。`never`・`transient`・`local`はTTLを持たない（`off`のTTLはopt-in時の既定期限）。
- `contains`は間接的な循環を含まない。
- 外側classは内包classより低い機微度を持たない。端末外送信可能（`opt_in`）なclassは、local-only（`never`）なclassを内包しない。
- credential用の`forbidden` classが1つ以上ある。

## 1.0既定policy（`policy_version: "1.0"`）

| class | 機微度 | 既定保存 | 既定TTL | 端末外送信 | 現状 |
|---|---|---|---|---|---|
| raw_frame | high | off | 1日 | opt_in | 保存箇所なし |
| cropped_image | high | off | 1日 | opt_in | 保存箇所なし |
| ocr_text | high | off | 7日 | opt_in | 単独保存なし。trace/decision_contextへ抜粋が入る |
| ui_metadata | medium | local_bounded | 30日 | opt_in | UI認識・埋め込みmemory |
| decision_context | high | off | 7日 | opt_in | 単独保存なし。traceへ入る |
| model_output | medium | local_bounded | 30日 | opt_in | 評価log等 |
| history | medium | local_bounded | 30日 | opt_in | history/execution history |
| trace | high | local_bounded | 30日 | opt_in | decision trace・Session event journal・campaign/E2E event |
| video | high | off | 7日 | never | 未実装（#201） |
| replay | high | off | 7日 | never | 未実装 |
| learning_dataset | high | off | 30日 | never | 明示操作で生成 |
| challenger_artifact | medium | local_bounded | 30日 | never | 昇格前候補と評価report |
| knowledge | medium | local | なし | never | 知識・採用済みsnapshot |
| crash_diagnostic | medium | local_bounded | 14日 | never | worker診断・bundle self-check |
| safety_audit | medium | local_bounded | 30日 | never | Safety判定・fail-safe journal |
| runtime_log | medium | local_bounded | 14日 | never | 実行log |
| app_config | low | local | なし | never | 設定・UI状態 |
| quality_report | low | local | なし | never | 開発用検査report |
| worker_ipc | low | transient | なし | never | planning worker一時file |
| unclassified | high | off | 1日 | never | 意味型のない`binary.payload` |
| credential | forbidden | never | なし | never | 禁止field名で検出 |

親Issue #123の1.0既定（telemetry OFF、Remote Provider OFF、raw frame/replay保存OFF、traceはlocal-onlyかつ期限付き、録画OFF）と対応します。telemetryは現行コードに送信箇所がないためclassを設けていません。traceは現在local保存だけで、`opt_in`はProvider wire schemaが`trace`を送信classとして宣言できることに合わせた値です。

## 実装の棚卸し（2026-11-04のmain時点）

保存（`writers`、`module:最上位qualname`単位）:

- game directory配下: `history.json`（history）、`decision_trace.json`（trace）、`knowledge.json`（knowledge）、`execution_history.json`（history）、`action_safety.json`・`safety_guard.jsonl`・`fail_safe/`（safety_audit）、`session_events/*.sqlite3`（trace）、`light_adaptation/`配下のsnapshot・pending（knowledge）、Knowledge promotionの`reports/`・`state.json`（challenger_artifact）。
- `data/config.json`・`data/shell_state.json`（app_config）、`user_data/output/log/ai_game_player.jsonl`（runtime_log）。
- 呼出側がpathを渡すもの: UI認識・埋め込みmemory（ui_metadata）、EvaluationLog（model_output）、campaign/E2E event・runtime mode recorder（trace）、bundle self-check・worker診断（crash_diagnostic）、品質report（quality_report）。
- 基盤（classは呼出側で決まる）: Artifact Store（classは`artifact_types`で決まる）、原子的JSON書込み、lock file、Retention索引、journal移行marker。
- frame画素・crop画像・録画を保存するコードはありません。

送信（`transmitters`、`module:qualname`単位）:

- `OllamaProvider._request_decision`: `choose_context()`はDecision Context（state要約・OCR抜粋・候補・評価根拠・直近履歴）、旧`choose()`は`ScreenObservation`（OCR全文・features）と候補を設定endpointへ送ります。
- `OllamaProvider.assess_outcome`: `ScreenObservation.to_dict()`（OCR全文・features）を送ります。
- `OllamaProvider.list_models`: model一覧のGETで、利用者dataを送りません。
- endpointの既定はloopback（`127.0.0.1:11434`）ですが、利用者がremote hostへ変更できるため、送信classを登録しています。raw frame bytesを送る経路はありません。

## componentからの問合せ

```python
from ai_game_player.data_inventory import load_data_inventory

inventory = load_data_inventory()
inventory.policy("trace").default_persistence            # "local_bounded"
inventory.class_for_artifact_type("frame.raw")           # raw_frameのpolicy
inventory.effective_classes(["decision_context"])         # 内包classを含む集合
inventory.remote_eligible_classes()                       # 同意後に送信可能なclass
inventory.forbidden_field_paths(payload)                  # credential名fieldの位置
inventory.ttl_ms_by_artifact_type()                       # RetentionPolicy.ttl_ms_by_type用
```

未登録のclassやartifact型は推測せず`DataInventoryError`で拒否します。`forbidden_field_paths`はfield名だけを見ます（小文字化・`-`→`_`後に禁止名と一致、または`_禁止名`で終わる）。値の内容からcredentialを検出することはしません。接尾一致は`csrf_token`等を見逃さないためのfail-closed判定で、非機密の`*_token`も検出し得ます。自己参照を含むobjectでも同じcontainerは1回だけ辿ります。

## Retention Core（#164）との接続

`ttl_ms_by_artifact_type()`は、Artifact Storeのartifact型ごとに、そのclassの既定TTLを返します。`RetentionPolicy(ttl_ms_by_type=...)`へそのまま渡せ、型を宣言しないpayloadには`unclassified`のTTL（1日）を`default_ttl_ms`として使えます。Retention Core自体はまだruntimeへ接続されていないため、この変更で既存の保存期間は変わりません。

## 未分類の保存・送信を検出するtest

`tests/test_data_inventory.py`の`DataInventoryCoverageTest`は`src/ai_game_player`をASTで走査し、次のいずれかで失敗します。

- 永続化呼出し（`write_text`/`write_bytes`、書込みmodeの`open`、`stage_json_write`、`write_json_array_atomically`、`NamedTemporaryFile`、`mkstemp`、`sqlite3.connect`）を含む最上位定義が`writers`にない。
- 送信呼出し（`urlopen`、`socket`/`http.client`系、`requests`/`httpx`/`aiohttp`）を含む関数が`transmitters`にない。または送信classの内包classに`remote: never`がある。
- path結合（`/ "名前"`）、`Path("…")`、保存拡張子で終わる文字列のpath名が、`writers[].paths`にも`non_data_paths`にもない。
- `artifact_type=`・`expected_artifact_type=`・`artifact_type`引数の既定値として書かれた型が`artifact_types`にない。

inventoryに残った古い登録（コードから消えたsite・path）もstaleとして失敗します。新しい保存・送信を追加したら、同じPRでinventoryへclassを登録してください。

## 非対象と残り

- 既定の保存・送信をruntimeで強制する処理は本Issueの範囲外です。保存期間・削除・exportは #225、送信前のconsent照合・未許可classの拒否・credential拒否・audit eventは #226 が、このinventoryの`remote`・`effective_classes`・`forbidden_field_paths`を使って実装します。
- 現行コードとの差: `ui_metadata`・`history`・`trace`等の`local_bounded`のTTLは、各storeにはまだ適用されていません（#225）。Provider wire schema v1の`data_classes`列挙へ`ui_metadata`・`model_output`を追加しました（#806、v1の互換拡張）。列挙は`remote_eligible_classes()`と一致し、`tests/test_provider_wire_data_classes.py`が`OllamaProvider`の実payloadの全fieldをclassへ対応付けます（未分類fieldで失敗）。送信を実際に許可・拒否するconsent照合は#226です。候補の参考知識（`candidates[].knowledge[]`）は`decision_context`に分類しており、`knowledge` class（`remote: never`）との関係は#226が決めます（DG-3、設計書 `doc/design/issue-806-provider-wire-data-classes.md`）。
- UIのPrivacy表示（#123の「README/UIから確認できる」）はこのinventoryを表示元にする前提で、表示自体は未実装です。
