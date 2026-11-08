# Issue #171 Promotion / Rollback Gate 設計と検証

Parent: #36（兄弟: #170 Challenger Update、#173 Candidate Evaluation Runner）。最終確認日: 2026-11-07。

## 目的と範囲

#170の`ChallengerArtifact`を、比較評価の数値から採否（promote/reject）し、Champion pointerの版追跡・rollback・監査を提供する。Knowledge Snapshot専用の`KnowledgePromotionGate`（#532、[knowledge_promotion](../architecture/knowledge_promotion.md)）とは別で、認識・判断の校正parameter（`ComponentParameters`）を対象にする。

対象外: 比較評価そのものの実行（#173）、実runtimeからのpointer読取り・自動学習配線（#422等）、test synthesis、無制御なオンライン自己更新。

## 入口

| 入口 | 役割 |
|---|---|
| `ChampionRegistry(path).initialize(champion, at=)` | component×scopeの初回Championを登録 |
| `.decide(artifact, evaluation, criteria, at=)` | 採否を計算し、昇格ならpointerを切替。結果は`PromotionDecision` |
| `.rollback(component, scope, reason=, at=)` | 直前のChampionへ戻す。reasonは`operator_request`/`regression_detected`/`safety_stop` |
| `.champion(component, scope)` / `.audit_events()` | 現在のChampion・監査の読取り |

## 採否の規則（`decide`）

外部の`passed`フラグは受け付けず、`CandidateEvaluation`の数値からGateが計算する。却下理由は固定コードで、満たさない条件はすべて列挙する。

1. 現在のChampionが`artifact.parent`と一致しない → `stale_champion`（同じartifactの二重昇格もここで拒否）
2. 評価の`artifact_id`・champion/challenger hashがartifactと一致しない → `evaluation_mismatch`
3. 件数が`min_target_cases`/`min_regression_cases`未満 → `insufficient_*_cases`（失敗・Timeoutを母数に含めて渡す前提）
4. 対象指標の改善が`min_target_gain`未満 → `no_target_improvement`
5. 回帰指標の低下が`max_regression_drop`（既定0）を超える → `regression`（対象指標が大きく改善しても昇格しない）
6. `resource_cost`が`max_parameter_bytes`/`max_delta_bytes`を超える → `resource_exceeded`

全て満たした時だけ昇格し、旧Championを履歴（最大32）へ積んでpointerを切り替える。却下もaudit eventに残る。

## 状態と監査

1ファイルのJSON（`schema: challenger-champion-registry`）。各操作は`cross_process_file_lock`内で状態を読み直し、検証し、一時ファイル経由で原子的に置換する（失敗時は旧状態）。audit eventは`prev_digest`/`digest`のhash連鎖で、保持上限128件。切り詰めた分は`audit_base_digest`に連鎖を引き継ぐ。読込時にkeyとparameterの整合、連鎖、重複key、NaNを検査し、不整合は`PromotionError(invalid_state)`にする。

## テスト（`tests/test_challenger_promotion.py`）

| 完了条件 | 試験 |
|---|---|
| 評価結果から採否できる | `test_improvement_promotes_and_moves_pointer`、`test_no_improvement_is_rejected_and_pointer_stays` |
| regression時rejectできる | `test_regression_is_rejected_even_with_big_gain`、`test_too_few_cases_are_rejected`、`test_resource_limit_is_rejected`、`test_evaluation_of_other_artifact_is_rejected` |
| promoted versionを追跡できる | `test_state_survives_new_registry_instance`、`test_stale_champion_is_rejected_after_another_promotion`、`test_targets_are_isolated_by_component_and_scope` |
| rollbackできる | `test_rollback_restores_previous_champion`、`test_rollback_reason_is_a_fixed_code` |
| audit trailがある | `test_audit_records_every_outcome_with_chain`、`test_tampered_state_is_rejected`、`test_audit_is_bounded_and_chain_stays_valid` |

## 制限と未実装

- 実runtime（Decision等）が`ChampionRegistry`のpointerを読む配線は未実装。
- 評価値の真正性は呼出側（#173）の責務。Gateは数値の範囲・対象一致・件数だけ検査する。
- 履歴32世代・監査128件を超えた古い分は保持しない。読込時にChampion pointerと世代は、保持している監査eventの最新状態と一致することを検査する（切り詰めで消えたtargetは監査との照合ができない）。
- Windows実機・複数processの同時書込みの実測は未実施（lockはdependency-freeなadvisory lock）。
- #426のaggressivenessは生成側にだけ影響し、本Gateの最低条件は弱めない。#427の複数candidateは、best candidateを1件ずつ`decide`へ渡す。
