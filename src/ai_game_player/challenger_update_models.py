"""Challenger更新提案・部品parameter・rollback可能なChallenger artifactの版付きの型。

提案、parameter、artifactは同じ#170の出力契約として同時に版を上げるため、
小さな不変dataclassを一つのファイルにまとめる。生成規則はchallenger_update.pyにある。
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
import re
from typing import Any

from ai_game_player.challenger_update_policy import (
    CHALLENGER_UPDATE_RULE_VERSION,
    FAILURE_COMPONENTS,
    MAX_PROPOSAL_REFS,
    NEGATIVE_FAILURE_KINDS,
    STRATEGIES,
)
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis_models import COMPONENTS


CHALLENGER_PROPOSALS_SCHEMA_VERSION = 1
CHALLENGER_ARTIFACT_SCHEMA_VERSION = 1
UPDATABLE_COMPONENTS = ("recognition", "decision")
PARAMETER_OF_STRATEGY = {
    "calibration": "confidence_scale",
    "threshold": "acceptance_threshold",
    "prototype": "prototypes",
}
DIRECTIONS = ("decrease", "increase", "add")
SKIP_REASONS = (
    "ambiguous_failure_kind",
    "missing_champion",
    "no_applicable_strategy",
    "nothing_to_update",
    "strategy_disabled",
    "strict_evidence_unavailable",
)
ERROR_CODES = (
    "invalid_report",
    "invalid_policy",
    "invalid_parameters",
    "stale_champion",
    "scope_mismatch",
    "invalid_proposal",
    "parameter_out_of_bounds",
    "prototype_capacity",
    "version_too_long",
    "invalid_created_at",
    "invalid_artifact",
)
MIN_CONFIDENCE_SCALE = 0.1
MAX_CONFIDENCE_SCALE = 2.0
MAX_PROTOTYPES = 256
MAX_STRICT_REFS = MAX_PROPOSAL_REFS
MAX_LINEAGE_DEPTH = 8
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")
_DIAGNOSIS_ID = re.compile(r"^diagnosis\.[0-9a-f]{64}$")
_CREATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


# {
#   責務: [ChallengerUpdateError: 更新提案・Challenger生成・読込の拒否を固定codeで伝える]
#   フィールド: [code: ERROR_CODESの一つ]
# }
class ChallengerUpdateError(ValueError):
    # {
    #   責務: [__init__: 固定codeと説明を保持する]
    #   処理: [未知のcodeを拒否する] 引数: [code: ERROR_CODESの一つ, message: 説明]
    #   戻り値: [なし] エラー: [ValueError: 未知のcode]
    # }
    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError("unknown challenger update error code")
        super().__init__(f"{code}: {message}")
        self.code = code


# {
#   責務: [Prototype: 部品が参照する検証済みの負例caseを一つ表す]
#   フィールド: [ref: 元Experienceのepisode/step/event参照, label: 本版はnegativeだけ]
# }
@dataclass(frozen=True, slots=True, order=True)
class Prototype:
    ref: DatasetSource
    label: str = "negative"

    # {
    #   責務: [__post_init__: 参照型とlabelを検査する]
    #   処理: [DatasetSource以外とnegative以外を拒否する] 引数: [self: prototype]
    #   戻り値: [なし] エラー: [ChallengerUpdateError: invalid_parameters]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.ref, DatasetSource) or self.label != "negative":
            raise ChallengerUpdateError("invalid_parameters", "prototype is invalid")

    # {
    #   責務: [to_dict: prototypeをJSON互換の辞書にする]
    #   処理: [参照とlabelを並べる] 引数: [self: prototype] 戻り値: [dict[str, Any]]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref.to_dict(), "label": self.label}


# {
#   責務: [ComponentParameters: Champion/Challengerが共有する一部品の不変parameter版]
#   フィールド: [component: recognition/decision, scope: Knowledge隔離scope, version: 版文字列,
#     confidence_scale: 部品の確信度に掛ける校正倍率0.1..2.0,
#     acceptance_threshold: 部品出力を採用する確信度下限0..1,
#     prototypes: 検証済み負例の参照。ref順・重複なし・最大256件]
# }
@dataclass(frozen=True, slots=True)
class ComponentParameters:
    component: str
    scope: KnowledgeScope
    version: str
    confidence_scale: float = 1.0
    acceptance_threshold: float = 0.5
    prototypes: tuple[Prototype, ...] = ()

    # {
    #   責務: [__post_init__: 部品・版・数値範囲・prototype集合を検査する]
    #   処理: [prototypeはref順・一意であることを要求し、暗黙の並べ替えをしない]
    #   引数: [self: parameter版] 戻り値: [なし]
    #   エラー: [ChallengerUpdateError: invalid_parametersまたはparameter_out_of_bounds]
    # }
    def __post_init__(self) -> None:
        if (
            self.component not in UPDATABLE_COMPONENTS
            or not isinstance(self.scope, KnowledgeScope)
            or type(self.version) is not str
            or not _VERSION.fullmatch(self.version)
            or type(self.prototypes) is not tuple
            or any(type(item) is not Prototype for item in self.prototypes)
        ):
            raise ChallengerUpdateError("invalid_parameters", "parameters are invalid")
        refs = [item.ref for item in self.prototypes]
        if refs != sorted(set(refs)):
            raise ChallengerUpdateError(
                "invalid_parameters", "prototypes must be unique and sorted by ref"
            )
        if (
            not _finite(self.confidence_scale)
            or not MIN_CONFIDENCE_SCALE <= self.confidence_scale <= MAX_CONFIDENCE_SCALE
            or not _finite(self.acceptance_threshold)
            or not 0 <= self.acceptance_threshold <= 1
            or len(self.prototypes) > MAX_PROTOTYPES
        ):
            raise ChallengerUpdateError(
                "parameter_out_of_bounds", "parameter value is out of bounds"
            )

    # {
    #   責務: [to_dict: parameter版をJSON互換の辞書にする]
    #   処理: [scopeとprototypeを辞書化する] 引数: [self: parameter版]
    #   戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "scope": self.scope.to_dict(),
            "version": self.version,
            "confidence_scale": self.confidence_scale,
            "acceptance_threshold": self.acceptance_threshold,
            "prototypes": [item.to_dict() for item in self.prototypes],
        }

    # {
    #   責務: [from_dict: 宣言fieldだけからparameter版を復元する]
    #   処理: [field過不足・型を検査し、構築時の検査へ渡す]
    #   引数: [value: JSON由来の辞書] 戻り値: [ComponentParameters: 検証済みの版]
    #   エラー: [ChallengerUpdateError: invalid_parametersなど]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "ComponentParameters":
        _fields(value, _PARAMETER_FIELDS, "invalid_parameters")
        try:
            prototypes = []
            for item in _list(value["prototypes"], "invalid_parameters"):
                _fields(item, _PROTOTYPE_FIELDS, "invalid_parameters")
                prototypes.append(Prototype(_source(item["ref"]), item["label"]))
            return cls(
                component=value["component"],
                scope=KnowledgeScope.from_dict(value["scope"]),
                version=value["version"],
                confidence_scale=value["confidence_scale"],
                acceptance_threshold=value["acceptance_threshold"],
                prototypes=tuple(prototypes),
            )
        except ChallengerUpdateError:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise ChallengerUpdateError("invalid_parameters", str(error)) from error

    # {
    #   責務: [sha256: parameter版の正規化JSONのSHA-256を返す]
    #   処理: [to_dictを正規化してhashする] 引数: [self: parameter版]
    #   戻り値: [str: 64桁の小文字hex]
    # }
    @property
    def sha256(self) -> str:
        return digest(self.to_dict())


# {
#   責務: [ResourceCost: Challenger artifactの決定論的な資源見積りを保持する]
#   フィールド: [parameter_bytes: challengerの正規化JSONのbyte数, delta_bytes: parentからの増加byte数,
#     prototype_count: challengerのprototype件数, compute_class: constantかlinear_in_prototypes]
# }
@dataclass(frozen=True, slots=True)
class ResourceCost:
    parameter_bytes: int
    delta_bytes: int
    prototype_count: int
    compute_class: str

    # {
    #   責務: [to_dict: 見積りをJSON互換の辞書にする]
    #   処理: [fieldをそのまま並べる] 引数: [self: 見積り] 戻り値: [dict[str, Any]]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_bytes": self.parameter_bytes,
            "delta_bytes": self.delta_bytes,
            "prototype_count": self.prototype_count,
            "compute_class": self.compute_class,
        }


# {
#   責務: [UpdateProposal: 一つのcandidate targetに対する一strategyの型付き更新提案]
#   フィールド: [component/scope: 更新先, strategy/parameter/direction: 固定規則で決まる更新の種類,
#     magnitude: 更新量0..1, failure_kind: 根拠の#169種類, target_confidence: candidate確信度,
#     strict_support: 採用kindの検証済み根拠件数, strict_refs: verified_effect根拠の参照,
#     champion_version/champion_sha256: 提案の前提にしたChampion,
#     diagnosis_id/diagnosis_policy_fingerprint: 元の診断, update_policy_fingerprint: 提案policy,
#     rank: 同じtarget内の優先順]
# }
@dataclass(frozen=True, slots=True)
class UpdateProposal:
    component: str
    scope: KnowledgeScope
    strategy: str
    parameter: str
    direction: str
    magnitude: float
    failure_kind: str
    target_confidence: float
    strict_support: int
    strict_refs: tuple[DatasetSource, ...]
    champion_version: str
    champion_sha256: str
    diagnosis_id: str
    diagnosis_policy_fingerprint: str
    update_policy_fingerprint: str
    rank: int

    # {
    #   責務: [__post_init__: 列挙値・組合せ・数値・参照・識別子を検査し、矛盾した提案を拒否する]
    #   処理: [failure_kind→component、strategy→parameter→directionの固定規則と一致させ、
    #     prototypeは負例kindだけに限る。参照は一意・件数上限内・strict_support以下]
    #   引数: [self: 提案] 戻り値: [なし] エラー: [ChallengerUpdateError: invalid_proposal]
    # }
    def __post_init__(self) -> None:
        components = dict(FAILURE_COMPONENTS)
        refs = self.strict_refs
        valid = (
            self.failure_kind in components
            and self.component == components[self.failure_kind]
            and isinstance(self.scope, KnowledgeScope)
            and self.strategy in STRATEGIES
            and self.parameter == PARAMETER_OF_STRATEGY[self.strategy]
            and self.direction == expected_direction(self.strategy, self.failure_kind)
            and (
                self.strategy != "prototype"
                or self.failure_kind in NEGATIVE_FAILURE_KINDS
            )
            and _finite(self.magnitude)
            and 0 < self.magnitude <= 1
            and _finite(self.target_confidence)
            and 0 < self.target_confidence <= 1
            and type(self.strict_support) is int
            and 2 <= self.strict_support <= 65536
            and type(refs) is tuple
            and 1 <= len(refs) <= min(MAX_STRICT_REFS, self.strict_support)
            and all(type(ref) is DatasetSource for ref in refs)
            and len(set(refs)) == len(refs)
            and type(self.champion_version) is str
            and _VERSION.fullmatch(self.champion_version) is not None
            and type(self.champion_sha256) is str
            and _SHA256.fullmatch(self.champion_sha256) is not None
            and type(self.diagnosis_id) is str
            and _DIAGNOSIS_ID.fullmatch(self.diagnosis_id) is not None
            and all(
                type(item) is str and _FINGERPRINT.fullmatch(item) is not None
                for item in (
                    self.diagnosis_policy_fingerprint,
                    self.update_policy_fingerprint,
                )
            )
            and type(self.rank) is int
            and 0 <= self.rank < len(STRATEGIES)
        )
        if not valid:
            raise ChallengerUpdateError(
                "invalid_proposal", "update proposal is invalid"
            )

    # {
    #   責務: [to_dict: 提案をproposal_id付きのJSON互換辞書にする]
    #   処理: [内容を辞書化し、内容のSHA-256先頭32桁をproposal_idとして加える]
    #   引数: [self: 提案] 戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        content = {
            "component": self.component,
            "scope": self.scope.to_dict(),
            "strategy": self.strategy,
            "parameter": self.parameter,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "failure_kind": self.failure_kind,
            "target_confidence": self.target_confidence,
            "strict_support": self.strict_support,
            "strict_refs": [ref.to_dict() for ref in self.strict_refs],
            "champion_version": self.champion_version,
            "champion_sha256": self.champion_sha256,
            "diagnosis_id": self.diagnosis_id,
            "diagnosis_policy_fingerprint": self.diagnosis_policy_fingerprint,
            "update_policy_fingerprint": self.update_policy_fingerprint,
            "rank": self.rank,
        }
        return {"proposal_id": "proposal." + digest(content)[:32], **content}

    # {
    #   責務: [from_dict: proposal_idを含む辞書から提案を復元し、IDの改ざんを拒否する]
    #   処理: [field過不足を検査して構築し、再計算したproposal_idと照合する]
    #   引数: [value: JSON由来の辞書] 戻り値: [UpdateProposal: 検証済み提案]
    #   エラー: [ChallengerUpdateError: invalid_proposal]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "UpdateProposal":
        _fields(value, _PROPOSAL_FIELDS, "invalid_proposal")
        try:
            proposal = cls(
                component=value["component"],
                scope=KnowledgeScope.from_dict(value["scope"]),
                strategy=value["strategy"],
                parameter=value["parameter"],
                direction=value["direction"],
                magnitude=value["magnitude"],
                failure_kind=value["failure_kind"],
                target_confidence=value["target_confidence"],
                strict_support=value["strict_support"],
                strict_refs=tuple(
                    _source(item)
                    for item in _list(value["strict_refs"], "invalid_proposal")
                ),
                champion_version=value["champion_version"],
                champion_sha256=value["champion_sha256"],
                diagnosis_id=value["diagnosis_id"],
                diagnosis_policy_fingerprint=value["diagnosis_policy_fingerprint"],
                update_policy_fingerprint=value["update_policy_fingerprint"],
                rank=value["rank"],
            )
        except ChallengerUpdateError:
            raise
        except (ValueError, TypeError, KeyError) as error:
            raise ChallengerUpdateError("invalid_proposal", str(error)) from error
        if proposal.to_dict() != value:
            raise ChallengerUpdateError("invalid_proposal", "proposal id mismatch")
        return proposal

    # {
    #   責務: [proposal_id: 内容から決まる提案識別子を返す]
    #   処理: [to_dictのproposal_idを返す] 引数: [self: 提案] 戻り値: [str]
    # }
    @property
    def proposal_id(self) -> str:
        return self.to_dict()["proposal_id"]


# {
#   責務: [ObservationRequest: 確定根拠が不足するtargetを、更新せず追加観測の対象として残す]
#   フィールド: [component/scope: #169のtarget, confidence: 上限付きのまま変更しない確信度,
#     strict_support/uncertain_support: 根拠件数, reason: 本版はinsufficient_strict_supportだけ]
# }
@dataclass(frozen=True, slots=True)
class ObservationRequest:
    component: str
    scope: KnowledgeScope
    confidence: float
    strict_support: int
    uncertain_support: int
    reason: str = "insufficient_strict_support"

    # {
    #   責務: [__post_init__: 観測要求のcomponent・確信度・件数・理由を検査する]
    #   処理: [#169のcomponent、有限な0..1の確信度、bool以外の非負整数件数、既知の理由だけを受け入れる]
    #   引数: [self: 観測要求] 戻り値: [なし] エラー: [ChallengerUpdateError: invalid_report]
    # }
    def __post_init__(self) -> None:
        if (
            self.component not in COMPONENTS
            or not isinstance(self.scope, KnowledgeScope)
            or not _finite(self.confidence)
            or not 0 <= self.confidence <= 1
            or any(
                type(value) is not int or value < 0
                for value in (self.strict_support, self.uncertain_support)
            )
            or self.reason != "insufficient_strict_support"
        ):
            raise ChallengerUpdateError(
                "invalid_report", "observation request is invalid"
            )

    # {
    #   責務: [to_dict: 観測要求をJSON互換の辞書にする]
    #   処理: [fieldを並べる] 引数: [self: 観測要求] 戻り値: [dict[str, Any]]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "scope": self.scope.to_dict(),
            "confidence": self.confidence,
            "strict_support": self.strict_support,
            "uncertain_support": self.uncertain_support,
            "reason": self.reason,
        }


# {
#   責務: [UpdateProposalSet: 一つの診断報告から作った提案・観測要求・見送りの版付き集合]
#   フィールド: [diagnosis_id: 元の診断, update_policy_fingerprint: 提案policy, proposals: 提案,
#     observation_requests: 仮説targetの観測要求, skipped: (component, scope_key, reason)の名前順,
#     proposals_truncated: max_proposalsで切ったか, schema_version: 出力契約の版]
# }
@dataclass(frozen=True, slots=True)
class UpdateProposalSet:
    diagnosis_id: str
    update_policy_fingerprint: str
    proposals: tuple[UpdateProposal, ...]
    observation_requests: tuple[ObservationRequest, ...]
    skipped: tuple[tuple[str, str, str], ...]
    proposals_truncated: bool
    schema_version: int = CHALLENGER_PROPOSALS_SCHEMA_VERSION

    # {
    #   責務: [to_dict: 集合をproposal_set_id付きのJSON互換辞書にする]
    #   処理: [内容を辞書化し、内容のSHA-256をproposal_set_idとして加える]
    #   引数: [self: 提案集合] 戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        content = {
            "schema": "challenger-update-proposals",
            "schema_version": self.schema_version,
            "diagnosis_id": self.diagnosis_id,
            "update_policy_fingerprint": self.update_policy_fingerprint,
            "proposals": [item.to_dict() for item in self.proposals],
            "observation_requests": [
                item.to_dict() for item in self.observation_requests
            ],
            "skipped": [list(item) for item in self.skipped],
            "proposals_truncated": self.proposals_truncated,
        }
        return {"proposal_set_id": "proposals." + digest(content), **content}

    # {
    #   責務: [proposal_set_id: 同じ入力とpolicyから同じ値になる集合識別子を返す]
    #   処理: [to_dictのproposal_set_idを返す] 引数: [self: 提案集合] 戻り値: [str]
    # }
    @property
    def proposal_set_id(self) -> str:
        return self.to_dict()["proposal_set_id"]


# {
#   責務: [ChallengerArtifact: Championを変えずに作ったChallengerと、rollback先の元Championを同梱する版]
#   フィールド: [challenger: 新しいparameter版, parent: 元Championのparameter版全体,
#     proposal: 生成に使った提案, builder_rule_version: 生成規則の版, created_at: 呼出側の生成時刻,
#     schema_version: 出力契約の版]
# }
@dataclass(frozen=True, slots=True)
class ChallengerArtifact:
    challenger: ComponentParameters
    parent: ComponentParameters
    proposal: UpdateProposal
    builder_rule_version: int
    created_at: str
    schema_version: int = CHALLENGER_ARTIFACT_SCHEMA_VERSION

    # {
    #   責務: [__post_init__: 型・時刻形式・parentと提案の対応を検査する]
    #   処理: [challenger/parentの部品とscopeが提案と一致し、parentが提案の前提Championであること]
    #   引数: [self: artifact] 戻り値: [なし]
    #   エラー: [ChallengerUpdateError: invalid_artifactまたはinvalid_created_at]
    # }
    def __post_init__(self) -> None:
        if (
            type(self.challenger) is not ComponentParameters
            or type(self.parent) is not ComponentParameters
            or type(self.proposal) is not UpdateProposal
            or type(self.builder_rule_version) is not int
            or self.builder_rule_version != CHALLENGER_UPDATE_RULE_VERSION
            or type(self.schema_version) is not int
            or self.schema_version != CHALLENGER_ARTIFACT_SCHEMA_VERSION
        ):
            raise ChallengerUpdateError("invalid_artifact", "artifact is invalid")
        validate_created_at(self.created_at)
        proposal = self.proposal
        if (
            self.parent.sha256 != proposal.champion_sha256
            or self.parent.version != proposal.champion_version
            or {self.parent.component, self.challenger.component}
            != {proposal.component}
            or self.parent.scope != proposal.scope
            or self.challenger.scope != proposal.scope
        ):
            raise ChallengerUpdateError(
                "invalid_artifact", "artifact does not match its proposal"
            )

    # {
    #   責務: [lineage_depth: parentの版にあるChallenger世代数＋1を返す]
    #   処理: [版文字列中の+c.の出現数を数える] 引数: [self: artifact] 戻り値: [int]
    # }
    @property
    def lineage_depth(self) -> int:
        return self.parent.version.count("+c.") + 1

    # {
    #   責務: [resource_cost: parameter容量とprototype件数から決定論的な資源見積りを返す]
    #   処理: [正規化JSONのbyte数を比べる。時間計測は含めない]
    #   引数: [self: artifact] 戻り値: [ResourceCost]
    # }
    @property
    def resource_cost(self) -> ResourceCost:
        size = len(canonical(self.challenger.to_dict()))
        parent_size = len(canonical(self.parent.to_dict()))
        return ResourceCost(
            parameter_bytes=size,
            delta_bytes=max(size - parent_size, 0),
            prototype_count=len(self.challenger.prototypes),
            compute_class=(
                "linear_in_prototypes"
                if self.proposal.strategy == "prototype"
                else "constant"
            ),
        )

    # {
    #   責務: [rollback_parameters: rollback先の元Champion parameterを返す]
    #   処理: [同梱したparentをそのまま返す。適用とpointer更新は#171が行う]
    #   引数: [self: artifact] 戻り値: [ComponentParameters: 元Champion]
    # }
    def rollback_parameters(self) -> ComponentParameters:
        return self.parent

    # {
    #   責務: [to_dict: artifactをartifact_id付きのJSON互換辞書にする]
    #   処理: [created_atを除いた内容のSHA-256をartifact_idにし、#173向けevaluation_contractを加える]
    #   引数: [self: artifact] 戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        proposal = self.proposal
        content = {
            "schema": "challenger-artifact",
            "schema_version": self.schema_version,
            "status": "challenger",
            "challenger": self.challenger.to_dict(),
            "challenger_sha256": self.challenger.sha256,
            "parent": self.parent.to_dict(),
            "parent_sha256": self.parent.sha256,
            "proposal": proposal.to_dict(),
            "provenance": {
                "builder_rule_version": self.builder_rule_version,
                "update_policy_fingerprint": proposal.update_policy_fingerprint,
                "diagnosis_id": proposal.diagnosis_id,
                "diagnosis_policy_fingerprint": proposal.diagnosis_policy_fingerprint,
                "strict_refs": [ref.to_dict() for ref in proposal.strict_refs],
                "lineage_depth": self.lineage_depth,
            },
            "resource_cost": self.resource_cost.to_dict(),
            "evaluation_contract": {
                "component": proposal.component,
                "scope": proposal.scope.to_dict(),
                "trigger_refs": [ref.to_dict() for ref in proposal.strict_refs],
                "compare": {
                    "champion_sha256": self.parent.sha256,
                    "challenger_sha256": self.challenger.sha256,
                },
            },
        }
        artifact_id = "challenger." + digest(content)
        content["provenance"] = {**content["provenance"], "created_at": self.created_at}
        return {"artifact_id": artifact_id, **content}

    # {
    #   責務: [artifact_id: created_atに依存しないartifact識別子を返す]
    #   処理: [to_dictのartifact_idを返す] 引数: [self: artifact] 戻り値: [str]
    # }
    @property
    def artifact_id(self) -> str:
        return self.to_dict()["artifact_id"]

    # {
    #   責務: [from_dict: 保存辞書からartifactを復元し、派生fieldの改ざんを拒否する]
    #   処理: [必要fieldから構築し、to_dictの再生成結果が入力と完全一致することを要求する]
    #   引数: [value: JSON由来の辞書] 戻り値: [ChallengerArtifact: 検証済みartifact]
    #   エラー: [ChallengerUpdateError: invalid_artifact]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "ChallengerArtifact":
        _fields(value, _ARTIFACT_FIELDS, "invalid_artifact")
        try:
            provenance = value["provenance"]
            if not isinstance(provenance, dict):
                raise ValueError("provenance must be an object")
            artifact = cls(
                challenger=ComponentParameters.from_dict(value["challenger"]),
                parent=ComponentParameters.from_dict(value["parent"]),
                proposal=UpdateProposal.from_dict(value["proposal"]),
                builder_rule_version=provenance["builder_rule_version"],
                created_at=provenance["created_at"],
                schema_version=value["schema_version"],
            )
        except ChallengerUpdateError as error:
            raise ChallengerUpdateError("invalid_artifact", str(error)) from error
        except (ValueError, TypeError, KeyError) as error:
            raise ChallengerUpdateError("invalid_artifact", str(error)) from error
        if artifact.to_dict() != value:
            raise ChallengerUpdateError(
                "invalid_artifact", "artifact content does not match its digest"
            )
        return artifact


# {
#   責務: [expected_direction: strategyとfailure_kindから更新方向を一意に決める]
#   処理: [prototypeはadd、thresholdは採用を厳しくするincrease、calibrationは過小評価だけincrease]
#   引数: [strategy: STRATEGIESの一つ, failure_kind: 更新可能なfailure_kind]
#   戻り値: [str: DIRECTIONSの一つ]
# }
def expected_direction(strategy: str, failure_kind: str) -> str:
    if strategy == "prototype":
        return "add"
    if strategy == "threshold" or failure_kind == "decision_underestimated":
        return "increase"
    return "decrease"


# {
#   責務: [validate_created_at: 呼出側が渡す生成時刻をUTCのZ表記に限る]
#   処理: [字面と実在する日時かを確認する] 引数: [value: 時刻文字列] 戻り値: [なし]
#   エラー: [ChallengerUpdateError: invalid_created_at]
# }
def validate_created_at(value: object) -> None:
    if type(value) is not str or not _CREATED_AT.fullmatch(value):
        raise ChallengerUpdateError("invalid_created_at", "created_at must be UTC Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ChallengerUpdateError("invalid_created_at", str(error)) from error


# {
#   責務: [canonical: JSON互換値をkey順・区切り固定・NaN拒否のUTF-8 bytesにする]
#   処理: [json.dumpsを固定設定で呼ぶ] 引数: [value: JSON互換値] 戻り値: [bytes]
# }
def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")


# {
#   責務: [digest: JSON互換値の正規化表現からSHA-256を作る]
#   処理: [canonicalの結果をhashする] 引数: [value: JSON互換値] 戻り値: [str: 64桁の小文字hex]
# }
def digest(value: Any) -> str:
    return sha256(canonical(value)).hexdigest()


# {
#   責務: [component_order: 出力の並びに使うcomponentの順位を返す]
#   処理: [#169のCOMPONENTSの順を使う] 引数: [component: component名] 戻り値: [int]
# }
def component_order(component: str) -> int:
    return COMPONENTS.index(component)


_PROTOTYPE_FIELDS = frozenset({"ref", "label"})
_PARAMETER_FIELDS = frozenset(
    {
        "component",
        "scope",
        "version",
        "confidence_scale",
        "acceptance_threshold",
        "prototypes",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {
        "proposal_id",
        "component",
        "scope",
        "strategy",
        "parameter",
        "direction",
        "magnitude",
        "failure_kind",
        "target_confidence",
        "strict_support",
        "strict_refs",
        "champion_version",
        "champion_sha256",
        "diagnosis_id",
        "diagnosis_policy_fingerprint",
        "update_policy_fingerprint",
        "rank",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "schema",
        "schema_version",
        "status",
        "challenger",
        "challenger_sha256",
        "parent",
        "parent_sha256",
        "proposal",
        "provenance",
        "resource_cost",
        "evaluation_contract",
    }
)


# {
#   責務: [_finite: 値がboolでない有限の数値かを返す]
#   処理: [型と有限性を確認する] 引数: [value: 検査値] 戻り値: [bool]
# }
def _finite(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value)


# {
#   責務: [_fields: 辞書のfield集合が契約と完全一致することを検査する]
#   処理: [辞書以外と過不足を拒否する]
#   引数: [value: 検査値, expected: field集合, code: 拒否code] 戻り値: [なし]
#   エラー: [ChallengerUpdateError: 指定code]
# }
def _fields(value: object, expected: frozenset[str], code: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ChallengerUpdateError(code, "fields do not match the contract")


# {
#   責務: [_list: JSON配列であることを検査して返す]
#   処理: [list以外を拒否する] 引数: [value: 検査値, code: 拒否code] 戻り値: [list]
#   エラー: [ChallengerUpdateError: 指定code]
# }
def _list(value: object, code: str) -> list:
    if type(value) is not list:
        raise ChallengerUpdateError(code, "expected a list")
    return value


# {
#   責務: [_source: 辞書からDatasetSourceを復元する]
#   処理: [既存DatasetSource.from_dictの検査を使う] 引数: [value: JSON由来の辞書]
#   戻り値: [DatasetSource] エラー: [ValueError: 不正な参照]
# }
def _source(value: object) -> DatasetSource:
    if type(value) is not dict:
        raise ValueError("dataset source must be an object")
    return DatasetSource.from_dict(value)
