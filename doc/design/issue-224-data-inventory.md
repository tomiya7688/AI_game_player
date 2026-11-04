# Issue #224 設計: Privacy Data Inventory / Defaults

- Spec-Status: READY
- Depends-On: #164（Retention Core、merge済み。`RetentionPolicy.ttl_ms_by_type`へ既定TTLを渡すだけ）
- Decision-Gaps:
  - DG-1（owner確認、非ブロッキング）: class別の既定TTL値（raw_frame/cropped_image/unclassified 1日、ocr_text/decision_context/video/replay 7日、crash_diagnostic/runtime_log 14日、その他local_bounded 30日）は本設計の提案値。現行runtimeには適用しない（適用は#225）ため実装は進めるが、#225で強制する前にownerが値を確定する。
  - DG-2（#226で決定、非ブロッキング）: Provider wire schema v1の`data_classes`列挙は`ui_metadata`・`model_output`を持たない。`OllamaProvider.assess_outcome`/旧`choose()`が送るfeaturesを宣言できない。schema拡張かclass統合かは#226で決める。本Issueではschemaを変更しない。
- Parent: #123（Privacy Parent: Data Inventory / Retention / Remote Consent）。兄弟: #225 Retention/Delete/Export、#226 Remote Consent Enforcement。

## 成果物

| file | 内容 |
|---|---|
| `src/ai_game_player/assets/privacy/data_inventory.json` | inventory正本（`schema_version: 1`、`policy_version: "1.0"`）。package-dataとWindows bundle（assets丸ごと）に含まれる |
| `config/data_inventory.schema.json` | 形のJSON Schema（draft 2020-12） |
| `src/ai_game_player/data_class_policy.py` | `DataClassPolicy`（1 classの値と単独不変条件）・`DataClassPolicyError` |
| `src/ai_game_player/data_inventory.py` | `load_data_inventory()`・`DataInventory`（相互参照検証と問合せ）・`DataInventoryError` |
| `tests/test_data_inventory.py` | 既定値・検証・問合せ・coverage（未分類の保存/送信の検出） |
| `doc/architecture/privacy_data_inventory.md` | 利用者・実装者向け説明。`doc/versions.md`ほか入口を更新 |

## 入出力

- 入力: inventory JSON。256KiB上限の通常fileのみ（`read_regular_file`）。UTF-8。JSON objectの重複keyは拒否。
- top-level（全て必須、追加キー禁止）: `schema_version`(=1), `policy_version`(非空), `classes`[], `artifact_types`{型→class}, `writers`[], `transmitters`[], `non_data_paths`{path名→理由}, `forbidden_field_names`[]。
- class: `id`(`^[a-z][a-z0-9_]*$`), `sensitivity`(low<medium<high<forbidden), `default_persistence`(never/off/transient/local_bounded/local), `default_retention_ms`(正整数ms|null), `remote`(never/opt_in), `contains`[class], `owner_modules`[`ai_game_player.*`], `implemented`(bool), `description`。
- writer: `site`(`ai_game_player.module:最上位qualname`), `data_class`(class|null), `paths`[], `note`（null時必須）。transmitter: `site`(`module:qualname`), `data_classes`[文字列class], `note`。
- 出力（問合せ）: `policy(id)`、`class_for_artifact_type(type)`、`effective_classes(ids)`（contains閉包）、`remote_eligible_classes()`、`ttl_ms_by_artifact_type()`、`forbidden_field_paths(payload)`（`$.a.b[0]`形式、sorted tuple）。
- エラー: 全て`DataInventoryError`（ValueError）。単独値の誤りは`DataClassPolicyError`を包む。未登録のclass・artifact型は推測せず拒否。

## 手順

1. 読込: 上限付き読取→重複key拒否JSON解析→`from_dict`。部分結果を返さない（fail-closed）。
2. class単独検証（`DataClassPolicy.__post_init__`）: 列挙値・ID・module名。forbidden⇒never/never、high⇒`local`禁止、`local_bounded`⇒TTL必須、never/transient/local⇒TTLなし、implemented⇒owner必須。
3. 相互検証（`_parse_classes`ほか）: ID重複、contains未登録参照、外側classが内包classより低機微度、`opt_in`が`never`を内包、間接循環、writer/transmitterのsite重複・未登録class、path重複、persisted pathとnon-data pathの重複、forbidden class不在を拒否。
4. 責務境界: 本Issueは「宣言と問合せ」だけ。保存・送信・削除の強制はしない（#225/#226）。runtime経路へは接続しない。
5. `forbidden_field_paths`はfield名のみ判定（小文字化・`-`→`_`、完全一致または`_禁止名`接尾一致のfail-closed）。同じcontainerは1回だけ辿り循環objectでも停止。

