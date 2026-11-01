from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionDecision
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.planning_outcome import PlanningOutcome

BASELINE_PROVIDER_ID = "baseline_weighted_score"
BASELINE_VERSION = "1"
SELECT = "select"
ABSTAIN = "abstain"
OBSERVE_MORE = "observe_more"
DEFAULT_WEIGHTS: dict[str, float] = {
    "utility": 1.0,
    "recognition": 0.3,
    "knowledge_prior": 0.5,
}


# {
#   責務: [
#     BaselineProviderProfile: 学習Model・GPU・通信なしで動く判断元が、満たす条件と対応する判断能力を機械可読に宣言する
#   ]
#   フィールド: [
#     provider_kind: deterministic・quantitative・decision_treeのいずれか,
#     supported_capabilities: この判断元がModelなしで成立させる判断能力名,
#     expected_latency_class: 1回の判断に要する時間の区分,
#     fallback_priority: 小さいほどModel不可時に先に使う優先順位,
#     deterministic_version: 同じ入力から同じ結果を返す実装の版
#   ]
# }
@dataclass(frozen=True)
class BaselineProviderProfile:
    provider_id: str
    provider_kind: str
    supported_capabilities: tuple[str, ...]
    expected_latency_class: str = "microseconds"
    required_features: tuple[str, ...] = (
        "candidates.utility_score",
        "candidates.recognition_confidence",
    )
    optional_knowledge_inputs: tuple[str, ...] = ("knowledge_snapshot",)
    fallback_priority: int = 100
    deterministic_version: str = BASELINE_VERSION
    requires_learned_model: bool = False
    requires_gpu: bool = False
    requires_network: bool = False

    # {
    #   責務: [__post_init__: Profileがモデル・GPU・通信を要求せず、種別が既知であることを生成時に保証する]
    #   引数: []
    #   戻り値: [なし]
    #   エラー: [ValueError: provider_kindが未知の値、またはrequires_learned_model・requires_gpu・requires_networkのいずれかがTrueの場合]
    # }
    def __post_init__(self) -> None:
        if self.provider_kind not in {"deterministic", "quantitative", "decision_tree"}:
            raise ValueError(
                "provider_kind must be deterministic, quantitative or decision_tree"
            )
        if self.requires_learned_model or self.requires_gpu or self.requires_network:
            raise ValueError(
                "a baseline provider must not require a learned model, GPU or network"
            )

    # {
    #   責務: [to_dict: Profileの宣言をJSON化できる辞書へ変換し、機械可読な能力確認に使えるようにする]
    #   引数: []
    #   戻り値: [dict: 全フィールドを含む辞書。タプルはlistに変換される]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "requires_learned_model": self.requires_learned_model,
            "requires_gpu": self.requires_gpu,
            "requires_network": self.requires_network,
            "supported_capabilities": list(self.supported_capabilities),
            "expected_latency_class": self.expected_latency_class,
            "required_features": list(self.required_features),
            "optional_knowledge_inputs": list(self.optional_knowledge_inputs),
            "fallback_priority": self.fallback_priority,
            "deterministic_version": self.deterministic_version,
        }


# {
#   責務: [
#     BaselineDecision: 1回のModel不要判断の結果と、採用規則・候補別スコア・寄与内訳を保持する
#   ]
#   フィールド: [
#     kind: select（候補を選ぶ）・abstain（選ばない）・observe_more（再観測を求める）,
#     selected_action_id: kindがselectのときだけ許可候補のID,
#     score_by_action: 許可候補ごとの総合スコア,
#     margin: 最良と次点のスコア差。候補が1件なら最良スコア,
#     confidence: 選択候補の認識確信度と評価確信度の小さい方,
#     rule_id: 結論を決めた規則名,
#     contributions: 候補ごとのスコア寄与の内訳,
#     knowledge_snapshot_version: 利用したKnowledge Snapshotの版。未使用または不正ならNone,
#     provenance: 入力の扱い（Snapshot拒否など）を説明する文
#   ]
# }
@dataclass(frozen=True)
class BaselineDecision:
    kind: str
    selected_action_id: str | None
    score_by_action: dict[str, float]
    margin: float
    confidence: float
    rule_id: str
    provider_id: str
    version: str
    contributions: dict[str, dict[str, float]] = field(default_factory=dict)
    knowledge_snapshot_version: str | None = None
    provenance: tuple[str, ...] = ()

    # {
    #   責務: [to_dict: 判断結果と採用規則・スコア内訳をTraceへ保存できる辞書へ変換する]
    #   引数: []
    #   戻り値: [dict: 全フィールドを含むJSON化可能な辞書]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "selected_action_id": self.selected_action_id,
            "score_by_action": self.score_by_action,
            "margin": self.margin,
            "confidence": self.confidence,
            "rule_id": self.rule_id,
            "provider_id": self.provider_id,
            "version": self.version,
            "contributions": self.contributions,
            "knowledge_snapshot_version": self.knowledge_snapshot_version,
            "provenance": list(self.provenance),
        }


