"""Scopeを照合したKnowledgeを観測と区別したsoft evidenceへ投影する。"""

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import Any, Callable

from ai_game_player.decision_context import DecisionContext
from ai_game_player.knowledge_projection import PROJECTION_VERSION, KnowledgeProjection
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.models import ScreenObservation


MAX_HINT_ENTRIES = 64
MAX_SOURCE_RANGES = 8
SUPPORT_PRIOR = 4.0


# {
#   責務: [KnowledgeFeatureAdapter: 選択済みKnowledgeの版・scope・期限を照合して一回の判断へ固定する]
#   フィールド: [_snapshot_source: 呼出側が選択したimmutable Snapshotを取得する関数,
#     _scope_source: 信頼されたSession設定から現在のgame/scene/profile/player/lineageを得る関数,
#     _clock: UTCの評価時刻を返す時計, _max_age_seconds: 任意の最大経過秒,
#     _expose_to_models: モデルcontextへ投影を含める明示許可]
# }
class KnowledgeFeatureAdapter:
    # {
    #   責務: [__init__: Knowledgeと現在scopeの取得元、期限・公開設定を固定する]
    #   処理: [最大経過時間と公開設定を検証する。Snapshotの昇格・学習や外部送信は開始しない]
    #   引数: [snapshot_source: 現在scopeに対して選択済みの版を返す。欠落時はNone,
    #     scope_source: Observationを手掛かりに信頼されたSession側で決めたscope。OCR文字列から生成しない,
    #     clock: aware datetimeを返す時計。省略時は現在UTC,
    #     max_age_seconds: 作成からの経過上限。NoneならSnapshot自身のexpires_atだけを使う,
    #     expose_to_models: 既定False。モデル要求にKnowledge投影を含める明示許可]
    #   戻り値: [なし]
    #   エラー: [ValueError: 最大経過秒が非有限・非正、または公開設定がbool以外]
    # }
    def __init__(
        self,
        snapshot_source: Callable[[KnowledgeScope], KnowledgeSnapshot | None],
        scope_source: Callable[[ScreenObservation], KnowledgeScope | None],
        *,
        clock: Callable[[], datetime] | None = None,
        max_age_seconds: float | None = None,
        expose_to_models: bool = False,
    ) -> None:
        if max_age_seconds is not None and (
            isinstance(max_age_seconds, bool)
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("maximum knowledge age must be finite and positive")
        if type(expose_to_models) is not bool:
            raise ValueError("knowledge model exposure must be a boolean")
        self._snapshot_source = snapshot_source
        self._scope_source = scope_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_age_seconds = max_age_seconds
        self._expose_to_models = expose_to_models

    # {
    #   責務: [apply: 許可候補や観測を変えず、この判断に対応するKnowledge投影だけを追加する]
    #   処理: [scopeとSnapshotを一度ずつ取得し、失敗時は空の投影を記録して静的判断へ戻す]
    #   引数: [context: 既存の候補評価で生成した判断材料, observation: 同じ判断時点の観測]
    #   戻り値: [DecisionContext: 元の候補・許可状態・観測・前回結果を保持した新しい値]
    # }
    def apply(
        self, context: DecisionContext, observation: ScreenObservation
    ) -> DecisionContext:
        projection = self.project(context, observation)
        return replace(context, knowledge_projection=projection)

    # {
    #   責務: [project: 現在のscopeと時刻で使えるSnapshotからboundedなsoft hintを作る]
    #   処理: [期限・作成時刻・scope・型を照合し、sample数と不確実性でpriorを縮小する。
    #     不明な結果は行動の成功やno-effectに変換しない。入力元の障害はreason codeで記録する]
    #   引数: [context: 投影を結び付ける判断snapshotと許可候補, observation: 現在scopeを取得する同時点の観測]
    #   戻り値: [KnowledgeProjection: このstepだけの版・出所・soft hint、または静的fallbackの理由]
    # }
    def project(
        self, context: DecisionContext, observation: ScreenObservation
    ) -> KnowledgeProjection:
        payload = _empty_projection(context.snapshot_id)
        try:
            scope = self._scope_source(observation)
            if not isinstance(scope, KnowledgeScope):
                return self._fallback(payload, "scope_unavailable")
            snapshot = self._snapshot_source(scope)
            if snapshot is None:
                return self._fallback(payload, "missing")
            if not isinstance(snapshot, KnowledgeSnapshot):
                return self._fallback(payload, "invalid_snapshot")
            if snapshot.scope != scope:
                return self._fallback(payload, "scope_mismatch")
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() is None:
                return self._fallback(payload, "invalid_clock")
            created = datetime.fromisoformat(snapshot.created_at.replace("Z", "+00:00"))
            age = (now - created).total_seconds()
            if age < 0:
                return self._fallback(payload, "future_snapshot")
            if snapshot.is_expired(now):
                return self._fallback(payload, "expired")
            if self._max_age_seconds is not None and age > self._max_age_seconds:
                return self._fallback(payload, "stale")
            values = snapshot.to_dict()
            return self._make_projection(context, values, snapshot)
        except Exception:
            # Callback failures must not expose paths, raw observations or exception text.
            return self._fallback(payload, "source_unavailable")

    # {
    #   責務: [_make_projection: 検証済みの一つの版から候補priorと参考hintを固定する]
    #   処理: [未知結果を除いたsample支持と不確実性・候補信頼性でpriorを縮小し、件数を制限する]
    #   引数: [context: 許可候補と判断ID, values: 検証済みSnapshotの独立した写し, snapshot: 同じ版の不変値]
    #   戻り値: [KnowledgeProjection: boundedなsoft priorと出所。Safety条件は含めない]
    # }
    def _make_projection(
        self,
        context: DecisionContext,
        values: dict[str, Any],
        snapshot: KnowledgeSnapshot,
    ) -> KnowledgeProjection:
        allowed = set(context.allowed_action_ids) & {
            c.action_id for c in context.candidates if c.allowed
        }
        stats = values["action_outcome_stats"]
        support = (
            sum(
                item["success"] + item["no_effect"] + item["failure"]
                for item in stats.values()
            )
            if stats
            else values["sample_count"]
        )
        confidence = (1.0 - values["uncertainty"]) * support / (support + SUPPORT_PRIOR)
        priors = {}
        applicable_priors = sorted(allowed & values["candidate_priors"].keys())
        for action_id in applicable_priors[:MAX_HINT_ENTRIES]:
            action_stats = stats.get(action_id)
            count = (
                (
                    action_stats["success"]
                    + action_stats["no_effect"]
                    + action_stats["failure"]
                )
                if action_stats
                else values["sample_count"]
            )
            trust = (1.0 - values["uncertainty"]) * count / (count + SUPPORT_PRIOR)
            trust *= values["candidate_reliability"].get(action_id, 1.0)
            priors[action_id] = values["candidate_priors"][action_id] * trust
        transitions = [
            item for item in values["transition_priors"] if item["action_id"] in allowed
        ]
        payload = _empty_projection(context.snapshot_id)
        payload.update(
            {
                "status": "applied",
                "reason": "scope_time_verified_soft_evidence",
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_version": snapshot.version,
                "snapshot_sha256": sha256(snapshot.to_bytes()).hexdigest(),
                "scope": values["scope"],
                "sample_count": snapshot.sample_count,
                "confidence": confidence,
                "priors": priors,
                "weight_hints": values["weight_hints"],
                "threshold_hints": values["threshold_hints"],
                "roi_hints": dict(
                    sorted(values["roi_hints"].items())[:MAX_HINT_ENTRIES]
                ),
                "transition_priors": transitions[:MAX_HINT_ENTRIES],
                "truncated": {
                    "candidate_priors": max(
                        0, len(applicable_priors) - MAX_HINT_ENTRIES
                    ),
                    "roi_hints": max(0, len(values["roi_hints"]) - MAX_HINT_ENTRIES),
                    "transition_priors": max(0, len(transitions) - MAX_HINT_ENTRIES),
                    "source_experience_range": max(
                        0, len(values["source_experience_range"]) - MAX_SOURCE_RANGES
                    ),
                },
                "provenance": {
                    "update_rule": values["update_rule"],
                    "update_rule_version": values["update_rule_version"],
                    "source_experience_range": values["source_experience_range"][
                        -MAX_SOURCE_RANGES:
                    ],
                },
            }
        )
        return KnowledgeProjection.from_dict(
            payload, expose_to_models=self._expose_to_models
        )

    # {
    #   責務: [_fallback: 不採用理由だけを残し、版やpriorを採用しない投影を作る]
    #   処理: [入力元の内容を含めず、空のhintを静的判断へ返す]
    #   引数: [payload: この判断に結び付く空投影, reason: 固定の失敗理由code]
    #   戻り値: [KnowledgeProjection: 候補を変更しないfallback]
    # }
    def _fallback(self, payload: dict[str, Any], reason: str) -> KnowledgeProjection:
        return KnowledgeProjection.from_dict(
            {**payload, "reason": reason}, expose_to_models=self._expose_to_models
        )


# {
#   責務: [_empty_projection: この判断だけに対応する静的fallbackの初期値を作る]
#   処理: [Knowledgeの版・prior・hintが無いことを明示し、観測と出所を混ぜない]
#   引数: [context_snapshot_id: 同時点のDecisionContextの参照]
#   戻り値: [dict: 有効な空投影]
# }
def _empty_projection(context_snapshot_id: str) -> dict[str, Any]:
    return {
        "schema_version": PROJECTION_VERSION,
        "context_snapshot_id": context_snapshot_id,
        "status": "fallback",
        "reason": "missing",
        "snapshot_id": None,
        "snapshot_version": None,
        "snapshot_sha256": None,
        "scope": None,
        "sample_count": 0,
        "confidence": 0.0,
        "priors": {},
        "weight_hints": {},
        "threshold_hints": {},
        "roi_hints": {},
        "transition_priors": [],
        "provenance": {},
        "truncated": {
            "candidate_priors": 0,
            "roi_hints": 0,
            "transition_priors": 0,
            "source_experience_range": 0,
        },
    }
