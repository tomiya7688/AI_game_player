# Issue #173 Candidate Evaluation Runner 設計と検証

Parent: #37（兄弟: #170 Challenger Update、#171 Promotion Gate）。最終確認日: 2026-11-08。

## 目的と範囲

#170の`ChallengerArtifact`（Champion=`parent`とChallenger）を**同じ症例**で採点し、対象の改善・回帰・latency/資源を分けて測る。結果は#171のGateが読む`CandidateEvaluation`と、保存できる`EvaluationEvent`で返す。採否そのもの（#171）と実世界検証の代替は行わない。

対象外: BoardGame Arena（#756）のAdapter、prototypeの効果の測定、実runtimeへの配線、複数processの並列採点。

## 入口

| 入口 | 役割 |
|---|---|
| `evaluate_candidate(artifact, cases, policy, *, evaluation_ref, evaluated_at, predictor=, clock=)` | `(CandidateEvaluation, EvaluationEvent)`を返す。副作用なし |
| `save_evaluation_event(store, event)` / `load_evaluation_event(store, reference)` | 内容hashの不変object（`artifact_type="learning.evaluation_event"`、256KiBまで）として保存・再検証して読込み |
| `EvaluationCase(case, group)` | #169の`DiagnosisCase`に群（`target`＝改善を見たい症例、`regression`＝悪化させたくない症例）を付ける |

## 採点の規則

1. 同じ症例を、Champion（`artifact.parent`）とChallengerの両方で採点する（症例・scope・群の重複は拒否）。
2. 正解は**確定ラベル**だけ（`label_status=verified_effect`かつ`outcome`が`success`/`failure`、矛盾・保留なし）。それ以外は`unscorable`として数え、採点に含めない（#647: 確定扱いしない）。
3. 判定は`predict_accept`: componentの信号（decision→`decision_expected_success`、recognition→`recognition_reliability`）に`confidence_scale`を掛けて1.0で頭打ちにし、`acceptance_threshold`以上なら「採用」。採用が成功と一致すれば正解。
4. 採点中の例外と、1症例の採点が`case_latency_budget_ms`を超えた場合は**誤り扱いで母数に含める**（失敗・Timeoutを隠さない）。
5. 群×armごとに`ArmSummary`（scored/correct/failed/unscorable/accuracy/latency_p95_ms）を作る。`CandidateEvaluation`の件数は両armの小さい方の`scored`。
6. `resource_cost`はartifactの決定論的な見積りをEventへ写す（時間計測とは別）。

## 学習Level信号（助言のみ）

回帰群の低下が`regression_tolerance`を超える→`de_escalate`（`regression`）。採点件数が`min_scored_per_group`未満→`hold`（`insufficient_evidence`）。Challengerの失敗がChampionより多い→`de_escalate`（`more_failures`）。対象の改善が`min_gain`以上→`escalate`（`improved_without_regression`）。それ以外→`hold`。信号は#36のController等が読む助言で、runnerは学習Levelを変更しない。

## offline / real-world

runnerが作るEventは常に`validation_state="offline"`。`real_world`は型として表すが、runnerは作らない（実世界検証の代替ではない）。

## テスト（`tests/test_challenger_evaluation.py`）

| 完了条件 | 試験 |
|---|---|
| 同一caseでChampion/Challengerを比較 | `test_same_case_is_scored_by_both_arms` |
| improvement/regressionを別集計 | `test_improvement_and_regression_are_aggregated_separately`、`test_regression_lowers_the_level_signal` |
| latency/resourceを測る | `test_latency_is_measured_per_arm`、`test_errors_and_timeouts_count_as_failures_in_the_denominator` |
| Evaluation Eventを保存 | `EvaluationEventStorageTest`（往復・改ざん拒否） |
| Level調整signal | 上記signal試験、`test_insufficient_evidence_holds` |
| offline/real-worldの区別 | `test_improvement_and_regression_are_aggregated_separately`（offline） |
| 確定ラベルのみ採点 | `test_cases_without_verified_labels_are_not_scored` |
| #171との接続 | `test_evaluation_feeds_the_promotion_gate`（改善は昇格、回帰は却下） |

## 制限と未実装

- prototypeだけを変えるChallengerは、この採点では差が出ない（`no_target_improvement`で却下される）。prototypeの評価は別途。
- `predict_accept`は「信号×scale≧threshold」という最小の判定で、実Providerや実Decisionを通した評価ではない。実機・実Provider経由の評価は未実装。
- 症例の集め方（targetとregressionの分け方）は呼出側の責務。runnerは母数の偏りを検査しない。
