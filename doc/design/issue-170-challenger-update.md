Spec-Status: READY
Depends-On: #169（Training Value / Error Diagnosis、merged PR #728）
Decision-Gaps: なし（下記の既定値はいずれも保守側で、後から版を上げて変更できる。実runtimeへのChallenger適用はこのスライスで行わない）
Parent: #36（兄弟: #171 Promotion / Rollback Gate、#173 Candidate Evaluation Runner。関連: #647、#712、#426、#427）

# Issue #170 設計：Challenger Update Pipeline（今回のスライス）

#169の`TrainingDiagnosisReport`から、型付きの更新提案（update proposal）を作り、Championの部品parameterを変更せずに版・出所（provenance）・資源見積り付きのChallenger artifactを決定論的に生成する。採否（#171）と比較評価（#173）は行わず、両者が読む契約だけを定める。

今回のスライス: proposal schema、strategy選択（calibration / threshold / prototype の3 baseline）、Champion/Challengerの分離、version/provenance、resource cost metadata、rollback可能なartifactの保存・読込。
残り: lightweight ML strategy、#426/#427の本実装との接続、実runtime（認識・判断）がChallenger parameterを読む経路（末尾「範囲外・残件」）。

## 現状調査

- `src/ai_game_player/training_diagnosis*.py`（#169）: `TrainingDiagnosisReport`（`schema_version=1`、`policy_fingerprint`、`diagnosis_id`）。`ComponentTarget.status`は確定根拠（`strict`）が`minimum_strict_support`（既定2）以上なら`candidate`、未満は`hypothesis`（確信度上限0.5）。`CaseDiagnosis.attributions[].strict`はreceipt事実か#647の`verified_effect`由来だけTrue。`ComponentTarget.evidence_refs`は確定根拠を優先するが不確実な参照も混ざり得て、参照ごとのstrict区別を持たない。`findings`は`max_findings`で切り詰められ得る（`findings_truncated`）。
- `src/ai_game_player/knowledge_promotion.py`: Knowledge Snapshot専用のChampion pointer・評価・rollback（`KnowledgePromotionGate`）。Knowledge本体の更新経路で、認識/判断の校正parameterは扱わない。本Issueはこれを変更・流用せず、同じ「不変の保存版＋pointerは別所有」の考え方に合わせる。
- `src/ai_game_player/artifact_store.py`: 内容hash（SHA-256）で保存する`FileSystemArtifactStore`（`put`/`read`/`verify`）。Challenger artifactの永続化に再利用する。
- `src/ai_game_player/knowledge_scope.py`: `KnowledgeScope`（5識別子、`key`=SHA-256）。提案・artifactのscope隔離に再利用する。
- `src/ai_game_player/learning_dataset.py`: `DatasetSource`（episode/step/event参照）。provenanceの根拠参照に再利用する。
- 認識信頼度・判断予測成功率を校正するparameterの汎用保存先は既存に無い（`grep calibrat`はbelief/planning固有の値だけ）。本Issueで最小の`ComponentParameters`を定義する。

## 成果物

| file | 内容 |
|---|---|
| `src/ai_game_player/challenger_update_policy.py` | `ChallengerUpdatePolicy`（strategy表・強度・上限・指紋）、`STRATEGIES`、`CHALLENGER_UPDATE_RULE_VERSION` |
| `src/ai_game_player/challenger_update_models.py` | `ComponentParameters`、`ResourceCost`、`UpdateProposal`、`ObservationRequest`、`UpdateProposalSet`、`ChallengerArtifact`（不変・`to_dict`/`from_dict`・digest） |
| `src/ai_game_player/challenger_update.py` | `propose_updates()`、`build_challenger()`、`save_challenger()`、`load_challenger()` |
| `tests/test_challenger_update.py` | 下記「テスト」の全項目 |
| `src/ai_game_player/assets/privacy/data_inventory.json`（変更） | artifact_type `learning.challenger`を既存class `challenger_artifact`へ分類し、owner moduleに`challenger_update`を追加（#224の網羅testの要件） |
| 文書 | `doc/Challenger更新機能説明書.md`（新規）、`doc/学習診断機能説明書.md`・`doc/guide/records_learning.md`・`doc/versions.md`へ追記、`doc/class_diagram.mmd`更新、`generated/repo_map.json`再生成 |

## 入出力

### ComponentParameters（Champion/Challengerの部品parameter）

