"""Experienceの新しい操作効果だけから、昇格前のKnowledge候補を生成する。"""

from dataclasses import dataclass
from hashlib import sha256
from itertools import islice
from typing import Iterable

from ai_game_player.experience import ExperienceEvent
from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.knowledge_update_sample import (
    KnowledgeUpdateSample,
    OUTCOME_EVENT_TYPE,
)
from ai_game_player.knowledge_update_statistics import (
    estimate_uncertainty,
    update_action_statistics,
)


UPDATE_RULE = "beta-ewma"
UPDATE_RULE_VERSION = "1"


# {
#   責務: [KnowledgeBuildResult: 更新候補と、入力を採用・重複排除・無視した件数を呼出側へ返す]
#   フィールド: [snapshot: 昇格していない候補。証拠が無ければ元SnapshotまたはNone,
#     accepted_events: 独立な操作結果として数えた件数, duplicate_events: source/相関系列で既に数えた件数,
#     ignored_events: 対象外eventまたは既定で除外したsensitive証拠の件数]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeBuildResult:
    snapshot: KnowledgeSnapshot | None
    accepted_events: int
    duplicate_events: int
    ignored_events: int


# {
#   責務: [KnowledgeBuilder: モデル再学習や全履歴走査なしに有界のExperience batchから数値Knowledge候補を作る]
#   フィールド: [policy: 証拠採用・項目数・一件の更新幅を制限する不変方針]
# }
class KnowledgeBuilder:
    # {
    #   責務: [__init__: 増分統計の資源・証拠方針を固定する]
    #   処理: [指定が無ければ有界の既定方針を使う]
    #   引数: [policy: 任意の検証済みBuilder方針]
    #   戻り値: [なし]
    # }
    def __init__(self, policy: KnowledgeBuilderPolicy | None = None) -> None:
        self.policy = policy or KnowledgeBuilderPolicy()

    # {
    #   責務: [build: 新しい独立証拠だけを集計し、元Snapshotを変更せず次の候補を返す]
    #   処理: [有界batchとscopeを検証し、永続watermarkで再投入・相関sampleを排除して統計と出所を更新する]
    #   引数: [previous: 元Snapshot。初回はNone, events: Reader等が選んだ新しいcanonical eventのbatch,
    #     scope: game/scene/profile/Player系統の完全一致範囲, version: 新しい候補版,
    #     created_at: 候補のUTC作成時刻, expires_at: 初回の任意期限。既存期限を延長しない]
    #   戻り値: [KnowledgeBuildResult: 昇格前候補と件数。全て既投入なら元Snapshotをそのまま返す]
    #   エラー: [ValueError: batch/項目上限、不正event、scope不一致、版不一致。失敗時も元Snapshotは不変]
    # }
    def build(
        self,
        previous: KnowledgeSnapshot | None,
        events: Iterable[ExperienceEvent],
        *,
        scope: KnowledgeScope,
        version: str,
        created_at: str,
        expires_at: str | None = None,
    ) -> KnowledgeBuildResult:
        validate_identifier(version, "candidate version")
        if previous is not None and previous.scope != scope:
            raise ValueError("knowledge update scope does not match previous snapshot")
        batch = list(islice(events, self.policy.max_batch_events + 1))
        if len(batch) > self.policy.max_batch_events:
            raise ValueError("knowledge batch capacity reached")
        if previous is not None:
            data = previous.to_dict()
            if (
                data["update_rule"] != UPDATE_RULE
                or data["update_rule_version"].split(".", 1)[0] != UPDATE_RULE_VERSION
            ):
                raise ValueError("knowledge update rule requires explicit migration")
            if expires_at is not None and expires_at != previous.expires_at:
                raise ValueError(
                    "knowledge update cannot change an existing retention deadline"
                )
        else:
            data = _initial_data(scope, version, created_at, expires_at)
        initial_watermarks = {
            name: dict(value) for name, value in data["builder_state"].items()
        }
        accepted = duplicate = ignored = 0
        for event in batch:
            if not isinstance(event, ExperienceEvent):
                raise ValueError("knowledge inputs must be canonical Experience events")
            if len(event.artifacts) > 64:
                raise ValueError("knowledge event artifact reference capacity reached")
            if event.event_type != OUTCOME_EVENT_TYPE or (
                not self.policy.allow_sensitive_sources
                and any(reference.sensitive for reference in event.artifacts)
            ):
                ignored += 1
                continue
            sample = KnowledgeUpdateSample.from_event(event)
            if sample.scope != scope:
                raise ValueError("experience scope does not match knowledge scope")
            if sample.execution_status not in {"executed", "dispatch_unknown"}:
                ignored += 1
                continue
            if not _accept_sequence(data, sample, self.policy):
                duplicate += 1
                continue
            trusted = (
                sample.execution_status == "executed"
                and sample.verified
                and sample.confidence >= self.policy.minimum_confidence
                and sample.evidence_kind in self.policy.trusted_evidence_sources
                and sample.provenance_source in self.policy.trusted_evidence_sources
            )
            outcome = sample.outcome if trusted else "unknown"
            update_action_statistics(data, sample, outcome, self.policy)
            _record_source(data, sample, self.policy)
            accepted += 1
        if accepted == 0 and data["builder_state"] == initial_watermarks:
            return KnowledgeBuildResult(previous, 0, duplicate, ignored)
        if previous is not None and previous.version == version:
            raise ValueError("a knowledge update must use a new version")
        data["version"] = version
        data["update_rule_version"] = f"{UPDATE_RULE_VERSION}.{self.policy.fingerprint}"
        data["created_at"] = created_at
        data["uncertainty"] = estimate_uncertainty(data)
        provenance = ":".join(
            item["sha256"]
            for item in sorted(
                data["source_experience_range"], key=lambda item: item["source_id"]
            )
        )
        data["snapshot_id"] = (
            "knowledge."
            + sha256(f"{scope.key}:{version}:{provenance}".encode()).hexdigest()
        )
        return KnowledgeBuildResult(
            KnowledgeSnapshot.from_dict(data), accepted, duplicate, ignored
        )


