"""#169の診断から型付き更新提案を作り、Championを変えずにChallenger artifactを生成する。

#170の提案・生成・保存だけを行い、採否とChampion pointer(#171)、比較評価(#173)は行わない。
確定根拠は#647のverified_effect段階だけで、確定根拠が足りないtargetは更新せず
#712向けの観測要求(ObservationRequest)として分けて返す。
"""

from collections.abc import Iterable
from dataclasses import replace
import json

from ai_game_player.artifact_store import ArtifactReference, FileSystemArtifactStore
from ai_game_player.challenger_update_models import (
    MAX_CONFIDENCE_SCALE,
    MAX_LINEAGE_DEPTH,
    MAX_PROTOTYPES,
    MAX_STRICT_REFS,
    MIN_CONFIDENCE_SCALE,
    PARAMETER_OF_STRATEGY,
    ChallengerArtifact,
    ChallengerUpdateError,
    ComponentParameters,
    ObservationRequest,
    Prototype,
    UpdateProposal,
    UpdateProposalSet,
    canonical,
    component_order,
    expected_direction,
    validate_created_at,
)
from ai_game_player.challenger_update_policy import (
    CHALLENGER_UPDATE_RULE_VERSION,
    FAILURE_COMPONENTS,
    ChallengerUpdatePolicy,
)
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis_models import (
    TRAINING_DIAGNOSIS_SCHEMA_VERSION,
    ComponentTarget,
    TrainingDiagnosisReport,
)


MAX_ARTIFACT_BYTES = 256 * 1024
_UPDATABLE_KINDS = dict(FAILURE_COMPONENTS)


# {
#   責務: [propose_updates: 診断報告のcandidate targetから型付き更新提案を作り、仮説targetを観測要求へ分ける]
#   処理: [報告・policy・Championを検査し、targetごとにverified_effect根拠を数え直してstrategyを選ぶ。
#     出力は入力順に依存しない固定順に並べ、件数上限で切る。ファイル・Championへ書き込まない]
#   引数: [report: #169の診断報告, champions: (component, scope)ごとの現Champion parameter,
#     policy: 提案policy。Noneなら既定]
#   戻り値: [UpdateProposalSet: 提案・観測要求・見送り理由の版付き集合]
#   エラー: [ChallengerUpdateError: invalid_report/invalid_policy/invalid_parameters]
# }
def propose_updates(
    report: TrainingDiagnosisReport,
    champions: Iterable[ComponentParameters],
    policy: ChallengerUpdatePolicy | None = None,
) -> UpdateProposalSet:
    selected = _policy(policy)
    if (
        type(report) is not TrainingDiagnosisReport
        or report.schema_version != TRAINING_DIAGNOSIS_SCHEMA_VERSION
    ):
        raise ChallengerUpdateError("invalid_report", "unsupported diagnosis report")
    keys = [(target.component, target.scope.key) for target in report.targets]
    if len(set(keys)) != len(keys):
        raise ChallengerUpdateError(
            "invalid_report", "diagnosis targets are not unique"
        )
    by_key: dict[tuple[str, str], ComponentParameters] = {}
    for champion in champions:
        if type(champion) is not ComponentParameters:
            raise ChallengerUpdateError(
                "invalid_parameters", "champion type is invalid"
            )
        key = (champion.component, champion.scope.key)
        if key in by_key:
            raise ChallengerUpdateError("invalid_parameters", "duplicate champion")
        by_key[key] = champion
    diagnosis = report.to_dict()
    verified = _verified_evidence(report)
    proposals: list[UpdateProposal] = []
    observations: list[ObservationRequest] = []
    skipped: list[tuple[str, str, str]] = []
    for target in report.targets:
        key = (target.component, target.scope.key)
        if target.status == "hypothesis":
            observations.append(
                ObservationRequest(
                    target.component,
                    target.scope,
                    target.confidence,
                    target.strict_support,
                    target.uncertain_support,
                )
            )
            continue
        made, reason = _target_proposals(
            target,
            verified.get(key, []),
            by_key.get(key),
            selected,
            diagnosis["diagnosis_id"],
            report.policy_fingerprint,
        )
        if reason is not None:
            skipped.append((*key, reason))
        proposals.extend(made)
    proposals.sort(
        key=lambda item: (
            -item.target_confidence,
            component_order(item.component),
            item.scope.key,
            item.rank,
        )
    )
    observations.sort(
        key=lambda item: (
            -item.confidence,
            component_order(item.component),
            item.scope.key,
        )
    )
    return UpdateProposalSet(
        diagnosis_id=diagnosis["diagnosis_id"],
        update_policy_fingerprint=selected.fingerprint,
        proposals=tuple(proposals[: selected.max_proposals]),
        observation_requests=tuple(observations),
        skipped=tuple(sorted(skipped)),
        proposals_truncated=len(proposals) > selected.max_proposals,
    )