| field | 型・制約 |
|---|---|
| `component` | `recognition` / `decision`（#169の`COMPONENTS`のうち本スライスが更新できるもの） |
| `scope` | `KnowledgeScope` |
| `version` | `[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}` |
| `confidence_scale` | 有限float、0.1..2.0（既定1.0）。部品の確信度（認識信頼度・判断予測成功率）に掛ける校正倍率 |
| `acceptance_threshold` | 有限float、0..1（既定0.5）。部品の出力を採用する確信度下限 |
| `prototypes` | `Prototype(ref: DatasetSource, label: "negative")`のtuple、0..256件、ref重複なし、ref順に正規化 |
| `sha256` | 上記の正規化JSONのSHA-256（property） |

### UpdateProposal

`proposal_id`（内容のSHA-256先頭32桁に`proposal.`接頭辞）、`component`、`scope`、`strategy`（`calibration`/`threshold`/`prototype`）、`parameter`（`confidence_scale`/`acceptance_threshold`/`prototypes`）、`direction`（`decrease`/`increase`/`add`）、`magnitude`（0..1、strategyの上限内）、`failure_kind`（根拠にした#169の種類。componentと一致する種類だけ）、`target_confidence`（candidateの確信度、0より大きい）、`strict_support`（採用failure_kindの検証済み根拠件数、`minimum_strict_support`以上）、`strict_refs`（`verified_effect`の検証済み根拠の参照だけ、1..min(16, strict_support)件、重複なし）、`champion_version`/`champion_sha256`（提案の前提にしたChampion）、`diagnosis_id`、`diagnosis_policy_fingerprint`、`update_policy_fingerprint`、`rank`（同じtarget内の優先順0..2）。

組合せの固定規則（`__post_init__`で検査）: `recognition_unreliable`→recognition、`decision_overconfident`/`decision_underestimated`/`action_ineffective`→decision。strategy→parameterは calibration→`confidence_scale`、threshold→`acceptance_threshold`、prototype→`prototypes`。directionは手順3の規則で一意に決まる。prototypeは負例の意味を持つ`decision_overconfident`/`action_ineffective`だけ（policyでも他のkindへ割り当て不可）。

### ObservationRequest（不確実な学習の分離、#712）

`component`、`scope`、`confidence`（≤0.5のまま）、`strict_support`、`uncertain_support`、`reason`（`insufficient_strict_support`）。更新提案ではなく「追加観測が必要」という出力で、Challengerを作らない。

### UpdateProposalSet

`schema: "challenger-update-proposals"`、`schema_version: 1`、`diagnosis_id`、`update_policy_fingerprint`、`proposals`、`observation_requests`、`skipped`（`(component, scope_key, reason)`の名前順tuple）、`proposals_truncated`、`proposal_set_id`（内容SHA-256）。

`skipped.reason`: `no_applicable_strategy`（例: execution）、`missing_champion`、`strict_evidence_unavailable`（findings切り詰め等で、findingsから数え直した検証済み根拠件数がtargetの`strict_support`と一致しない、または採用failure_kindの件数が下限未満）、`ambiguous_failure_kind`（検証済み根拠の最多failure_kindが同数）、`strategy_disabled`、`nothing_to_update`（どのstrategyも有効な変更を作れない：範囲外、追加できるprototypeが無い、magnitudeが0）。

### ChallengerArtifact（rollback可能な版）

| field | 内容 |
|---|---|
| `schema` / `schema_version` | `"challenger-artifact"` / `1` |
| `artifact_id` | `created_at`を除いた内容のSHA-256に`challenger.`接頭辞（同じ提案・Championからは時刻に関係なく同じID） |
| `status` | 常に`"challenger"`。本Issueは`champion`を生成しない |
| `challenger` | 新しい`ComponentParameters`（`version` = `<champion.version>+c.<digest先頭12桁>`、128字を超える場合は拒否） |
| `parent` | 元Championの`ComponentParameters`全体（rollback先。参照ではなく値を同梱） |
| `proposal` | 生成に使った`UpdateProposal` |
| `provenance` | `builder_rule_version`、`update_policy_fingerprint`、`diagnosis_id`、`diagnosis_policy_fingerprint`、`strict_refs`、`created_at`（呼出側が渡す。`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`のUTCだけ受理し、不正は`invalid_created_at`）、`lineage_depth`（`parent.version`中の`+c.`出現数＋1。8を超える生成は`version_too_long`で拒否） |
| `resource_cost` | `ResourceCost(parameter_bytes, delta_bytes, prototype_count, compute_class)`。`parameter_bytes`はchallengerの正規化JSONのbyte数、`delta_bytes`はparentとの差（負なら0）、`compute_class`は`constant`（calibration/threshold）か`linear_in_prototypes`。評価case数・時間はartifactの外（#173）で決まり非決定的なので含めない |
| `evaluation_contract` | #173向け: `trigger_refs`（=`strict_refs`。提案の根拠になった検証済みfailureの再現用で、評価集合の全体ではない）、`component`、`scope`、`compare`（`champion_sha256`/`challenger_sha256`）。regression・汎化・保護sampleの選定と実行は#173が行い、#173も入力時に参照の適格性を自分で検査する |

