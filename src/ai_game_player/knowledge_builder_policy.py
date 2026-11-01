"""増分Knowledge更新の計算・メモリ・証拠採用量を制限する方針。"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite

from ai_game_player.knowledge_scope import validate_identifier


# {
#   責務: [KnowledgeBuilderPolicy: 一件の経験と一回の更新がKnowledgeを過大に変更しない上限を保持する]
#   フィールド: [max_batch_events: 一回に読むevent数上限, max_actions/max_sources/max_correlations/max_transitions: 保持項目上限,
#     minimum_confidence: 効果を既知とみなす外部証拠の最低確信度, ewma_rate: 過去値へ新証拠を混ぜる最大割合,
#     prior_evidence_scale: 少数証拠のpriorを0へ縮める件数尺度, max_prior_delta: 一証拠によるprior/hint変化の絶対上限,
#     no_effect_reward: 失敗と区別して無効操作へ与えるsoft報酬, trusted_evidence_sources: 操作効果を検証する登録済みprovenance名,
#     allow_sensitive_sources: 明示指定時だけsensitive artifactに由来する経験の数値集計を許す設定]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeBuilderPolicy:
    max_batch_events: int = 256
    max_actions: int = 256
    max_sources: int = 128
    max_correlations: int = 128
    max_transitions: int = 1024
    minimum_confidence: float = 0.6
    ewma_rate: float = 0.1
    prior_evidence_scale: float = 8.0
    max_prior_delta: float = 0.1
    no_effect_reward: float = -0.25
    trusted_evidence_sources: frozenset[str] = frozenset(
        {"human_correction", "deterministic_action_effect", "calibrated_action_effect"}
    )
    allow_sensitive_sources: bool = False

    # {
    #   責務: [__post_init__: schema上限と有界更新の前提を方針生成時に検証する]
    #   処理: [整数・確率・件数尺度・信頼元の型と範囲を検査する]
    #   引数: [self: 構築中の方針]
    #   戻り値: [なし]
    #   エラー: [ValueError: 非有限数、範囲外または不正な型]
    # }
    def __post_init__(self) -> None:
        for name, limit in (
            ("max_batch_events", 4096),
            ("max_actions", 4096),
            ("max_sources", 1024),
            ("max_correlations", 1024),
            ("max_transitions", 4096),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError(f"{name} is outside its bounded contract")
        for name in ("minimum_confidence", "ewma_rate", "max_prior_delta"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not 0 < value <= 1
                or not isfinite(value)
            ):
                raise ValueError(f"{name} must be finite in (0, 1]")
        if (
            type(self.prior_evidence_scale) not in (int, float)
            or not 1 <= self.prior_evidence_scale <= 1_000_000_000_000
            or not isfinite(self.prior_evidence_scale)
        ):
            raise ValueError("prior_evidence_scale must be finite in [1, 1e12]")
        if (
            type(self.no_effect_reward) not in (int, float)
            or not -1 <= self.no_effect_reward <= 0
            or not isfinite(self.no_effect_reward)
        ):
            raise ValueError("no_effect_reward must be finite between -1 and 0")
        if (
            not isinstance(self.trusted_evidence_sources, frozenset)
            or not self.trusted_evidence_sources
        ):
            raise ValueError("trusted_evidence_sources must be a non-empty frozenset")
        for source in self.trusted_evidence_sources:
            validate_identifier(source, "trusted evidence source")
        if type(self.allow_sensitive_sources) is not bool:
            raise ValueError("allow_sensitive_sources must be boolean")

    # {
    #   責務: [fingerprint: 更新量と信頼元を変更した候補を同一アルゴリズム内で識別する]
    #   処理: [方針の全フィールドを順序固定JSONへ変換してSHA-256を計算する]
    #   引数: [self: 使用するBuilder方針]
    #   戻り値: [str: Snapshotのupdate_rule_versionへ含める24文字の指紋]
    # }
    @property
    def fingerprint(self) -> str:
        fields = asdict(self)
        fields["trusted_evidence_sources"] = sorted(self.trusted_evidence_sources)
        return sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
