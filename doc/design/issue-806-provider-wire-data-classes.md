Spec-Status: READY
Depends-On: #224（Privacy Data Inventory、merge済み）
Decision-Gaps: DG-3（非ブロッキング。範囲外・残件を参照）
Parent: #123（兄弟: #226 Remote Consent Enforcement、後続）

# Issue #806 Provider wire schemaのdata class列挙へ`ui_metadata`・`model_output`を追加し、送信payloadを全classへ対応付ける

## 成果物

| file | 内容 |
|---|---|
| `schemas/inference_provider/v1/inference_request.schema.json`、`provider_manifest.schema.json` | `privacy.data_classes` / `privacy.declared_data_classes`の`enum`へ`ui_metadata`・`model_output`を追加（v1の互換拡張） |
| `src/ai_game_player/assets/privacy/data_inventory.json` | `OllamaProvider._request_decision`のtransmitterへ`model_output`を追加し、noteを実payloadの対応に合わせる |
| `tests/test_provider_wire_data_classes.py`（新規） | 実payloadのfield→class対応。未分類fieldで失敗 |
| `tests/test_data_inventory.py` | 既存の「wire enum ⊆ remote」を「wire enum == `remote_eligible_classes()`」へ強化し、transmittersの全classがenumに含まれることを追加 |
| `doc/architecture/inference_provider_api.md`、`privacy_data_inventory.md`、`doc/design/issue-224-data-inventory.md`（範囲外節）、機能説明書、`doc/versions.md` | 列挙・対応表・DG-2解決を記録 |

## 入出力

- 列挙（v1の送信data class）: `decision_context`, `ocr_text`, `cropped_image`, `raw_frame`, `history`, `trace`, **`ui_metadata`, `model_output`**。inventoryで`remote: opt_in`の8 classと一致させる（`never`のclassは列挙に入れない）。
- 対応付けの粒度: OllamaProviderがHTTP bodyへ入れる`prompt`（JSON文字列）を復号し、全leafのpath（`$`相対、list indexは`[]`、任意key名は`*`）を規則表で1 classへ解決する。規則は完全一致、または末尾`.**`の部分木。複数規則に一致する場合は最長一致。
- 規則表（`tests/test_provider_wire_data_classes.py`内の契約。表記: 実装のpathは`/`区切り（例`decision_context/candidates/[]/evaluation/**`）で、下表ではpathを`.`区切りで書き、セル内の`/`は同階層の別keyの列挙。`NON_DATA`は由来が画面・プレイ内容でない固定のprotocol定数または実行設定で、利用者のdata classではない）:

| path | class | 根拠 |
|---|---|---|
| body: `model` / `stream` / `format` | NON_DATA | `model`はProvider構築時の実行設定（testで`OllamaProvider.model`と一致を確認）、`stream`/`format`はコード内の固定値。画面・プレイ由来の値は入らない |
| `instruction` | NON_DATA | コード内の固定文 |
| `personality`、`purpose` | decision_context | 判断に与える利用者設定の目的・方針（Decision Contextの`goal`と同種） |
| `observation.screen_id/width/height`、`observation.features.**`、`previous.*` 同上 | ui_metadata | UI識別子・寸法・特徴量 |
| `observation.ocr_text`、`previous.ocr_text` | ocr_text | 画面由来の文字列 |
| `allowed_actions[].**` | ui_metadata | 候補の種類・座標・bbox・label（UI要素情報） |
| `decision_context.schema/snapshot_id/allowed_action_ids/goal.**/uncertainty[]` | decision_context | 判断snapshotの参照・目標 |
| `decision_context.state.visible_text[]` | ocr_text | OCR抜粋 |
| `decision_context.state.**`（その他） | ui_metadata | 画面id・寸法・署名・hash・明るさ・要素数 |
| `decision_context.candidates[].action_id/kind/label/recognition_confidence/allowed/uncertainty[]` | ui_metadata | 候補のUI情報と認識確信度 |
| `decision_context.candidates[].evaluation.**` | model_output | 評価器の点数・理由（Provider/評価器出力） |
| `decision_context.candidates[].knowledge[].**` | knowledge | 候補に添える参考知識の抜粋（DG-3） |
| `decision_context.previous_outcome.**` | model_output | 直前操作の結果評価（OutcomeEvaluator/Ollama出力） |
| `decision_context.recent_history[].**` | history | 実行済み操作の履歴 |
| `decision_context.knowledge_projection.**` | knowledge | 明示`expose_to_models`時のみ。既定では含まれない。圧縮したsoft evidence（`priors`等は空に落とす`to_model_dict`）で保存本体ではない。DG-3の対象 |
| `decision_context.evaluation_projection.**` | model_output | 同上 |

- 送信箇所→class（inventory `transmitters`）: `assess_outcome` = {ocr_text, ui_metadata}、`_request_decision` = {decision_context, ocr_text, ui_metadata, history, model_output}、`list_models` = {}（GET、body無し）。testは実payloadから導いたclass集合とinventoryの宣言が**等しい**ことを要求する（過少・過大宣言のどちらも失敗）。
- 失敗契約: 規則表に一致しないleafが1つでもあれば、test失敗（メッセージに未分類pathを列挙）。規則表にあるが実payloadでどの捕捉にも現れない規則は、任意項目（projection）を除きstaleとして失敗。
- 既定は送信しない: 変更はスキーマ列挙・宣言・テストだけで、送信条件（既定endpointはloopback、`remote_transmission_allowed`の判定は#226）を変えない。class側の`remote`は全て`opt_in`のままで、既定で送信するclassはない。

## 手順

