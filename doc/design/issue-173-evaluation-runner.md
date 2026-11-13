# Issue #173 Candidate Evaluation Runner 設計と検証

Parent: #37（兄弟: #170 Challenger Update、#171 Promotion Gate）。最終確認日: 2026-11-08。

## 目的と範囲

#170の`ChallengerArtifact`（Champion=`parent`とChallenger）を**同じ症例**で採点し、対象の改善・回帰・latency/資源を分けて測る。結果は#171のGateが読む`CandidateEvaluation`と、保存できる`EvaluationEvent`で返す。採否そのもの（#171）と実世界検証の代替は行わない。

対象外: BoardGame Arena（#756）のAdapter、prototypeの効果の測定、実runtimeへの配線、複数processの並列採点。

## 入口

| 入口 | 役割 |
|---|---|
| `evaluate_candidate(artifact, cases, policy, *, evaluation_ref, evaluated_at, predictor=, clock=, evaluable_strategies=)` | `(CandidateEvaluation, EvaluationEvent)`を返す。副作用なし |
| `save_evaluation_event(store, event)` / `load_evaluation_event(store, reference)` | 内容hashの不変object（`artifact_type="learning.evaluation_event"`、256KiBまで）として保存・再検証して読込み |
| `EvaluationCase(case, group)` | #169の`DiagnosisCase`に群（`target`＝改善を見たい症例、`regression`＝悪化させたくない症例）を付ける |

## 採点の規則

1. 同じ症例を、Champion（`artifact.parent`）とChallengerの両方で採点する（症例・scopeの不一致と、同じ症例`ref`の重複は拒否する。**同じ`ref`をtargetとregressionの両群に置くことも拒否**する: 両群は独立した改善/回帰検査で、共有を許すと検証データ漏洩と件数の二重計上になる）。
2. 正解は**確定ラベル**だけ（`label_status=verified_effect`かつ`outcome`が`success`/`failure`、矛盾・保留なし）。それ以外は`unscorable`として数え、採点に含めない（#647: 確定扱いしない）。
3. 判定は`predict_accept`: componentの信号（decision→`decision_expected_success`、recognition→`recognition_reliability`）に`confidence_scale`を掛けて1.0で頭打ちにし、`acceptance_threshold`以上なら「採用」。採用が成功と一致すれば正解。
4. 採点中の例外（`Exception`の型を問わない。`TimeoutError`・`RuntimeError`・Provider由来の例外を含む）と、1症例の採点が`case_latency_budget_ms`を超えた場合は**誤り扱いで母数に含める**（失敗・Timeoutを隠さない）。`KeyboardInterrupt`/`SystemExit`は握り潰さない。**限界**: 同期のPython callableは途中で強制停止できないため、このrunnerは「経過後に予算超過なら誤り」と数えるだけで、wall-clockの上限は強制しない。実Providerを接続する時は、強制停止できるworker/process側の契約（別Issue）で上限を課す。
5. 群×armごとに`ArmSummary`（scored/correct/failed/unscorable/accuracy/latency_p95_ms）を作る。`CandidateEvaluation`の件数は両armの小さい方の`scored`。
6. `resource_cost`はartifactの決定論的な見積りをEventへ写す（時間計測とは別）。

## 評価できない候補（prototype）と、予測・正解の分離

- 組込みの採点関数`predict_accept`は`confidence_scale`と`acceptance_threshold`だけを見て、`prototypes`を参照しない。一方#170の`prototype`戦略は負例参照を足すだけでscale/thresholdを変えない。そのため、prototypeだけ違うChallengerをこの採点で比べると、全症例でChampionと同じ予測になり、改善幅が必ず0になる。これを「測った結果が0」と返すと、有効な更新が採用不能に見えてしまう。
- そこで、`evaluable_strategies`（既定は`calibration`と`threshold`）に含まれないstrategy、またはprototypeが親と違うのにprototypeを評価できない場合は、**採点不可**として返す: 全症例を`unscorable`に数え、`scored`は0、`CandidateEvaluation`の件数は0、信号は`hold`・理由は`unsupported_evaluation`。#171のGateは件数不足（`insufficient_target_cases`）で拒否するので昇格しない。Eventは通常どおり保存できる。
- prototypeの参照の突き合わせと負例の効果を反映した採点関数は、このrunnerには含めない（後続Issue）。その採点関数を渡す時は、呼出側が`evaluable_strategies`に`"prototype"`を含める。
- 採点関数は`PredictionInput`（症例の`ref`と、decision/recognitionの信号だけ）を受け取る。正解ラベル（`outcome`・`label_status`等）は渡さず、正解の判定はrunnerが別に持つ。将来の外部predictorへのラベル漏洩を防ぐため。

## 学習Level信号（助言のみ）

回帰群でChallengerの失敗(例外・時間超過)がChampionより多い→`de_escalate`（`regression_failures`）。元から不正解の症例がTimeoutに置換されてもaccuracyは変わらないため、故障数で検出する。回帰群の低下が`regression_tolerance`を超える→`de_escalate`（`regression`）。採点件数が`min_scored_per_group`未満→`hold`（`insufficient_evidence`）。Challengerの失敗がChampionより多い→`de_escalate`（`more_failures`）。対象の改善が`min_gain`以上→`escalate`（`improved_without_regression`）。それ以外→`hold`。#171 Gateの型(`CandidateEvaluation`)は故障数を持たないため、いずれかの群でChallengerの故障がChampionより増えた候補は`regression_cases=0`として渡し、Gateが`insufficient_regression_cases`で拒否する(これは件数を偽る暫定のfail-closed接続で、型付きEvidenceへの移行は#901で行う。p95遅延も本番昇格条件には未反映)。信号は#36のController等が読む助言で、runnerは学習Levelを変更しない。

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
| prototypeだけ違う候補を改善0と測らない | `PrototypeEvaluationTest`（組込み採点では区別できないこと、採点不可としてholdし昇格しないこと、保存の往復、prototype対応の採点関数では差が出ること、calibrationは従来どおり評価） |
| 正解ラベルを予測に渡さない | `PredictionInputSeparationTest` |
| #171との接続 | `test_evaluation_feeds_the_promotion_gate`（改善は昇格、回帰は却下） |

## 制限と未実装

- prototypeだけを変えるChallengerは、この採点では評価できないので採点不可（`unsupported_evaluation`、件数0・hold）として返し、昇格させない（上の節）。prototypeの参照の突き合わせと負例の効果を反映した採点関数は未実装で、後続Issueで扱う。
- `predict_accept`は「信号×scale≧threshold」という最小の判定で、実Providerや実Decisionを通した評価ではない。実機・実Provider経由の評価は未実装。
- 症例の集め方（targetとregressionの分け方）は呼出側の責務。runnerは母数の偏りを検査しない。
