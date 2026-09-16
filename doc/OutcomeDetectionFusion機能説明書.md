# Outcome Detection / Fusion 機能説明書

## 目的

Issue #34 のOutcome Detectionは、`Before Observation + Action + After Observation` から「何が起きたか」を構造化する責務を持つ。成功/失敗の価値判断そのものはここで行わず、検出した事実をIssue #8の評価プリミティブへ渡す。

標準経路は常時LLM/VLMへ依存しない。StateDelta / ScreenDiff / Temporal / Terminal textを軽量detectorで別々に検出し、deterministic fusionで統合する。Semantic Providerは低信頼・abstention時だけfallbackとして呼び出す。

```text
Before Observation + Action + After Observation
  -> TerminalTextDetector
  -> StateDeltaDetector
  -> ScreenDiffDetector
  -> TemporalChangeDetector (optional follow-up observations)
  -> DeterministicOutcomeFusion
  -> OutcomeEvent
  -> PrimitiveEvaluator.evaluate_event()
```

## OutcomeEvidence

`OutcomeEvidence` はdetectorごとの根拠を失わず保持する。

- `detector`: detector名
- `signal`: `terminal` / `state_delta` / `screen_diff` / `temporal_change` / `semantic`
- `value`: detectorの観測結果
- `confidence`: 観測自体の確信度
- `reliability`: detector経路の既定信頼度
- `provenance`: detector/versionまたはSemantic Provider名
- `details`: changed field、visual distance、matched keyword等

Fusion後もEvidenceを消さないため、最終statusだけではなく、どのdetectorが何を見たかを追跡できる。

## OutcomeEvent

`OutcomeEvent` schemaは `outcome-event/v1`。

statusは以下を使う。

- `success`: terminal success evidence
- `failure`: terminal failure evidence
- `changed`: 状態変化を検出したが価値は未判定
- `unchanged`: 状態が維持された
- `unknown`: Evidence不足またはconflict

さらに以下を保持する。

- `action_id`
- `confidence`
- `conflict`
- `abstained`
- `semantic_fallback_used`
- `reason`
- 元の `evidence[]`

既存の `OutcomeAssessment` へは互換変換でき、`changed` / `unchanged` は `ongoing` としてDecision Contextへ渡す。

## StateDelta

`StateDeltaDetector` は画面差分とは独立して、以下を比較する。

- `features["state"]` のscalar field
- `score / progress / level / hp / health / lives / resources / currency` 等のgeneric numeric feature
- OCR text集合
- detected UI elementのID/type/text

明示的なstructured field変更を最も強い根拠とし、OCR/UI elementだけの変更は低めのconfidence/reliabilityで扱う。

`progress_delta` / `resource_delta` がgeneric featureから得られる場合はEvidence detailsへ残す。これはDetection時点では価値scoreへ変換せず、`PrimitiveEvaluator` が利用する。

## ScreenDiff

`ScreenDiffDetector` はpixel系変化をStateDeltaとは別Evidenceとして保持する。

優先順位:

1. `perceptual_hash` のHamming distance
2. raw `signature` の一致/不一致
3. 利用不能なら `unknown`

raw signature不一致はanimationや描画揺れでも発生するため低reliabilityとする。画面変化単独をprogress/successとして扱わない。

## Temporal filter

`TemporalChangeDetector` は追加follow-up observationが与えられた場合のみ、After状態が持続しているかを判定する。

- `persistent`: follow-upがAfter側に近い
- `transient`: follow-upがBefore側へ戻る
- `ambiguous`: どちらにも安定しない
- `unverified`: follow-upなし

通常Decision loopでは余計なcaptureを強制しない。追加captureを行えるE2E/sceneではtemporal observationを渡すことでanimationや一時OCR揺れを抑制できる。

## Deterministic Fusion

FusionはEvidenceの `confidence * reliability` をsupportとして利用する。

優先規則:

1. success/failure terminal evidence
2. strong structured StateDelta
3. persistent visual change
4. stable structured state + stable/transient visual evidence
5. insufficient/conflictingなら `unknown + abstained`

successとfailureのterminal evidenceが同時に存在する場合は一方を勝手に選ばず `conflict=true` の `unknown` とする。

ScreenDiffがchangedでもstructured stateがstableでTemporalもtransient/stableなら、背景animationとして `unchanged` に寄せる。逆にstructured stateまたはpersistent temporal evidenceが強ければ `changed` とする。

## Semantic fallback

`OutcomeDetector` へ `assess_outcome(observation, previous)` を持つProviderを任意接続できる。

Semantic Providerを呼ぶ条件:

- deterministic fusionがabstainした
- またはconfidenceが設定threshold未満

terminal keyword等で十分な確信がある場合はSemantic Providerを呼ばない。Ollama Providerを利用する場合でも、Outcome判定のたびに常時LLM requestを発生させない。

Semantic結果も `OutcomeEvidence(signal="semantic")` として追加し、元のdeterministic evidenceを保持したまま再Fusionする。

## Evaluationとの責務分離

`OutcomeEvent.to_evaluation_signals()` は検出済みのgeneric signalだけを返す。

- `changed` -> novelty signal
- `unchanged` -> repetition signal
- generic progress featureの差 -> progress_delta
- generic resource featureの差 -> resource_delta

`PrimitiveEvaluator.evaluate_event()` がこれをIssue #8の評価軸へ変換する。

したがってOutcome Detectionは「変化した」「terminal keywordが出た」「resource値が減った」という事実を扱い、「その変化が良いか悪いか」の最終価値判断はEvaluation側に残る。

## Engine統合

`GamePlayerEngine` は直前のObservationだけでなく直前の `action_id` を保持する。

次stepで:

```text
previous observation
previous selected action_id
current observation
```

を `OutcomeDetector.detect()` へ渡し、生成された `OutcomeEvent` を `last_outcome_event` に保持する。

Decision Context互換のため、前回Outcomeは `OutcomeEvent.to_assessment()` を通して渡す。

## 将来拡張

初期実装では重い学習系を必須にしない。以下はExperienceが蓄積してから比較する。

- detector別reliability calibration
- Bayesian evidence fusion
- Dempster-Shafer conflict/unknown表現
- HMM / state-space smoothing
- change point detection
- conformal confidence
- transition embedding / similarity retrieval
- world-model-lite prediction residual
- scene別Mixture-of-Experts gating

いずれを追加しても、元Evidence・provenance・conflict・abstention理由は保持する。
