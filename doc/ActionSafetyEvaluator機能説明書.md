# Action Safety Evaluator 機能説明書

## 目的

`ActionSafetyEvaluator` は、Decisionが選んだ `ActionCandidate` を実行前に独立評価し、その候補を実行候補として信用してよいかを `SAFE / SUSPICIOUS / BLOCK` で返す。

Recognition confidence、Action utility、Action safetyは別の値として扱う。認識confidenceが低いことだけで危険Actionとは判定せず、逆にutilityが高くても不可逆・高リスクならSafety側で警告できる。

## 責務境界

```text
Decision candidate
 -> Action Safety Evaluator (#22)
 -> SafetyGuard (#33, deterministic hard stop)
 -> Executor
```

#22は意味的リスク、異常性、goal/context整合性、追加verification要求を担当する。OS対象window、危険キーdeny-list、rate limit、Emergency Stop等の最終決定論的hard boundaryは#33へ残す。

## ActionSafetyResult

各評価は `action-safety/v1` schemaとして以下を保持する。

- `status`: `SAFE / SUSPICIOUS / BLOCK`
- `recognition_confidence`: 認識由来confidence
- `safety_score`: 安全評価score
- `risk_score / risk_level`
- `reversible`
- `blast_radius`
- `target_scope`: `local / game / session / system`
- `goal_alignment`
- `expected_effect_consistent`
- `requires_verification`
- `verification_requests`
- `upstream_anomaly`
- 判定ごとの `SafetyEvidence`

これにより、認識confidenceやDecision utilityへSafety情報を潰さずprovenanceを維持する。

## 初期rule

初期実装は学習モデルではなくrule/invariantを優先する。

- click/double-clickの座標欠落、画面外座標、画面外bbox: `BLOCK`
- `dangerous=True`: `BLOCK`
- save/data削除・上書き: 高リスク `SUSPICIOUS`
- game終了/restart/reset: session高リスク `SUSPICIOUS`
- purchase/resource commitを示す語: `SUSPICIOUS`
- OSへ波及し得るkey表現: system scope `SUSPICIOUS`
- bboxとclick座標の不整合: 再認識要求
- current goalと高リスクActionの不整合: goal verification要求
- expected transition/effectとの明示的不整合: effect verification要求
- 高utilityかつ高risk: upstream evaluation anomalyとして記録

低recognition confidenceはSafetyとは分離してinfo evidenceとして保持する。ただし高リスクActionと同時に発生した場合は再認識を要求する。

## Selective / abstain設計

二値SAFE/BLOCKへ無理に落とさず、意味的に不確かな高リスク候補は `SUSPICIOUS` としてabstainする。

`verification_requests` には、例として以下を入れる。

- `reobserve_target`
- `verify_goal_alignment`
- `verify_expected_effect`
- `confirm_irreversible_action`
- `verify_target_scope`
- `independent_safety_evidence`
- `recheck_upstream_evaluation`

重いVerifierやensembleは常時実行せず、SUSPICIOUS時のみ将来#13/#43経由で追加可能な境界とする。

## Pipeline連携

`DecisionPipeline.run_and_execute()` はDecisionに使ったものと同じObservation snapshot上で選択Actionを評価する。

- `BLOCK`: dry-run/liveを問わずExecutorへ渡さない
- `SUSPICIOUS`: dry-runでは監査可能。live入力ではverification未完了としてExecutorへ渡さない
- `SAFE`: 通常どおりExecutorへ渡す

これによりSafety評価の存在を実行経路で保証しつつ、#33 SafetyGuardとは別責務を維持する。

## 監査ログ

`ActionSafetyAuditLog` は評価、Execution、後から判明したactual Outcomeを同じ `assessment_id` へ紐付ける。

- `evaluation`
- `execution`
- `actual_outcome`

`DecisionPipeline.record_safety_outcome()` から後続のOutcome Detection結果を保存できる。将来#34のOutcome Fusion接続時も同じIDを利用できる。

## 将来拡張

以下はschemaを壊さず追加できる。

- calibrated risk threshold
- conformal/selective prediction
- OOD / one-class detector
- sequence anomaly detector
- independent verifier / ensemble disagreement
- scene/action別cost-sensitive threshold
- CVaR等のtail-risk評価

これらを導入しても、#33の決定論的hard stopは独立して残す。