### API

```python
propose_updates(report: TrainingDiagnosisReport,
                champions: Iterable[ComponentParameters],
                policy: ChallengerUpdatePolicy | None = None) -> UpdateProposalSet
build_challenger(proposal: UpdateProposal, champion: ComponentParameters,
                 policy: ChallengerUpdatePolicy | None = None, *, created_at: str) -> ChallengerArtifact
save_challenger(store: FileSystemArtifactStore, artifact: ChallengerArtifact) -> ArtifactReference
load_challenger(store: FileSystemArtifactStore, reference: ArtifactReference,
                policy: ChallengerUpdatePolicy | None = None) -> ChallengerArtifact  # policy指定時は手順5の提案検査も行う
ChallengerArtifact.rollback_parameters() -> ComponentParameters   # parentを返す
```

エラー: すべて`ChallengerUpdateError(ValueError)`、`code`属性は`invalid_report` / `invalid_policy` / `invalid_parameters` / `stale_champion` / `scope_mismatch` / `invalid_proposal` / `parameter_out_of_bounds` / `prototype_capacity` / `version_too_long` / `invalid_created_at` / `invalid_artifact`。

### ChallengerUpdatePolicy（既定値）

| field | 既定 | 制約 |
|---|---|---|
| `strategy_table` | `recognition_unreliable: (threshold, calibration)`、`decision_overconfident: (calibration, prototype)`、`decision_underestimated: (calibration,)`、`action_ineffective: (prototype, calibration)` | keyは上記4種だけ（execution/outcome系はcomponentが更新対象外なので不可）、名前順・一意、STRATEGIESの重複なし、prototypeは負例kindだけ |
| `enabled_strategies` | 3つ全部 | STRATEGIESの部分集合 |
| `intensity` | 0.5 | 0..1。#426の`normalized_intensity`を受ける口。上限（step）だけに作用する |
| `min_step` / `max_step` | calibration 0.02/0.2、threshold 0.02/0.15 | 0<min≤max≤`hard_max_step`(0.25) |
| `max_prototypes_per_proposal` / `max_prototypes` | 16 / 256 | 1..16（1提案の参照上限） / 1..256 |
| `max_proposals_per_target` | 2 | 1..3 |
| `max_proposals` | 64 | 1..1024 |
| `minimum_strict_support` | 2 | 2..65536（2未満へ下げられない） |

`fingerprint` = 規則版＋全field（sort済みJSON）のSHA-256先頭16桁。

## 手順

1. **報告の検査**（`propose_updates`）: `type(report) is TrainingDiagnosisReport`かつ`schema_version == 1`、targetsの`(component, scope.key)`が一意でなければ`invalid_report`（#169は一意に集計するが、手で作った報告も受けるため検査する）。policyは型一致のみ受理（`invalid_policy`）。championsは型検査の後`(component, scope.key)`の辞書へ正規化し、重複は`invalid_parameters`。
2. **targetの分類**（targetごとに独立。出力の並びは手順4で正規化するので報告・championsの入力順に依存しない）:
   - `hypothesis` → `ObservationRequest`（確信度は変更しない）。Challengerは作らない。
   - `candidate`は、`findings`から当該component×scopeの帰属のうち「caseの`label_status == "verified_effect"`かつ`strict=True`かつ`basis == "verified_label"`」だけを検証済み根拠として数え直す（strictはreceipt由来もあり得るため、#647の`verified_effect`を明示的に検査する）。数え直した件数がtargetの`strict_support`と一致しなければ`strict_evidence_unavailable`（findings切り詰めで欠けた根拠があり得る。fail-closed。`evidence_refs`は不確実な参照を含み得るため使わない）。
   - 確信度0より大きい検証済み根拠のfailure_kind件数の最多を採用。最多が同数で複数なら`ambiguous_failure_kind`、最多の件数が`minimum_strict_support`未満なら`strict_evidence_unavailable`。
   - `strategy_table`に無い／空なら`no_applicable_strategy`、表にあるが全て無効化なら`strategy_disabled`。Championが無ければ`missing_champion`。