# {
#   責務: [build_challenger: 一つの提案からChallenger parameterを計算し、元Championを同梱したartifactを返す]
#   処理: [提案とChampionの一致・鮮度、提案のpolicy適合、時刻形式を検査してから新しい値を作る。
#     Championは不変値のまま変更しない。範囲外はclampせず拒否する]
#   引数: [proposal: 更新提案, champion: 提案の前提にした現Champion, policy: 提案policy,
#     created_at: 呼出側が決めるUTCの生成時刻]
#   戻り値: [ChallengerArtifact: status=challengerのrollback可能なartifact]
#   エラー: [ChallengerUpdateError: scope_mismatch/stale_champion/invalid_proposal/
#     invalid_created_at/parameter_out_of_bounds/prototype_capacity/version_too_long]
# }
def build_challenger(
    proposal: UpdateProposal,
    champion: ComponentParameters,
    policy: ChallengerUpdatePolicy | None = None,
    *,
    created_at: str,
) -> ChallengerArtifact:
    selected = _policy(policy)
    if type(proposal) is not UpdateProposal:
        raise ChallengerUpdateError("invalid_proposal", "proposal type is invalid")
    if type(champion) is not ComponentParameters:
        raise ChallengerUpdateError("invalid_parameters", "champion type is invalid")
    if champion.component != proposal.component or champion.scope != proposal.scope:
        raise ChallengerUpdateError("scope_mismatch", "proposal targets another part")
    if (
        champion.sha256 != proposal.champion_sha256
        or champion.version != proposal.champion_version
    ):
        raise ChallengerUpdateError("stale_champion", "champion has changed")
    _check_proposal(proposal, selected)
    validate_created_at(created_at)
    depth = champion.version.count("+c.") + 1
    version = _challenger_version(champion.version, proposal)
    if depth > MAX_LINEAGE_DEPTH or len(version) > 128:
        raise ChallengerUpdateError(
            "version_too_long", "challenger lineage is too deep"
        )
    challenger = replace(
        _apply(proposal, champion, selected.max_prototypes), version=version
    )
    return ChallengerArtifact(
        challenger=challenger,
        parent=champion,
        proposal=proposal,
        builder_rule_version=CHALLENGER_UPDATE_RULE_VERSION,
        created_at=created_at,
    )


# {
#   責務: [save_challenger: artifactを内容hashの不変objectとして保存する]
#   処理: [正規化JSONをFileSystemArtifactStoreへ追加する。既存objectやChampionを変更しない]
#   引数: [store: 保存先, artifact: 生成済みartifact] 戻り値: [ArtifactReference: 内容hash参照]
#   エラー: [ChallengerUpdateError: invalid_artifact, OSError: 保存失敗]
# }
def save_challenger(
    store: FileSystemArtifactStore, artifact: ChallengerArtifact
) -> ArtifactReference:
    if type(artifact) is not ChallengerArtifact:
        raise ChallengerUpdateError("invalid_artifact", "artifact type is invalid")
    payload = canonical(artifact.to_dict())
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ChallengerUpdateError("invalid_artifact", "artifact is too large")
    return store.put(
        payload,
        media_type="application/json",
        artifact_type="learning.challenger",
        sensitive=False,
    )


# {
#   責務: [load_challenger: 保存artifactを上限付きで読み、内容・派生field・計算結果を再検証する]
#   処理: [hash検証付きで読み、重複key・NaNを拒否して復号し、提案からchallengerを再計算して照合する]
#   引数: [store: 保存先, reference: save_challengerの参照,
#     policy: 指定時は提案がそのpolicyに適合することも検査し、prototype上限にその値を使う]
#   戻り値: [ChallengerArtifact: 検証済みartifact]
#   エラー: [ChallengerUpdateError: invalid_artifact, OSError: 読取失敗]
# }
def load_challenger(
    store: FileSystemArtifactStore,
    reference: ArtifactReference,
    policy: ChallengerUpdatePolicy | None = None,
) -> ChallengerArtifact:
    try:
        payload = store.read_bounded(
            reference,
            max_bytes=MAX_ARTIFACT_BYTES,
            expected_artifact_type="learning.challenger",
        )
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise ChallengerUpdateError("invalid_artifact", str(error)) from error
    artifact = ChallengerArtifact.from_dict(value)
    limit = MAX_PROTOTYPES
    try:
        if policy is not None:
            _check_proposal(artifact.proposal, _policy(policy))
            limit = policy.max_prototypes
        expected = replace(
            _apply(artifact.proposal, artifact.parent, limit),
            version=_challenger_version(artifact.parent.version, artifact.proposal),
        )
    except ChallengerUpdateError as error:
        raise ChallengerUpdateError("invalid_artifact", str(error)) from error
    if expected != artifact.challenger:
        raise ChallengerUpdateError(
            "invalid_artifact", "challenger does not follow its proposal"
        )
    return artifact