## テスト

| 完了条件 | 試験 |
|---|---|
| inventory schemaがある | `test_canonical_document_matches_schema`（jsonschema有時） |
| 1.0 defaultを表現できる | `test_1_0_defaults_are_local_first`（raw/crop/replay/video OFF、trace local_bounded+TTL、high⇒非local） |
| credential禁止classがある | `test_issue_scope_classes_and_credential_class_are_defined`、`test_forbidden_field_paths_find_nested_credentials` |
| componentがpolicyをqueryできる | `test_policy_queries`、`test_retention_ttl_defaults_feed_retention_policy` |
| validation testがある | `DataInventoryValidationTest`（版・未知キー・未登録参照・非privacy既定・内包矛盾・循環・重複・JSON重複key・非文字列class・読込失敗） |
| 保存/送信の未分類を検出 | `DataInventoryCoverageTest`（src AST走査: writer・transmitter・path名・artifact型、逆向きstale） |
| #226との整合 | `test_provider_wire_data_classes_are_remote_eligible` |

## 完了条件

- 上表の試験が全て通る。ruff/mypy strict（新規module）/finish_task相当の検査が通る（Planner時間依存testの負荷flakeは#729として別記録）。
- runtimeの保存・送信挙動が変わらない（既存test無変更で通る）。
- DG-1/DG-2がPRとIssueに記録されている。

## 現状調査（2026-11-04 main時点の棚卸し）

- 保存（33 site）: game directory配下の`history.json`(history)・`decision_trace.json`(trace)・`knowledge.json`(knowledge)・`execution_history.json`(history)・`action_safety.json`/`safety_guard.jsonl`/`fail_safe/`(safety_audit)・`session_events/*.sqlite3`(trace)・`light_adaptation/`(knowledge)・Knowledge promotionの`reports/`/`state.json`(challenger_artifact)。`data/config.json`・`data/shell_state.json`(app_config)、`user_data/output/log/ai_game_player.jsonl`(runtime_log)。呼出側pathのUI認識/埋め込みmemory(ui_metadata)・EvaluationLog(model_output)・campaign/E2E/runtime mode(trace)・worker診断/bundle self-check(crash_diagnostic)・品質report。基盤（class呼出側依存）: Artifact Store・原子的JSON・lock・Retention索引・journal移行marker。planning worker一時IPC(worker_ipc)。
- frame画素・crop・録画・replayを保存するコードはない。telemetry送信もない。
- 送信（3 site、全て`OllamaProvider`）: `_request_decision`（Decision Context＝state要約・OCR抜粋・候補・評価・履歴、旧`choose()`はOCR全文・features）、`assess_outcome`（OCR全文・features）、`list_models`（GETのみ）。既定endpointはloopbackだが利用者がremoteへ変更可能。raw frame送信はない。
- artifact型: src使用は`knowledge.snapshot`/`knowledge.evaluation`/`knowledge.light_validation`/既定`binary.payload`。test/設計で使う`frame.raw`/`frame.crop`/`video.segment`/`model.delta`/`model.adapter`/`ocr.model_delta`も登録。
- 再利用: `bounded_file.read_regular_file`、Provider wire schemaの既存class名、`RetentionPolicy`。

## 代替案と不採用理由

- Python定数で正本を持つ: 言語非依存でなく、#661 Rust Coreで再実装しにくい。JSON正本+Python loaderを採用。
- `config/`に正本を置く: 配布物に`config/`が含まれず、runtimeが問合せできない。package assetsへ置き、schemaだけ`config/`。
- 文字列grepで未分類検出: コメント・docstringの誤検出と書式差に弱い。AST走査を採用。
- runtimeで既定を強制（例: trace TTL適用）: 本Issueの非対象（retention engine）で挙動変更を伴うため不採用（#225）。
- Provider schema列挙へ`ui_metadata`等を追加: 共有wire契約の変更で#226の判断事項（DG-2）。

## 範囲外・残件

- #225: 既定TTL・削除・exportを各storeへ適用（DG-1の値確定後）。orphan等はRetention Core側。
- #226: 送信前のconsent照合（`effective_classes`で内包classも同意対象）、未許可class・credential（`forbidden_field_paths`）拒否、retryでのscope拡大禁止、audit event、DG-2。
- #123: README/UIのPrivacy表示（本inventoryを表示元にする）。#201 Session Video、#180 Learning Runtimeは同じpolicyを参照する。