3. **strategy選択**: 表の順で有効なstrategyを試し、Championに対して有効な変更を作れたものを最大`max_proposals_per_target`件（rankは0から詰める）。各proposalの`magnitude`:
   - calibration/threshold: `magnitude = round(policy.max_step(strategy) * target_confidence, 6)`。`max_step = min(min_step + intensity*(max_step-min_step), hard_max_step)`で、min_stepは上限の下端であり実際の更新量の下限ではない。magnitudeが0なら作らない。
   - prototype: `magnitude = target_confidence`。追加するのはChampionに無いrefだけで、`max_prototypes_per_proposal`件まで。0件なら作らない。
   - direction: calibrationは`decision_underestimated`だけ`increase`、他（overconfident/ineffective/recognition_unreliable）は`decrease`。thresholdは`increase`（採用を厳しく）。prototypeは`add`。
   - 新parameterが範囲外（手順5）になるstrategyは作らない。何も作れなければ`nothing_to_update`。
   - `strict_refs`は採用failure_kindの検証済み根拠の参照を確信度降順→ref順で最大16件（prototypeは上記の新規refだけ）。
4. **並びと全体上限**: proposalsを`(-target_confidence, COMPONENTS順, scope.key, rank)`で、observation_requestsを`(-confidence, COMPONENTS順, scope.key)`で、skippedを名前順で並べる。proposalsが`max_proposals`を超えたら先頭から残し`proposals_truncated=True`。
5. **Challenger生成**（`build_challenger`、純粋関数）: 型検査→`proposal.component/scope`とchampionの一致（`scope_mismatch`）→`proposal.champion_sha256 == champion.sha256`（`stale_champion`）→proposalをpolicyで再検査（`invalid_proposal`。外部で作られたproposalも同じ）: `update_policy_fingerprint == policy.fingerprint`、strategyがpolicyで有効かつ当該failure_kindの表に含まれる、`strict_support >= minimum_strict_support`、calibration/thresholdは`magnitude == round(policy.max_step(strategy) * target_confidence, 6)`、prototypeは`magnitude == target_confidence`かつ`len(strict_refs) <= max_prototypes_per_proposal`。組合せ（component/kind/strategy/parameter/direction）は型の構築時に検査済み→`created_at`の形式（`invalid_created_at`）→新parameterを計算:
   - calibration: `scale * (1 ∓ magnitude)`、threshold: `threshold + magnitude`。範囲外になる場合はclampせず`parameter_out_of_bounds`（暗黙の飽和で効果の無いChallengerを作らない）。
   - prototype: 既存に無いrefだけ追加。追加0件は`invalid_proposal`、合計が`max_prototypes`超過は`prototype_capacity`。
   - championは不変dataclassで、新しい値を`dataclasses.replace`で作る。championを書き換える経路は無い（分離）。
6. **保存と読込**: `save_challenger`は`to_dict`の正規化JSONを`FileSystemArtifactStore.put(media_type="application/json", artifact_type="learning.challenger", sensitive=False)`で保存。`load_challenger`は`read`（hash検証込み）→上限256KiBのJSON（重複key・NaN拒否）→`from_dict`で全field再検証（`to_dict()`の再生成結果が入力と完全一致すること。これで`artifact_id`・`resource_cost`・`evaluation_contract`・`proposal_id`の整合も確かめる）→`parent.sha256 == proposal.champion_sha256`、challengerが版（`<parent版>+c.<proposal_id先頭12桁>`）を含めて提案どおりの計算結果と完全一致（再計算して照合）を確認。`policy`を渡した場合は手順5の提案検査も行う（policyを変えた後でも過去のartifactを読めるよう、既定では整合性だけを検査する）。数え直しでは同じ参照の重複も`strict_evidence_unavailable`とする。どれか不一致は`invalid_artifact`。
7. **pointerを持たない**: 本Issueは「どれがChampionか」を記録しない。採否・pointer・監査は#171が`ChallengerArtifact`を入力として持つ。rollbackは`rollback_parameters()`がparent値を返すだけで、適用は#171。
8. **副作用**: `propose_updates`/`build_challenger`はファイル・DB・Championへ書き込まない。永続化は`save_challenger`だけで、内容hashの不変objectの追加のみ。