1. 版の判断: **v1の互換拡張**とする。互換性は2つに分けて扱う。(i) 既存のmanifest・requestは新schemaでも妥当（後方互換）。(ii) 新classを含むrequest/manifestを旧v1 schemaで検証すると拒否される（前方非互換）。根拠: (a) 列挙の追加であり、(i)を満たす。(b) Provider実装・HTTP clientは未実装（#238-#240）で、旧v1 schemaを固定して検証する外部実装は存在しない。(c) (ii)は旧v1検証器が未知classを受け付けないfail-closed側の差で、安全側。外部に旧schemaを固定する利用者は確認できていない（未実装のため）。(d) 版を上げると`kadoka.inference.request/1`の`const`・`$id`・examples・CI・docが全て連動し、受入条件の範囲を超える。`schema`文字列は変更しない。契約文書へ「列挙は追加のみ。削除・意味変更は版を上げる」と明記する。
2. 規則表とinventoryの一致: 実payloadの捕捉は`urlopen`をpatchして`Request.data`を取得し、`OllamaProvider.choose`（旧、観測と候補）・`choose_context`（実`DecisionContextBuilder`で履歴・評価を含む）・`assess_outcome`・`list_models`を実行する。`prompt`は外側JSON→内側JSONの順に復号する。
3. 任意projection（`knowledge_projection`/`evaluation_projection`）は既定で含まれない。含まれる形の合成payloadを分類関数へ直接渡す単体testで、規則が解決することと、既定payloadには出現しないことを確認する。
4. 列挙testの強化: 両schemaのenumが`remote_eligible_classes()`と等しく、`transmitters`の全classと`effective_classes`がenumの部分集合であること。
5. 責務境界: 本Issueは宣言・schema・testだけ。runtimeの送信/拒否（consent判定）、field規則のruntime利用、audit eventは#226。

## テスト

| 受入条件 | 試験 |
|---|---|
| enumに2 classが入り`remote_eligible_classes()`と一致 | `test_wire_enums_equal_remote_eligible_classes`（両schema） |
| transmittersとテストが一致 | `test_declared_transmitter_classes_match_real_payloads`（site別にclass集合が等しい） |
| OllamaProvider送信3箇所の実payloadを全classへ | `test_choose_context_payload_is_fully_classified`、`test_legacy_choose_payload_is_fully_classified`、`test_assess_outcome_payload_is_fully_classified`、`test_list_models_sends_no_body` |
| 未分類fieldでテスト失敗 | `test_unclassified_field_is_reported`（未知keyを足した合成payloadで失敗を確認）、`test_stale_rule_is_reported` |
| 既定は送信しない | `test_projection_fields_absent_by_default`、既存`test_1_0_default_table_is_pinned`（remoteは全てopt_in） |
| 例とschema | contract-assets CIの既存検証（examples妥当性）、`test_new_classes_validate_against_schema`（jsonschema有時） |

## 完了条件

- 上表の試験と既存の`tests/test_data_inventory.py`が通る。ruff / mypy strict（新規なし、tests）/ finish_task相当が通る。
- runtimeの送信payload・挙動が変わらない（`src`の実行コードは無変更、inventory JSONとschemaだけ）。
- 機能説明書とinventory資料に列挙・対応表・DG-2の解決が記録されている。

## 現状調査

- `OllamaProvider`の送信は3 site: `_request_decision`（`choose`/`choose_context`共通）、`assess_outcome`、`list_models`（GET）。bodyは`{model, stream, format, prompt}`で、`prompt`は`json.dumps(context)`。
- inventoryは#224で`assess_outcome`に`ui_metadata`を、`_request_decision`に`ui_metadata`を既に宣言していたが、wire enumに無いため`remote_eligible_classes()`の部分集合testが成立していなかっただけ（enum側が小さい）。`model_output`は送信siteに未宣言だったが、`previous_outcome`と`candidates[].evaluation`が実payloadに含まれる。
- 再利用: `DataInventory.remote_eligible_classes()`、`effective_classes()`、`DecisionContextBuilder`、既存の`FakeResponse`型patch方式（`tests/test_provider_decision.py`）。

## 代替案と不採用理由

- 版を`/2`へ上げる: 上記(d)のとおり影響が大きく、旧schemaの利用者もいない。不採用。
- `ui_metadata`/`model_output`を`decision_context`へ統合し列挙を増やさない: #224の分類（機微度・保存TTLが異なる）を壊し、#226の同意粒度が粗くなる。不採用。
- 規則表をinventory JSONへ置く: loaderとJSON Schemaの変更（相互参照検証）が必要で、runtime consumerも未実装。まずtestの契約として固定し、#226で必要になった時点で昇格する。
- `decision_context`の`contains`へ`model_output`を足す: 内包を暗黙に広げると同意範囲が曖昧になる。送信siteへ明示する方が#226の判定に正確。不採用。

## 範囲外・残件

- #226: 送信前のconsent照合、未許可class拒否、規則表のruntime利用、audit event。
- DG-3: `candidates[].knowledge[]`と明示opt-inの`knowledge_projection`は実際にモデル要求へ入りうる。inventoryの`knowledge` classは`remote: never`なので、テストでは`decision_context`へ寄せず**`knowledge`として分類**し（CodeRabbit指摘を採用）、「remote:neverなのに現行payloadへ入る」既知の差分`KNOWN_REMOTE_GAP={knowledge}`として固定した。`test_data_inventory`の不変条件（transmitterは全てremote-eligible）のため`knowledge`のtransmitter宣言はしない。本Issueは現行の送信挙動を変えない。送信を止めるか同意対象に含めるかは#226が決め、その時にKNOWN_REMOTE_GAPを空にする。
- `expose_to_models`が真のknowledge/evaluation projectionの送信は、既存の明示opt-inのまま。
