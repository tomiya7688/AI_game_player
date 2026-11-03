# Processing Hook Runtime：stage境界のread/modify/inject

[動作と配置の見取り図へ戻る](README.md)

Issue [#151](https://github.com/tomiya7688/AI_game_player/issues/151)（親 [#58](https://github.com/tomiya7688/AI_game_player/issues/58)）で実装する、Pipelineのstage境界へ[StageEnvelope v1](stage_envelope.md)を渡してHookを実行する仕組みです。Core loopをforkせずに処理を追加できる最小のruntimeで、同じprocess・同じ権限で動きます。権限の宣言と判定は#152、subprocessによる隔離は#235の範囲です。

| 正本 | 役割 |
|---|---|
| `src/ai_game_player/hook_registration.py` | `HookRegistration`（ID・mode・接続点・順序・予算・失敗時方針）、`HookMode`、`HookFailurePolicy` |
| `src/ai_game_player/hook_runtime.py` | `HookRuntime`（接続点の宣言・登録・dispatch・出力の再検証・quarantine） |
| `src/ai_game_player/hook_dispatch_result.py` | `HookDispatchResult`と、Hookごとの`HookExecutionRecord`（status・latency・失敗分類） |
| `src/ai_game_player/hook_envelope.py` | `derive_hook_envelope()`：Hook出力に必要なprovenanceを付けたEnvelopeを作る補助関数 |
| `src/ai_game_player/perception_hook_boundary.py` | 本番経路の接続点。`DecisionPipeline`の候補統合直後（`perception`/`output`） |

## 有効化

`HookRuntime`は既定で無効（`enabled=False`）です。無効なruntimeの`dispatch()`はHookを呼ばず、入力Envelopeをそのまま返します。`DecisionPipeline`は`hook_runtime`引数を指定した場合だけ接続点を作ります。引数を省略した既定の起動経路（`run_ai_game_player.bat`・Session構成）では、接続点もEnvelopeの作成もありません。設定fileやUIからの有効化はまだありません。

## Hookの種類

| mode | callbackの戻り値 | runtimeの扱い |
|---|---|---|
| `read` | `None`だけ | 観測だけ。Envelopeは不変（`payload`・`metadata`は読み取り専用mapping）なので書き換えは例外になり、失敗として記録します。`None`以外を返すと`contract_violation`です |
| `modify` | 新しい`StageEnvelope` 1件 | 後段へ渡すEnvelopeを置き換えます。変えられるのは`payload`・`confidence`・`metadata`と、自身のID・時刻・provenanceだけです。stage・boundary・`payload_schema`・`payload_schema_version`・`frame_id`・`turn_id`・`correlation_id`・`artifact_refs`は入力と同じでなければなりません（Artifact参照は、権限の判定（#152）がないまま他のdataを指せるため固定します）。複数のmodifyは順に連鎖し、各出力の`parent_envelope_id`は直前のEnvelopeを指します |
| `inject` | `StageEnvelope`のlistまたはtuple | 派生Envelopeを`HookDispatchResult.injected`へ追加します。入力IDを`derived_from`に含めます。`safety`・`execution` stageのEnvelopeは作れません |

Hook出力は`derive_hook_envelope()`で作れます。この関数は`producer_kind: "hook"`、`producer_id`/`producer_version`（登録の`hook_id`/`hook_version`）、`parent_envelope_id`と`derived_from`（入力ID）を設定し、`dataclasses.replace()`で作り直して検証します。

## 出力の再検証

runtimeは、Hookが返したobjectを信用しません。`object.__setattr__`で検証後に書き換えたEnvelopeも拒否するため、次の順で作り直します。

1. `StageEnvelope`以外は`contract_violation`。
2. canonical JSONへencodeし、byte数をHookの`max_output_bytes`と比べる（超過は`limit_exceeded`）。
3. `decode_stage_envelope()`で厳密に再検証し、Hookと参照を共有しない新しいinstanceにする（違反は`malformed`等の`StageEnvelopeError.code`）。
4. `producer_kind`が`hook`で、producerが登録値と一致し、`parent_envelope_id`が入力ID、`session_id`・`trace_id`が入力と同じか確認する。同じdispatch内でのEnvelope IDの再利用も拒否する。

1件でも違反があれば、そのHookの出力は全件捨てます（一部だけ採用しない）。

## 実行順序

1つの境界では、`modify`→`read`→`inject`の順に実行します。各modeの中は`priority`（-1000〜1000）の昇順、同じ値は`hook_id`の昇順です。登録順やdictの順には依存しません。`read`は後段へ渡る最終Envelopeを、`inject`は加工後のEnvelopeを受け取ります。

## 予算と失敗時方針

| 予算 | 既定 | 上限 |
|---|---|---|
| `timeout_ns`（1回の呼び出し） | 50ms | 1秒 |
| `max_output_bytes`（出力1件） | 64KiB | 1MiB（Envelope v1の上限） |
| `max_injected`（injectの1回） | 4件 | 16件 |
| `max_injected_total`（1 dispatch、runtime単位） | 16件 | 64件 |

Hookは専用のdaemon threadで呼び、`timeout_ns`まで待ちます。例外（`SystemExit`を含む）はthread内で捕捉し、Coreのthreadへ伝えません。失敗は`exception`・`timeout`・`invalid_output`・`contract_violation`・`capability_denied`（#152の権限判定で拒否）・`quarantined`に分類し、`ai_game_player.hook_runtime` loggerへWARNINGで記録します。logにはHook ID・mode・接続点・分類・code・例外の型名だけを出し、例外の文面やpayloadは出しません。出力の再検証はcanonical JSONのencodeとdecodeを1回ずつ行うため、その分の処理時間がHookを登録した境界にかかります（runtimeが無効、または登録がない境界では行いません）。

失敗したHookの出力は、方針にかかわらず使いません（fail-closed）。その後の扱いは`on_failure`で決めます。

| `on_failure` | 動作 | 既定 |
|---|---|---|
| `skip` | 失敗を記録し、直前のEnvelopeのまま残りのHookと後段を続ける | `read`・`inject` |
| `halt` | 残りのHookを呼ばず`halted=True`を返す。呼び出し側は後段へ進まず、入力なしで止まる | `modify`（加工を前提とする後段へ、未加工のdataを黙って渡さないため） |

timeoutしたHookは、その時点でquarantine（以後呼ばない）にします。Pythonのthreadは外から止められず、同じHookを再び呼ぶと実行中のthreadが増えるためです。例外・不正出力は`max_consecutive_failures`（既定3）回連続でquarantineにし、成功すると回数を0に戻します。quarantineされた`halt`方針のHookは、以後のdispatchを毎回`halted`にします（必須のHookが動かない状態で後段へ進まないため）。復帰するには`unregister()`で外し、原因を直してから登録し直します。timeoutで残るthreadは、quarantineにより1つのHook登録につき最大1本です。

`HookExecutionRecord.elapsed_ns`はHookごとの所要時間、`HookDispatchResult.elapsed_ns`はdispatch全体の所要時間です（どちらも単調時計のns）。

## Safetyとの関係

- runtimeはHookへEnvelopeだけを渡します。入力executor・座標・Safety評価器への参照は渡しません。
- `safety`・`execution`の接続点は`read`だけを宣言できます。どのHookも`safety`・`execution` stageのEnvelopeを作れません。
- Hookの失敗による停止は、入力を行わない保留（`DecisionWithheld`、`PlanningOutcome.ABSTAIN`）です。実入力の経路は、従来どおりDecision VerifierとAction Safety Evaluatorを通ります。

## 本番経路の接続点：perception/output

`DecisionPipeline._read_candidates()`は、OCR・画像候補を設定済み候補へ統合した直後に`PerceptionHookBoundary.run()`を呼びます。この接続点は`read`と`inject`だけを許可し、`modify`の登録は拒否します。Hookの出力は候補・判断・Safety・実行へ戻さず、`pipeline.perception_hooks.last_result`の診断として保持するだけです。

| payload（`kadoka.perception.candidates` v1） | 内容 |
|---|---|
| `screen_id`・`width`・`height` | 画面ID（256文字まで）と画面の大きさ |
| `candidate_count` | 統合後の候補数 |
| `candidates` | 先頭64件の`action_id`・`kind`・`label`（各256文字まで）・`confidence`・`dangerous` |

座標（`x`・`y`）、矩形（`bbox`）、保持時間はpayloadに含めません。`session_id`はSession Journalがあればその値、なければ接続点ごとに生成したIDです。`halt`方針のHookが失敗すると、`_read_candidates()`は`hook_halted`の保留を送出し、provider・Safety・入力のいずれも呼びません。Envelopeを作れない場合（例：対のないsurrogateを含む画面ID、文字列でない候補label。`StageEnvelopeError`・`TypeError`・`ValueError`）は、`halt`方針のHookがあれば`hook_envelope_unavailable`の保留、なければWARNINGを残してHookを呼ばずに続けます。

## 隔離しないもの

このruntimeは同じprocess・同じ権限でHookを実行します。次のものは隔離しません。

- timeoutしたHookのthread：止められず、終わるまで動き続けます（結果は捨てます）。
- CPU・memory：上限はありません。
- file・network・OS入力API・Core内部object：Pythonのimportで直接使えます。runtimeはそれらへの参照を渡さないだけで、利用を防げません。

信頼できないPluginを動かす前提ではありません。権限の宣言と判定は#152（[Plugin Capability Manifest](plugin_capability_manifest.md)。同じprocess内の判定でsandboxではない）、subprocess/IPCによる隔離とcapabilityの強制は#235の範囲です。

## 権限の判定（#152）

`HookRuntime(access_policy=...)`に[Plugin Capability Manifest](plugin_capability_manifest.md)の`PluginCapabilityPolicy`を渡すと、登録時（`authorize_registration`）、Hookを呼ぶ前の入力Envelope（`authorize_input`）、再検証後の各出力Envelope（`authorize_output`）で、Manifestの宣言と照合します。宣言のないHookは登録しません（deny-by-default）。入力の拒否ではHookを呼ばず、出力の拒否ではその出力を全件捨て、どちらも`failure_kind: "capability_denied"`と拒否codeを`error_code`へ記録します。判定が例外で失敗した場合も拒否として扱います（`policy_error`）。

`HookRuntime(authorizer=...)`に渡した関数は、従来どおり`register()`のたびに`HookRegistration`を受け取ります。例外を送出すると、そのHookは登録されません。`access_policy`・`authorizer`の既定（`None`）は判定をしません。権限の確認があることを意味しません。この判定は同じprocess内のpolicy checkで、下の「隔離しないもの」は変わりません。

## 未実装（#151の残り）

- `state`・`reasoning`・`evaluation`等の他の境界への接続と、`modify`を後段で使う境界。
- inject結果の次stageへの再投入（現在は診断として保持するだけ）。
- 設定fileやUIからの有効化と、Hook dispatch結果のSession Journalへの記録（#153 Record/Replayと合わせて設計）。
- #432 Flow Snapshotに従った順序の構成。