# {
#   責務: [_verified_evidence: findingsから#647のverified_effect根拠だけをtargetごとに集める]
#   処理: [caseのlabel_statusがverified_effectで、strictかつbasis=verified_labelの帰属だけを採用する]
#   引数: [report: 診断報告]
#   戻り値: [dict: (component, scope_key) → (failure_kind, confidence, ref)の一覧]
# }
def _verified_evidence(
    report: TrainingDiagnosisReport,
) -> dict[tuple[str, str], list[tuple[str, float, DatasetSource]]]:
    result: dict[tuple[str, str], list[tuple[str, float, DatasetSource]]] = {}
    for case in report.findings:
        if case.label_status != "verified_effect":
            continue
        for item in case.attributions:
            if item.strict and item.basis == "verified_label":
                result.setdefault((item.component, case.scope.key), []).append(
                    (item.failure_kind, item.confidence, case.ref)
                )
    return result


# {
#   責務: [_target_proposals: 一つのcandidate targetから最大件数までの提案を作る]
#   処理: [数え直した件数とtargetの件数の一致、最多kindの一意性、strategy表、Championを順に確認し、
#     Championへ有効な変更を作れるstrategyだけ提案にする]
#   引数: [target: candidate, evidence: verified根拠, champion: 現Championか無し, policy: 提案policy,
#     diagnosis_id/diagnosis_fingerprint: 元の診断]
#   戻り値: [tuple[list[UpdateProposal], str | None]: 提案と、提案が無い場合の見送り理由]
# }
def _target_proposals(
    target: ComponentTarget,
    evidence: list[tuple[str, float, DatasetSource]],
    champion: ComponentParameters | None,
    policy: ChallengerUpdatePolicy,
    diagnosis_id: str,
    diagnosis_fingerprint: str,
) -> tuple[list[UpdateProposal], str | None]:
    if target.component not in _UPDATABLE_KINDS.values():
        return [], "no_applicable_strategy"
    if len(evidence) != target.strict_support or len(
        {ref for _kind, _confidence, ref in evidence}
    ) != len(evidence):
        return [], "strict_evidence_unavailable"
    counts: dict[str, int] = {}
    for kind, confidence, _ref in evidence:
        if confidence > 0:
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return [], "strict_evidence_unavailable"
    top = max(counts.values())
    leaders = sorted(kind for kind, count in counts.items() if count == top)
    if len(leaders) > 1:
        return [], "ambiguous_failure_kind"
    kind = leaders[0]
    if top < policy.minimum_strict_support:
        return [], "strict_evidence_unavailable"
    if _UPDATABLE_KINDS.get(kind) != target.component:
        return [], "no_applicable_strategy"
    strategies = policy.strategies_for(kind)
    if not strategies:
        return [], "no_applicable_strategy"
    enabled = [item for item in strategies if item in policy.enabled_strategies]
    if not enabled:
        return [], "strategy_disabled"
    if champion is None:
        return [], "missing_champion"
    ranked = sorted(
        (item for item in evidence if item[0] == kind and item[1] > 0),
        key=lambda item: (-item[1], item[2]),
    )
    refs = tuple(item[2] for item in ranked)
    made: list[UpdateProposal] = []
    for strategy in enabled:
        if len(made) >= policy.max_proposals_per_target:
            break
        if strategy == "prototype":
            existing = {item.ref for item in champion.prototypes}
            chosen = tuple(ref for ref in refs if ref not in existing)[
                : policy.max_prototypes_per_proposal
            ]
            magnitude = target.confidence
        else:
            chosen = refs[:MAX_STRICT_REFS]
            magnitude = round(policy.max_step(strategy) * target.confidence, 6)
        if not chosen or magnitude <= 0:
            continue
        proposal = UpdateProposal(
            component=target.component,
            scope=target.scope,
            strategy=strategy,
            parameter=PARAMETER_OF_STRATEGY[strategy],
            direction=expected_direction(strategy, kind),
            magnitude=magnitude,
            failure_kind=kind,
            target_confidence=target.confidence,
            strict_support=top,
            strict_refs=chosen,
            champion_version=champion.version,
            champion_sha256=champion.sha256,
            diagnosis_id=diagnosis_id,
            diagnosis_policy_fingerprint=diagnosis_fingerprint,
            update_policy_fingerprint=policy.fingerprint,
            rank=len(made),
        )
        try:
            _apply(proposal, champion, policy.max_prototypes)
        except ChallengerUpdateError:
            continue
        made.append(proposal)
    return made, (None if made else "nothing_to_update")


