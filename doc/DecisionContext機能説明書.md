# Decision Context機能説明書

## 目的

Recognition / Evaluation / Decision を分離し、小型Decision LLMへraw認識結果ではなく、候補単位に整理した `DecisionContext` を渡します。

```text
Recognition sources
 -> Screen State + ActionCandidate[]
 -> candidate evaluators + Knowledge
 -> deterministic fusion / conflict / uncertainty
 -> DecisionContext
 -> Decision Provider
```

## DecisionContext

`decision-context/v1` は1 stepの同一snapshotを `snapshot_id` で識別します。

- `state`: screen_id、画面サイズ、signature、perceptual hash、短いvisible text、検出数だけを保持します。raw `features` はDecision LLMへ渡しません。
- `candidates`: action_id / kind / label、recognition confidence、評価結果、Knowledge、uncertaintyを候補ごとに保持します。座標はDecision LLMへ渡さず、LLMは候補IDだけを選びます。
- `allowed_action_ids`: Safety評価後に実行可能な候補IDです。
- `previous_outcome`: 直前行動後のsuccess/failure/ongoing/unknown、confidence、state_changedを保持します。
- `recent_history`: 直近snapshot / action / outcomeを上限付きで保持します。
- `goal`: `current_goal` と `short_term_goal` を持ち、将来のgoal hierarchyへ拡張できます。

## Evaluation Fusion

各Evaluatorは `score` と `confidence` と `reliability` を別々に返します。

`EvaluationFusion` は `confidence * reliability` を重みとして決定論的にscoreを統合します。一定以上の正評価と負評価が同時に存在すると `evaluator_conflict` を保持し、どちらかへ潰しません。

recognition confidenceは「そのUI候補を認識できている確からしさ」であり、action utilityとは別fieldです。

初期Evaluatorは以下です。

- `SafetyContextEvaluator`: unsupported / dangerous / outside-screen等を評価します。
- `RepetitionContextEvaluator`: 直近履歴、previous outcome、state_changedを使い、進展のない反復行動へpenaltyを与えます。

## Knowledge

`CandidateKnowledgeRetriever` は候補label / action_idで `KnowledgeStore` を検索し、候補ごとに最大件数を制限して付与します。

各Knowledge evidenceは `evidence_id / source / confidence / provenance / category` を保持します。全Knowledgeやrawログを無制限には入れません。

## Providerとの境界

標準 `RuleProvider` と `OllamaProvider` は `choose_context()` を実装します。

OllamaのDecision requestには `DecisionContext` だけを渡し、raw `ScreenObservation.features` は渡しません。返却action_idは `allowed_action_ids` 内であることを再検証します。

既存の `RuleProvider` 派生で旧 `choose()` だけをoverrideしている拡張は従来経路を維持します。

## OllamaProviderが送るdata class（Issue #806）

`OllamaProvider`が設定endpointへ送るJSONのfieldは、Privacy Data Inventoryのdata classへ全て対応付けています。`choose_context()`は、画面要約・候補ID/ラベル（ui_metadata）、OCR抜粋（ocr_text）、目標・参照ID（decision_context）、候補の参考知識（knowledge）、候補の評価と直前の結果評価（model_output）、直近の実行履歴（history）です。`assess_outcome()`と旧`choose()`はOCR全文（ocr_text）と特徴量・候補（ui_metadata）などです。`model`・`stream`・`format`・固定の指示文は利用者dataではありません。

Provider wire schema v1の`privacy.data_classes`列挙に`ui_metadata`と`model_output`を追加しました（互換拡張）。新しい送信fieldを足すとfield→class対応のtestが失敗するため、同じ変更でclassを決めてください。`knowledge`は保存本体の方針が`remote: never`ですが、候補の参考知識として現行payloadに入るため、inventoryには事実どおり宣言し、既知の差分（`KNOWN_REMOTE_GAP`）として #226 で解消します。その他のclassは既定で送信されることはなく（`remote: opt_in`）、既定endpointはloopbackです。同意に基づく送信の許可・拒否は #226 で実装します。

## 短期履歴とTrace

`DecisionTraceStore` は以下を同一snapshotへ紐付けて保存します。

- snapshot_id
- compact state
- candidate evaluation / conflict / uncertainty
- candidate Knowledge evidence
- previous outcome
- ActionDecision
- Decisionが参照したKnowledge evidence ID

これによりDecision理由から評価・Knowledge・履歴へ追跡できます。

同じ状態で直前と同じActionを実行して進展がなかった場合、標準RuleProviderではrepetition penaltyにより別候補を優先できるため、LoopGuardで停止する前に単純な復帰判断が可能です。

## 非対象

- Decision Context自身が最終Actionを決定すること
- Outcome検出の高度化
- 完全なPlanner / RL / world model
- 全Knowledgeやraw recognitionの直接投入

将来はEvaluator追加、Experience検索、Goal hierarchy、learned policyをDecision Provider交換で拡張します。