# {
#   責務: [
#     BaselineWithheld: Baselineが候補を選ばなかったことを、既存EngineのValueError経路へ安全に伝える
#   ]
#   フィールド: [decision: 保留（abstainまたはobserve_more）の理由と内訳を持つ判断]
# }
class BaselineWithheld(DecisionWithheld):
    # {
    #   責務: [__init__: 保留した判断を保持し、保留の種別と規則名を含む例外文を作る]
    #   引数: [decision: 選択しなかったBaselineDecision（abstainまたはobserve_more）]
    #   戻り値: [なし]
    # }
    def __init__(self, decision: BaselineDecision) -> None:
        super().__init__(
            DecisionDisposition(
                PlanningOutcome(decision.kind),
                (decision.rule_id,),
                decision.provider_id,
            )
        )
        self.decision = decision


# {
#   責務: [
#     BaselineDecisionProvider: 学習Modelなしで、許可候補の評価値・認識確信度・Knowledge priorの重み付き和から候補を選ぶ参照実装
#   ]
#   フィールド: [
#     weights: utility・recognition・knowledge_priorの各重み,
#     min_margin: 最良と次点の差がこの値未満ならabstainする閾値。0なら同点でも選ぶ,
#     min_recognition: 最良候補の認識確信度がこの値未満ならobserve_moreする閾値,
#     profile: 機械可読な能力宣言
#   ]
#   処理: [
#     1: DecisionContextの許可候補だけを採点する
#     2: 選択前に認識確信度とスコア差を検査し、不足なら選ばず保留を返す
#     3: 規則名とスコア内訳をBaselineDecisionに残す
#   ]
# }
class BaselineDecisionProvider:
    # {
    #   責務: [__init__: 重みと閾値を検証して保持し、Model不要を宣言するProfileを作る]
    #   引数: [
    #     weights: utility・recognition・knowledge_priorの重みの上書き。有限な0以上の値のみ,
    #     min_margin: 最良と次点のスコア差がこの値未満ならabstainする閾値,
    #     min_recognition: 最良候補の認識確信度がこの値未満ならobserve_moreする閾値（0〜1）,
    #     knowledge_snapshot: 任意のKnowledge Snapshot。versionとpriorsを持つ辞書,
    #     fallback_priority: Model不可時に使う順序。小さいほど先
    #     evaluation_weight: 同stepの候補hypothetical品質だけへのsoft係数0..10。actualには使わない
    #   ]
    #   戻り値: [なし]
    #   エラー: [ValueError: 未知の重み名、非有限または負の値、範囲外の閾値の場合]
    # }
    def __init__(
        self,
        weights: Mapping[str, float] | None = None,
        min_margin: float = 0.05,
        min_recognition: float = 0.6,
        knowledge_snapshot: Mapping[str, Any] | None = None,
        fallback_priority: int = 100,
        evaluation_weight: float = 1.0,
    ) -> None:
        merged = dict(DEFAULT_WEIGHTS)
        for key, raw in (weights or {}).items():
            if key not in DEFAULT_WEIGHTS:
                raise ValueError(f"unknown baseline weight: {key}")
            value = float(raw)
            if not isfinite(value) or value < 0:
                raise ValueError(
                    f"baseline weight must be finite and non-negative: {key}"
                )
            merged[key] = value
        if not isfinite(min_margin) or min_margin < 0:
            raise ValueError("min_margin must be finite and non-negative")
        if not isfinite(min_recognition) or not 0 <= min_recognition <= 1:
            raise ValueError("min_recognition must be between 0 and 1")
        self.weights = merged
        if (
            type(evaluation_weight) not in (int, float)
            or not isfinite(evaluation_weight)
            or not 0 <= evaluation_weight <= 10
        ):
            raise ValueError("runtime evaluation weight must be finite in 0..10")
        self.evaluation_weight = evaluation_weight
        self.min_margin = min_margin
        self.min_recognition = min_recognition
        self.knowledge_snapshot = knowledge_snapshot
        self.profile = BaselineProviderProfile(
            BASELINE_PROVIDER_ID,
            "quantitative",
            ("candidate_choice", "candidate_scoring", "observe_more_abstain"),
            fallback_priority=fallback_priority,
        )

    # {
    #   責務: [decide: 許可候補を採点し、選択・abstain・observe_moreのいずれかとその根拠を返す]
    #   処理: [
    #     1: step固定のKnowledge投影を優先し、不正・別step・fallbackなら静的判断へ戻す
    #     2: 許可候補ごとにutility・recognition・knowledge_priorの寄与を計算して合計する
    #     3: 最良候補の認識確信度が低ければobserve_more、スコア差が小さければabstainにする
    #   ]
    #   引数: [context: 許可状態・評価値・認識確信度を含む判断材料。許可されていない候補は採点しない]
    #   戻り値: [BaselineDecision: 判断の種別・候補別スコア・採用規則・寄与内訳]
    #   エラー: [ValueError: 許可候補が1件もない場合]
    # }
    def decide(self, context: DecisionContext) -> BaselineDecision:
        allowed_ids = set(context.allowed_action_ids)
        allowed = [
            c for c in context.candidates if c.allowed and c.action_id in allowed_ids
        ]
        if not allowed:
            raise ValueError("許可された操作候補がありません")
        priors, version, provenance = self._read_snapshot(context)
        weights = self._step_weights(context)
        contributions: dict[str, dict[str, float]] = {}
        scores: dict[str, float] = {}
        qualities = (
            context.evaluation_projection.candidate_qualities(context.snapshot_id)
            if context.evaluation_projection is not None
            else {}
        )
        if any(quality.available for quality in qualities.values()):
            provenance.append("runtime_evaluation/v1; candidate_hypothetical_only")
        for candidate in allowed:
            parts = {
                "utility": weights["utility"] * candidate.utility_score,
                "recognition": weights["recognition"]
                * (candidate.recognition_confidence - 0.5),
                "knowledge_prior": weights["knowledge_prior"]
                * priors.get(candidate.action_id, 0.0),
            }
            quality = qualities.get(candidate.action_id)
            if quality is not None and quality.available:
                parts["primitive_evaluation"] = (
                    self.evaluation_weight * quality.effective_value
                )
            contributions[candidate.action_id] = {
                name: round(value, 6) for name, value in parts.items()
            }
            scores[candidate.action_id] = round(sum(parts.values()), 6)
        ranked = sorted(
            allowed,
            key=lambda c: (
                -scores[c.action_id],
                -c.recognition_confidence,
                c.action_id,
            ),
        )
        best = ranked[0]
        margin = (
            scores[best.action_id] - scores[ranked[1].action_id]
            if len(ranked) > 1
            else scores[best.action_id]
        )
        confidence = min(best.recognition_confidence, best.utility_confidence)
        if best.recognition_confidence < self.min_recognition:
            kind, rule_id = OBSERVE_MORE, "R1_low_recognition_confidence"
        elif len(ranked) > 1 and margin < self.min_margin:
            kind, rule_id = ABSTAIN, "R2_low_margin"
        else:
            kind, rule_id = SELECT, "R3_highest_weighted_score"
        return BaselineDecision(
            kind,
            best.action_id if kind == SELECT else None,
            scores,
            round(margin, 6),
            round(confidence, 6),
            rule_id,
            BASELINE_PROVIDER_ID,
            BASELINE_VERSION,
            contributions,
            version,
            tuple(provenance),
        )

    # {
    #   責務: [choose_context: 既存DecisionEngineのcontext API互換で、選択時だけActionDecisionを返す]
    #   処理: [判断を保留した場合はBaselineWithheldを送出し、Engineが実行へ進まないようにする]
    #   引数: [context: 画面状態・許可候補・評価値を含む判断材料, personality: 共通API互換用。参照しない]
    #   戻り値: [ActionDecision: 選択候補、採用規則とスコアを含む理由、画面参照]
    #   エラー: [BaselineWithheld: abstainまたはobserve_moreの場合, ValueError: 許可候補がない場合]
    # }
    def choose_context(
        self, context: DecisionContext, personality: str = ""
    ) -> ActionDecision:
        decision = self.decide(context)
        if decision.kind != SELECT or decision.selected_action_id is None:
            raise BaselineWithheld(decision)
        reason = (
            f"{decision.rule_id}: score={decision.score_by_action[decision.selected_action_id]:.3f}, "
            f"margin={decision.margin:.3f}"
        )
        if decision.knowledge_snapshot_version is not None:
            reason += f", knowledge={decision.knowledge_snapshot_version}"
        if context.evaluation_projection is not None and any(
            quality.available
            for quality in context.evaluation_projection.candidate_qualities(
                context.snapshot_id
            ).values()
        ):
            profile = context.evaluation_projection.to_dict()["profile"]
            reason += f", evaluation={profile['profile_id']}@{profile['version']}"
        return ActionDecision(
            str(decision.selected_action_id),
            reason,
            f"{decision.provider_id}/{decision.version}",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )

    # {
    #   責務: [_read_snapshot: 同じstepに固定されたKnowledgeから候補別priorを取り出す]
    #   処理: [投影が無い旧APIだけlegacy辞書を使う。投影が拒否された場合は旧版へ戻らず静的policyを使う]
    #   引数: [context: 今回の判断IDと任意Knowledge投影]
    #   戻り値: [tuple: (候補IDごとのprior, Snapshotの版またはNone, 入力の扱いを説明する文のlist)]
    # }
    def _read_snapshot(
        self, context: DecisionContext
    ) -> tuple[dict[str, float], str | None, list[str]]:
        snapshot = self.knowledge_snapshot
        provenance: list[str] = []
        if context.knowledge_projection is not None:
            projection = context.knowledge_projection.to_dict()
            if (
                projection.get("context_snapshot_id") != context.snapshot_id
                or projection.get("status") != "applied"
            ):
                return (
                    {},
                    None,
                    ["knowledge_projection_rejected; default static policy used"],
                )
            snapshot = {
                "version": projection.get("snapshot_version"),
                "priors": projection.get("priors"),
            }
            provenance = [
                f"knowledge_projection:{projection.get('snapshot_id')}; soft_prior"
            ]
        if snapshot is None:
            return {}, None, []
        try:
            version = snapshot["version"]
            raw = snapshot["priors"]
            if (
                not isinstance(version, str)
                or not version
                or not isinstance(raw, Mapping)
            ):
                raise ValueError("invalid snapshot shape")
            priors = {}
            for action_id, value in raw.items():
                number = float(value)
                if not isfinite(number) or not -1.0 <= number <= 1.0:
                    raise ValueError("prior out of range")
                priors[str(action_id)] = number
            return priors, version, provenance
        except (KeyError, TypeError, ValueError) as exc:
            return (
                {},
                None,
                [
                    f"knowledge_snapshot_rejected:{type(exc).__name__}; default static policy used"
                ],
            )

    # {
    #   責務: [_step_weights: この判断に固定したsoft weight hintを静的重みへ混ぜる]
    #   処理: [既存3重みだけを有限範囲で補間し、認識最低値・許可候補・Safety条件は変更しない]
    #   引数: [context: 今回の判断IDとKnowledge投影]
    #   戻り値: [dict: この呼出しだけの重み。無効な投影なら静的重みの写し]
    # }
    def _step_weights(self, context: DecisionContext) -> dict[str, float]:
        weights = dict(self.weights)
        if context.knowledge_projection is None:
            return weights
        projection = context.knowledge_projection.to_dict()
        if (
            projection.get("context_snapshot_id") != context.snapshot_id
            or projection.get("status") != "applied"
        ):
            return weights
        try:
            confidence = float(projection["confidence"])
            hints = projection["weight_hints"]
            if (
                not isfinite(confidence)
                or not 0 <= confidence <= 1
                or not isinstance(hints, dict)
            ):
                return weights
            for name in DEFAULT_WEIGHTS.keys() & hints.keys():
                hint = float(hints[name])
                if not isfinite(hint) or not 0 <= hint <= 10:
                    return dict(self.weights)
                weights[name] += confidence * (hint - weights[name])
            return weights
        except (KeyError, TypeError, ValueError):
            return dict(self.weights)


# {
#   責務: [baseline_capability_matrix: 判断能力ごとにModel不要のfallbackがあるかを機械可読に返す]
#   処理: [Baselineが対応する能力はprovider_idを、未対応の能力はNoneを持たせる]
#   引数: [profiles: 検査するBaseline Profile一覧, required: 1.0で必須とする判断能力名]
#   戻り値: [dict: 能力名からfallbackのprovider_idまたはNoneへの対応]
# }
def baseline_capability_matrix(
    profiles: list[BaselineProviderProfile],
    required: tuple[str, ...] = (
        "candidate_choice",
        "candidate_scoring",
        "observe_more_abstain",
    ),
) -> dict[str, str | None]:
    matrix: dict[str, str | None] = {}
    for capability in required:
        providers = sorted(
            (p for p in profiles if capability in p.supported_capabilities),
            key=lambda p: (p.fallback_priority, p.provider_id),
        )
        matrix[capability] = providers[0].provider_id if providers else None
    return matrix