# {
#   責務: [_check_proposal: 外部で作られた提案も含め、policyと一致する提案だけを受け入れる]
#   処理: [policy指紋、strategyの有効性と表、確定根拠件数、magnitudeの再計算値、prototype件数を照合する]
#   引数: [proposal: 提案, policy: 提案policy] 戻り値: [なし]
#   エラー: [ChallengerUpdateError: invalid_proposal]
# }
def _check_proposal(proposal: UpdateProposal, policy: ChallengerUpdatePolicy) -> None:
    strategy = proposal.strategy
    if strategy == "prototype":
        magnitude_ok = (
            proposal.magnitude == proposal.target_confidence
            and len(proposal.strict_refs) <= policy.max_prototypes_per_proposal
        )
    else:
        magnitude_ok = proposal.magnitude == round(
            policy.max_step(strategy) * proposal.target_confidence, 6
        )
    if (
        proposal.update_policy_fingerprint != policy.fingerprint
        or strategy not in policy.enabled_strategies
        or strategy not in policy.strategies_for(proposal.failure_kind)
        or proposal.strict_support < policy.minimum_strict_support
        or not magnitude_ok
    ):
        raise ChallengerUpdateError(
            "invalid_proposal", "proposal does not match the policy"
        )


# {
#   責務: [_apply: 提案を元parameterへ適用した新しい値を作る（版は元のまま）]
#   処理: [calibrationは倍率を(1∓magnitude)倍、thresholdはmagnitude加算、prototypeは新規refを追加。
#     範囲外・追加0件・容量超過は拒否する。元の値は変更しない]
#   引数: [proposal: 提案, base: 元parameter, max_prototypes: prototype保持上限]
#   戻り値: [ComponentParameters: 版だけ未更新の新しい値]
#   エラー: [ChallengerUpdateError: parameter_out_of_bounds/invalid_proposal/prototype_capacity]
# }
def _apply(
    proposal: UpdateProposal, base: ComponentParameters, max_prototypes: int
) -> ComponentParameters:
    if proposal.strategy == "calibration":
        sign = 1 if proposal.direction == "increase" else -1
        value = round(base.confidence_scale * (1 + sign * proposal.magnitude), 6)
        if not MIN_CONFIDENCE_SCALE <= value <= MAX_CONFIDENCE_SCALE:
            raise ChallengerUpdateError(
                "parameter_out_of_bounds", "confidence scale leaves its range"
            )
        return replace(base, confidence_scale=value)
    if proposal.strategy == "threshold":
        value = round(base.acceptance_threshold + proposal.magnitude, 6)
        if not 0 <= value <= 1:
            raise ChallengerUpdateError(
                "parameter_out_of_bounds", "acceptance threshold leaves its range"
            )
        return replace(base, acceptance_threshold=value)
    existing = {item.ref for item in base.prototypes}
    added = [Prototype(ref) for ref in proposal.strict_refs if ref not in existing]
    if not added:
        raise ChallengerUpdateError("invalid_proposal", "no new prototype to add")
    merged = tuple(sorted((*base.prototypes, *added)))
    if len(merged) > max_prototypes:
        raise ChallengerUpdateError("prototype_capacity", "too many prototypes")
    return replace(base, prototypes=merged)


# {
#   責務: [_challenger_version: 元Championの版と提案IDからChallengerの版を一意に決める]
#   処理: [<元の版>+c.<proposal_idのhex先頭12桁>を返す。長さ・世代の検査は呼出側]
#   引数: [parent_version: 元Championの版, proposal: 提案] 戻り値: [str]
# }
def _challenger_version(parent_version: str, proposal: UpdateProposal) -> str:
    return f"{parent_version}+c.{proposal.proposal_id[len('proposal.') :][:12]}"


# {
#   責務: [_policy: 既定policyを補い、検証済みのpolicy型だけを受け入れる]
#   処理: [Noneは既定値、それ以外は型一致を要求する]
#   引数: [policy: 指定policyか無し] 戻り値: [ChallengerUpdatePolicy]
#   エラー: [ChallengerUpdateError: invalid_policy]
# }
def _policy(policy: ChallengerUpdatePolicy | None) -> ChallengerUpdatePolicy:
    if policy is None:
        return ChallengerUpdatePolicy()
    if type(policy) is not ChallengerUpdatePolicy:
        raise ChallengerUpdateError("invalid_policy", "policy type is invalid")
    return policy


# {
#   責務: [_unique_object: JSON objectの重複keyを拒否する]
#   処理: [json.loadsのobject_pairs_hookとしてkeyの再出現を検出する]
#   引数: [pairs: keyと値の組] 戻り値: [dict] エラー: [ValueError: 重複key]
# }
def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


# {
#   責務: [_reject_constant: NaN・Infinityを拒否する]
#   処理: [json.loadsのparse_constantとして常に拒否する]
#   引数: [name: 定数名] 戻り値: [なし] エラー: [ValueError: 常に]
# }
def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")