# {
#   責務: [_initial_data: 指定scopeの空の数値Knowledgeをschema v1で構成する]
#   処理: [モデル・Safety・既存actionを仮定せず、全件数0と不確かさ1で初期化する]
#   引数: [scope: 隔離範囲, version: 初版識別子, created_at: UTC作成時刻, expires_at: 任意の利用期限]
#   戻り値: [dict: 最初の候補生成に使う独立辞書]
# }
def _initial_data(
    scope: KnowledgeScope, version: str, created_at: str, expires_at: str | None
) -> dict:
    return {
        "schema_version": 1,
        "snapshot_id": "knowledge.initial",
        "version": version,
        "scope": scope.to_dict(),
        "created_at": created_at,
        "expires_at": expires_at,
        "sample_count": 0,
        "uncertainty": 1.0,
        "action_outcome_stats": {},
        "candidate_priors": {},
        "candidate_reliability": {},
        "transition_priors": [],
        "weight_hints": {},
        "threshold_hints": {},
        "roi_hints": {},
        "source_experience_range": [],
        "update_rule": UPDATE_RULE,
        "update_rule_version": UPDATE_RULE_VERSION,
        "builder_state": {"source_watermarks": {}, "correlation_watermarks": {}},
    }


# {
#   責務: [_accept_sequence: 同じsourceの再投入と同じ操作の相関観測を永続watermarkで排除する]
#   処理: [古い順序は拒否し、新しいsource順序を記録する。同じ操作の別frameも相関順序が増えなければ数えない]
#   引数: [data: Snapshot候補, sample: sourceと相関系列を明示した証拠, policy: 系列の保持上限]
#   戻り値: [bool: 独立な新証拠として集計する場合True]
#   エラー: [ValueError: 新しいsourceまたは相関系列が上限を超える]
# }
def _accept_sequence(
    data: dict, sample: KnowledgeUpdateSample, policy: KnowledgeBuilderPolicy
) -> bool:
    sources = data["builder_state"]["source_watermarks"]
    correlations = data["builder_state"]["correlation_watermarks"]
    if sample.source_sequence <= sources.get(sample.source_id, -1):
        return False
    if sample.source_id not in sources and len(sources) >= policy.max_sources:
        raise ValueError("knowledge source watermark capacity reached")
    if (
        sample.correlation_id not in correlations
        and len(correlations) >= policy.max_correlations
    ):
        raise ValueError("knowledge correlation watermark capacity reached")
    sources[sample.source_id] = sample.source_sequence
    if sample.correlation_sequence <= correlations.get(sample.correlation_id, -1):
        return False
    correlations[sample.correlation_id] = sample.correlation_sequence
    return True


# {
#   責務: [_record_source: 採用した証拠の範囲と指紋を、元payloadを保持せず増分更新する]
#   処理: [sourceごとに範囲と採用件数を集約し、既存hashと新証拠hashを連鎖させる]
#   引数: [data: Snapshot候補, sample: 今回採用した一証拠, policy: source範囲の上限]
#   戻り値: [なし。sample_countはsource採用件数の合計と一致する]
#   エラー: [ValueError: source範囲の保持上限を超える]
# }
def _record_source(
    data: dict, sample: KnowledgeUpdateSample, policy: KnowledgeBuilderPolicy
) -> None:
    records = data["source_experience_range"]
    current = next(
        (item for item in records if item["source_id"] == sample.source_id), None
    )
    if current is None:
        if len(records) >= policy.max_sources:
            raise ValueError("knowledge provenance capacity reached")
        current = {
            "source_id": sample.source_id,
            "first_sequence": sample.source_sequence,
            "last_sequence": sample.source_sequence,
            "event_count": 0,
            "sha256": "0" * 64,
        }
        records.append(current)
    current["last_sequence"] = sample.source_sequence
    current["event_count"] += 1
    current["sha256"] = sha256(
        bytes.fromhex(current["sha256"]) + bytes.fromhex(sample.evidence_digest)
    ).hexdigest()
    data["sample_count"] += 1
    records.sort(key=lambda item: item["source_id"])
