"""Knowledge昇格の成果・回帰・資源条件。Safety違反は設定で許可しない。"""

from dataclasses import dataclass
from math import isfinite

from ai_game_player.knowledge_evaluation import (
    EVALUATION_GROUPS,
    KnowledgeEvaluationReport,
)
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot


# {
#   責務: [KnowledgePromotionPolicy: 知識の小標本・悪化・資源超過を昇格前に拒否する]
#   フィールド: [min_cases: 全評価ケース最低件数, min_samples: Challengerの経験最低件数,
#     max_uncertainty: 許容する知識の残余不確かさ, min_improvement: replay群で必要な平均成果改善,
#     max_latency_ms/max_python_bytes: 評価中Providerの実測資源上限,
#     max_latency_ratio/max_memory_ratio: Championに対する資源増加倍率の上限]
# }
@dataclass(frozen=True)
class KnowledgePromotionPolicy:
    min_cases: int = 12
    min_samples: int = 3
    max_uncertainty: float = 0.9
    min_improvement: float = 0.01
    max_latency_ms: float = 1000.0
    max_python_bytes: int = 16 * 1024 * 1024
    max_latency_ratio: float = 5.0
    max_memory_ratio: float = 5.0

    # {
    #   責務: [__post_init__: 無限予算や不正な数値設定を拒否する]
    #   処理: [必要群数以上のケース、最低標本と有限正数の上限を要求する]
    #   引数: [self: 判定設定]
    #   戻り値: [なし]
    #   エラー: [ValueError: 設定が契約外]
    # }
    def __post_init__(self) -> None:
        if type(self.min_cases) is not int or not 4 <= self.min_cases <= 512:
            raise ValueError("knowledge promotion requires 4..512 cases")
        if type(self.min_samples) is not int or self.min_samples < 3:
            raise ValueError(
                "knowledge promotion requires at least three experience samples"
            )
        if type(self.max_python_bytes) is not int or self.max_python_bytes < 1:
            raise ValueError("knowledge promotion memory budget must be positive")
        for value in (
            self.max_uncertainty,
            self.min_improvement,
            self.max_latency_ms,
            self.max_latency_ratio,
            self.max_memory_ratio,
        ):
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(
                    "knowledge promotion thresholds must be finite and positive"
                )
        if self.max_uncertainty > 1 or self.min_improvement > 2:
            raise ValueError(
                "knowledge promotion uncertainty/improvement is outside its range"
            )

    # {
    #   責務: [assess: 自己申告の合格値を使わず対照測定から昇格可否を判断する]
    #   処理: [対象digestと標本、全評価群、境界違反ゼロ、全群非悪化、改善、資源を照合する]
    #   引数: [champion/challenger: 比較対象の保存版, report: Gateが実行して生成した測定]
    #   戻り値: [str: acceptedなら採用可能、それ以外は固定の却下理由]
    # }
    def assess(
        self,
        champion: KnowledgeSnapshot,
        challenger: KnowledgeSnapshot,
        report: KnowledgeEvaluationReport,
    ) -> str:
        if report.scope != champion.scope or report.scope != challenger.scope:
            return "scope_mismatch"
        if (
            report.champion_sha256 != champion.sha256
            or report.challenger_sha256 != challenger.sha256
        ):
            return "snapshot_mismatch"
        if (
            challenger.sample_count < self.min_samples
            or challenger.uncertainty > self.max_uncertainty
        ):
            return "insufficient_knowledge"
        if report.case_count < self.min_cases:
            return "insufficient_cases"
        groups = {
            name: (count, before, after)
            for name, count, before, after in report.group_quality
        }
        if set(groups) != EVALUATION_GROUPS or any(
            count < 1 for count, _, _ in groups.values()
        ):
            return "missing_evaluation_group"
        if report.invalid_decisions[1] or report.execution_errors[1]:
            return "unsafe_or_failed_decision"
        if report.invalid_decisions[0] or report.execution_errors[0]:
            return "invalid_baseline_comparison"
        if any(after < before for _, before, after in groups.values()):
            return "quality_regression"
        if groups["replay"][2] - groups["replay"][1] < self.min_improvement:
            return "no_target_improvement"
        old_latency, new_latency = report.latency_p95_ms
        old_memory, new_memory = report.peak_python_bytes
        if (
            new_latency > self.max_latency_ms
            or new_latency > max(old_latency, 0.1) * self.max_latency_ratio
        ):
            return "latency_budget_exceeded"
        if (
            new_memory > self.max_python_bytes
            or new_memory > max(old_memory, 1024) * self.max_memory_ratio
        ):
            return "memory_budget_exceeded"
        return "accepted"