## テスト

| 受入条件 | 試験（`tests/test_challenger_update.py`） |
|---|---|
| typed update proposalがある | `test_candidate_target_produces_typed_proposal`、`test_proposal_round_trips_and_id_is_content_digest` |
| 複数strategyを選択できる | `test_strategy_table_orders_multiple_strategies`、`test_disabled_strategy_is_skipped`、`test_intensity_scales_step_within_hard_cap` |
| ChallengerをChampionと分離できる | `test_build_challenger_does_not_mutate_champion`、`test_stale_champion_is_rejected`、`test_scope_mismatch_is_rejected` |
| version/provenanceがある | `test_artifact_records_version_and_provenance`、`test_lineage_depth_and_version_limit` |
| rollback可能なartifactを生成できる | `test_save_load_round_trip_and_rollback_parameters`、`test_tampered_artifact_is_rejected` |
| #647: verified以外を確定扱いしない | `test_hypothesis_target_becomes_observation_request`、`test_uncertain_refs_are_not_used_as_prototypes`、`test_truncated_findings_fail_closed`、`test_strict_receipt_is_not_a_verified_label`、`test_prototype_only_for_negative_kinds` |
| resource cost | `test_resource_cost_is_deterministic` |
| fail-closed/上限 | `test_ambiguous_failure_kind_is_skipped`、`test_out_of_bounds_parameter_is_rejected`、`test_prototype_capacity`、`test_policy_validation`、`test_proposal_limit_truncates`、`test_external_proposal_is_revalidated`、`test_duplicate_targets_are_rejected`、`test_invalid_created_at` |
| 再現性 | `test_same_input_same_output`（championsの順序・caseの入力順を入れ替えても同じ`proposal_set_id`。報告のtargets・findingsを並べ替えると`diagnosis_id`が変わるため、識別子以外の提案内容と順序が同じことを確かめる）、`created_at`が違っても同じ`artifact_id`（`test_artifact_records_version_and_provenance`） |

テストはネットワーク不要・決定論的。Experienceは#169のテストと同じ形で`diagnose_episodes`から作る。

## 完了条件

- 上記テストがすべて合格し、`finish_task.bat`が全段pass。
- #170の完了条件5項目に対応する試験がある（上表）。
- 文書（機能説明書・versions・class図・repo_map）が実装と一致。
- #171/#173の入力契約（`ChallengerArtifact`の`evaluation_contract`と`rollback_parameters()`）が説明書に記載されている。

## 代替案と不採用理由

- **KnowledgePromotionGateへ統合**: Knowledge Snapshot専用の保存形式・評価器に依存し、認識/判断の校正parameterを表せない。#171の採否もそこへ固定してしまうため不採用。
- **`ComponentTarget.evidence_refs`をそのままprototypeにする**: 不確実な参照が混ざり得て#647に反する。findingsのstrict帰属から再構成する。
- **範囲外の値をclampする**: 飽和したChallengerは評価コストだけ使って効果が無く、原因の追跡も難しくなるため拒否する。
- **Championのpointerを本Issueで持つ**: #171の責務と重複し、二重のpointerができるため不採用。
- **lightweight ML strategyを空実装で宣言**: 呼べないstrategyを契約に入れると#171/#173が扱いを決められない。版を上げて追加する。

## 範囲外・残件

- 採否・Champion pointer・監査・自動rollback: #171。
- Champion/Challengerの同一case比較・regression・資源計測: #173（`evaluation_contract`を入力にする）。
- lightweight ML（LightGBM等）strategy、component固有の探索空間とladder: #427。
- Adaptation Aggressivenessの共通slider: #426（本Issueは`intensity`の受け口だけ）。
- 不確実な学習（soft遷移）: #712（本Issueは`ObservationRequest`として分離して渡すだけ）。
- 実runtime（認識・判断）が`ComponentParameters`を読む経路、およびexecution/outcome_interpretation向けstrategy: #170の後続スライス。
