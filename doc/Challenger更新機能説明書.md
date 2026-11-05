# Challenger更新（Challenger Update Pipeline）機能説明書

[リポジトリに戻る](../README.md) ｜ [学習診断](学習診断機能説明書.md) ｜ [記録と学習の案内](guide/records_learning.md) ｜ [設計](design/issue-170-challenger-update.md)

Issue #170（親 #36）の今回のスライスを説明します。#169の学習診断（`TrainingDiagnosisReport`）から型付きの更新提案を作り、Championの部品parameterを変えずに、版・出所・資源見積り付きのChallenger artifactを決定論的に生成します。採否・Champion pointer・rollbackの適用（#171）と、Champion/Challengerの比較評価（#173）は行いません。

## 入口と出力

| 入口 | 出力 | 副作用 |
| --- | --- | --- |
| `challenger_update.propose_updates(report, champions, policy=None)` | `UpdateProposalSet`（`schema: "challenger-update-proposals"`、`schema_version=1`、`proposal_set_id`） | なし |
| `challenger_update.build_challenger(proposal, champion, policy=None, *, created_at)` | `ChallengerArtifact`（`schema: "challenger-artifact"`、`schema_version=1`、`status: "challenger"`） | なし。Championは不変値のまま |
| `save_challenger(store, artifact)` / `load_challenger(store, reference)` | `FileSystemArtifactStore`の内容hash参照 / 再検証済みartifact | 不変objectの追加だけ（`artifact_type="learning.challenger"`、256KiBまで） |

`champions`は`ComponentParameters`（component×scopeごとに一つ）です。本版で更新できるcomponentは`recognition`と`decision`で、parameterは次の3つだけです。

| parameter | 範囲 | 意味 |
| --- | --- | --- |
| `confidence_scale` | 0.1..2.0（既定1.0） | 部品の確信度（認識信頼度・判断予測成功率）に掛ける校正倍率 |
| `acceptance_threshold` | 0..1（既定0.5） | 部品出力を採用する確信度の下限 |
| `prototypes` | 最大256件 | 検証済みの負例case参照（`label: "negative"`） |

## 確定ラベルの扱い（#647・#712との境界）

- 提案に使う根拠は、診断の`findings`のうち「caseの`label_status`が`verified_effect`」かつ「帰属が`strict=True`・`basis="verified_label"`」のものだけです。receipt由来のstrict（実行失敗）は確定ラベルではないので使いません。
- targetの`strict_support`と、`findings`から数え直した検証済み根拠の件数が一致しない（`findings`の切り詰めなど）場合は提案しません（`strict_evidence_unavailable`）。`ComponentTarget.evidence_refs`は不確実な参照を含み得るため使いません。
- `hypothesis`のtargetは更新提案にせず、確信度をそのまま残した`ObservationRequest`（`reason: "insufficient_strict_support"`）として返します。#712のsoft学習経路が追加観測の対象として扱う想定で、Challengerは作りません。

## strategyの選び方

既定のstrategy表（`ChallengerUpdatePolicy.strategy_table`）です。表の順で有効なstrategyを試し、Championへ有効な変更を作れるものを最大2件（`max_proposals_per_target`）提案します。

| failure_kind | 更新先 | strategy（優先順） | 方向 |
| --- | --- | --- | --- |
| `recognition_unreliable` | recognition | threshold → calibration | thresholdは上げる、calibrationは下げる |
| `decision_overconfident` | decision | calibration → prototype | 下げる / 負例追加 |
| `decision_underestimated` | decision | calibration | 上げる |
| `action_ineffective` | decision | prototype → calibration | 負例追加 / 下げる |

