"""有界な操作効果統計とsoft hintをSnapshot候補のコピーへ反映する。"""

from math import fsum, sqrt

from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_update_sample import KnowledgeUpdateSample


EMPTY_ACTION_STATS = {
    "count": 0,
    "success": 0,
    "no_effect": 0,
    "failure": 0,
    "unknown": 0,
    "reward_mean": 0.0,
    "reward_ewma": 0.0,
    "alpha": 1.0,
    "beta": 1.0,
}
WEIGHT_DEFAULTS = {
    "utility": 1.0,
    "recognition": 0.3,
    "knowledge_prior": 0.5,
    "progress": 1.0,
    "survival": 1.0,
    "resources": 1.0,
    "risk": 1.0,
    "repetition": 1.0,
}
THRESHOLD_DEFAULTS = {"recognition_minimum": 0.6, "decision_margin": 0.05}


# {
#   責務: [update_action_statistics: 一つの独立した経験から分類件数・報酬・Beta推定・soft priorを更新する]
#   処理: [未知結果はunknown件数だけへ加算する。既知効果は少数証拠の縮小推定と変化上限付きpriorへ変換する]
#   引数: [data: 新Snapshot用の独立コピー, sample: 型付き証拠, outcome: 信頼性検査済み分類, policy: 量と変化幅の制限]
#   戻り値: [なし。dataの数値統計だけを更新する]
#   エラー: [ValueError: action項目上限を超える]
# }
def update_action_statistics(
    data: dict,
    sample: KnowledgeUpdateSample,
    outcome: str,
    policy: KnowledgeBuilderPolicy,
) -> None:
    actions = data["action_outcome_stats"]
    if sample.action_id not in actions and len(actions) >= policy.max_actions:
        raise ValueError("knowledge action capacity reached")
    stats = actions.setdefault(sample.action_id, dict(EMPTY_ACTION_STATS))
    known_before = stats["success"] + stats["no_effect"] + stats["failure"]
    stats["count"] += 1
    stats[outcome] += 1
    if outcome == "unknown":
        return
    reward = {"success": 1.0, "failure": -1.0, "no_effect": policy.no_effect_reward}[
        outcome
    ]
    stats["reward_mean"] += (reward - stats["reward_mean"]) / (known_before + 1)
    stats["reward_ewma"] += (
        policy.ewma_rate * sample.confidence * (reward - stats["reward_ewma"])
    )
    stats["alpha" if outcome == "success" else "beta"] += sample.confidence
    known = known_before + 1
    posterior_difference = (stats["alpha"] - stats["beta"]) / (
        stats["alpha"] + stats["beta"]
    )
    shrunk_prior = posterior_difference * known / (known + policy.prior_evidence_scale)
    old_prior = data["candidate_priors"].get(sample.action_id, 0.0)
    data["candidate_priors"][sample.action_id] = _bounded_change(
        old_prior, shrunk_prior, policy.max_prior_delta
    )
    if sample.candidate_reliability is not None:
        old_reliability = data["candidate_reliability"].get(sample.action_id, 0.5)
        data["candidate_reliability"][sample.action_id] = (
            old_reliability
            + policy.ewma_rate
            * sample.confidence
            * (sample.candidate_reliability - old_reliability)
        )
    _update_transition(data, sample, policy)
    _update_hints(data, sample, policy)


# {
#   責務: [estimate_uncertainty: 未知結果比率とBeta事後分布の分散から残る不確かさを返す]
#   処理: [action件数で分散を重み付けし、未知比率より小さい確信を作らない]
#   引数: [data: 更新後の数値統計]
#   戻り値: [float: 0〜1の不確かさ。証拠なしは1]
# }
def estimate_uncertainty(data: dict) -> float:
    count = sum(stats["count"] for stats in data["action_outcome_stats"].values())
    if count == 0:
        return 1.0
    variance_terms = []
    unknown = 0
    for _, stats in sorted(data["action_outcome_stats"].items()):
        alpha, beta = stats["alpha"], stats["beta"]
        variance = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1))
        variance_terms.append(stats["count"] * min(1.0, sqrt(12 * variance)) / count)
        unknown += stats["unknown"]
    return min(1.0, max(unknown / count, fsum(variance_terms)))


# {
#   責務: [_update_transition: 確認済みの操作前後状態の頻度を条件付き遷移確率へ変換する]
#   処理: [同じ前状態と操作ごとの件数を正規化し、未確認の画面変化は呼出側で除外する]
#   引数: [data: Snapshot候補, sample: 既知効果を持つ証拠, policy: 遷移項目上限]
#   戻り値: [なし]
#   エラー: [ValueError: 新しい遷移が保持上限を超える]
# }
def _update_transition(
    data: dict, sample: KnowledgeUpdateSample, policy: KnowledgeBuilderPolicy
) -> None:
    if sample.transition is None:
        return
    before, after = sample.transition
    records = data["transition_priors"]
    current = next(
        (
            item
            for item in records
            if (item["from_state"], item["action_id"], item["to_state"])
            == (before, sample.action_id, after)
        ),
        None,
    )
    if current is None:
        if len(records) >= policy.max_transitions:
            raise ValueError("knowledge transition capacity reached")
        current = {
            "from_state": before,
            "action_id": sample.action_id,
            "to_state": after,
            "probability": 0.0,
            "count": 0,
        }
        records.append(current)
    current["count"] += 1
    related = [
        item
        for item in records
        if item["from_state"] == before and item["action_id"] == sample.action_id
    ]
    total = sum(item["count"] for item in related)
    for item in related:
        item["probability"] = item["count"] / total
    records.sort(
        key=lambda item: (item["from_state"], item["action_id"], item["to_state"])
    )


# {
#   責務: [_update_hints: 確認済み数値hintを一件で急変しない候補値として集計する]
#   処理: [重みと閾値は既定値から変化幅を制限し、ROIは正規化矩形の凸結合で更新する]
#   引数: [data: Snapshot候補, sample: 既知効果を持つ証拠, policy: 更新割合と変化上限]
#   戻り値: [なし。Safety設定や実行候補は変更しない]
#   エラー: [ValueError: ROI項目上限を超える]
# }
def _update_hints(
    data: dict, sample: KnowledgeUpdateSample, policy: KnowledgeBuilderPolicy
) -> None:
    rate = policy.ewma_rate * sample.confidence
    for name, hints, defaults in (
        ("weight_hints", sample.weight_hints, WEIGHT_DEFAULTS),
        ("threshold_hints", sample.threshold_hints, THRESHOLD_DEFAULTS),
    ):
        for key, target in hints:
            old = data[name].get(key, defaults[key])
            data[name][key] = _bounded_change(
                old, old + rate * (target - old), policy.max_prior_delta
            )
    for key, target in sample.roi_hints:
        if (
            key not in data["roi_hints"]
            and len(data["roi_hints"]) >= policy.max_actions
        ):
            raise ValueError("knowledge ROI capacity reached")
        old = data["roi_hints"].get(key, (0.0, 0.0, 1.0, 1.0))
        data["roi_hints"][key] = [
            left + rate * (right - left) for left, right in zip(old, target)
        ]


# {
#   責務: [_bounded_change: 一証拠に許可された数値変化幅を適用する]
#   処理: [現在値から目標までの差を正負の上限内に切り詰める]
#   引数: [current: 更新前値, target: 推定目標値, limit: 一回の最大変化幅]
#   戻り値: [float: boundedな更新後値]
# }
def _bounded_change(current: float, target: float, limit: float) -> float:
    return current + max(-limit, min(limit, target - current))