- target内で検証済み根拠（確信度0より大）の最多failure_kindを採用します。最多が同数なら`ambiguous_failure_kind`、最多の件数が`minimum_strict_support`（既定2、2未満にできない）未満なら`strict_evidence_unavailable`です。
- 更新量: `magnitude = round(max_step × target確信度, 6)`。`max_step = min(min_step + intensity × (max_step − min_step), 0.25)`で、既定はcalibration 0.02..0.2、threshold 0.02..0.15、`intensity`=0.5です。`intensity`は#426 Adaptation Aggressivenessの正規化強度を受ける口で、更新量の上限だけを変え、確定根拠の下限・範囲検査・件数上限は緩めません。
- prototypeはChampionに無い検証済み負例の参照だけを追加します（1提案16件まで）。負例の意味を持つ`decision_overconfident`/`action_ineffective`以外にはpolicyでも割り当てられません。
- 範囲外になる変更はclampせずに作りません。どのstrategyも作れなければ`nothing_to_update`です。execution/outcome_interpretationのtargetは`no_applicable_strategy`です。
- 出力の並びは`(−確信度, component, scope key, rank)`の固定順で、報告やchampionsの入力順に依存しません。全体は`max_proposals`（64）で切り、`proposals_truncated`で示します。

## Challengerの生成とrollback

`build_challenger`は、提案とChampionのcomponent・scopeの一致（`scope_mismatch`）、Championの版とSHA-256が提案時と同じこと（`stale_champion`）、提案がpolicyと一致すること（指紋・strategyの有効性と表・確定根拠件数・更新量の再計算値・prototype件数。外部で作った提案も同じ検査で`invalid_proposal`）、`created_at`がUTCの`...Z`表記であること（`invalid_created_at`）を確認してから新しい値を作ります。

| artifactのfield | 内容 |
| --- | --- |
| `challenger` / `challenger_sha256` | 新しいparameter。版は`<Champion版>+c.<proposal_id先頭12桁>` |
| `parent` / `parent_sha256` | 元Championのparameter全体。`rollback_parameters()`がこれを返す |
| `proposal` | 生成に使った提案（`proposal_id`付き） |
| `provenance` | 生成規則の版、提案policy指紋、`diagnosis_id`、診断policy指紋、根拠参照、`lineage_depth`（8世代まで、超えると`version_too_long`）、`created_at` |
| `resource_cost` | `parameter_bytes`・`delta_bytes`・`prototype_count`・`compute_class`（`constant`/`linear_in_prototypes`）。時間や評価件数は含めない |
| `evaluation_contract` | #173向けの`component`・`scope`・`trigger_refs`・`compare`（Champion/ChallengerのSHA-256） |

- `artifact_id`は`created_at`を除いた内容のSHA-256です。同じ提案とChampionからは時刻に関係なく同じIDになります。
- `load_challenger`はhash検証付きで読み、重複key・NaNを拒否し、`to_dict()`の再生成が保存内容と完全一致すること、Challengerが版を含めて提案どおりの計算結果であることを確認します。`policy`を渡すと、提案がそのpolicyに適合することも検査します（既定はpolicyを変えた後も過去のartifactを読めるよう整合性だけ）。違反は`invalid_artifact`です。
- エラーはすべて`ChallengerUpdateError`（`ValueError`）で、`code`は`invalid_report`・`invalid_policy`・`invalid_parameters`・`stale_champion`・`scope_mismatch`・`invalid_proposal`・`parameter_out_of_bounds`・`prototype_capacity`・`version_too_long`・`invalid_created_at`・`invalid_artifact`のいずれかです。

## #171・#173への受け渡し

- 本機能は「どれがChampionか」を記録しません。#171は`ChallengerArtifact`を入力に採否・pointer・監査を持ち、rollbackは`rollback_parameters()`の値を適用します。
- `evaluation_contract.trigger_refs`は提案の根拠になった検証済みfailureの再現用で、評価集合の全体ではありません。regression・汎化・保護sampleの選定と実行は#173が行い、#173も入力時に参照の適格性を自分で検査します。

## 未実装・制限

- 実runtime（認識・判断）が`ComponentParameters`を読む経路は未接続です。Challengerは生成・保存できますが、まだどの判断にも使われません。
- lightweight ML（LightGBM等）strategy、component固有の探索空間（#427）、Adaptation Aggressivenessの共通slider（#426）は未実装です。
- execution/outcome_interpretation向けのstrategyはありません。
- 規則ベースの更新量で、更新が改善になる保証はありません。改善かどうかは#173の評価と#171の採否で決めます。

根拠：[challenger_update.py](../src/ai_game_player/challenger_update.py)、[challenger_update_models.py](../src/ai_game_player/challenger_update_models.py)、[challenger_update_policy.py](../src/ai_game_player/challenger_update_policy.py)、[test_challenger_update.py](../tests/test_challenger_update.py)。
