"""DecisionPipelineの候補統合直後（perception/output）をHook runtimeへ公開する境界。"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.hook_dispatch_result import HookDispatchResult
from ai_game_player.hook_registration import HookMode
from ai_game_player.hook_runtime import HookRuntime
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.stage_envelope import StageEnvelope, StageEnvelopeError


_LOGGER = logging.getLogger(__name__)
PERCEPTION_HOOK_STAGE = "perception"
PERCEPTION_HOOK_BOUNDARY = "output"
PERCEPTION_HOOK_PAYLOAD_SCHEMA = "kadoka.perception.candidates"
PERCEPTION_HOOK_PAYLOAD_SCHEMA_VERSION = 1
# この境界のHook出力は判断・実行へ戻さないため、加工（modify）は許可しない。
PERCEPTION_HOOK_MODES = frozenset({HookMode.READ, HookMode.INJECT})
PERCEPTION_HOOK_MAX_CANDIDATES = 64
_MAX_TEXT_LENGTH = 256


# {
#   責務: [PerceptionHookBoundary: 統合済み操作候補を座標なしのStageEnvelopeにしてread/inject Hookへ渡し、失敗時方針に従って判断を止める]
#   フィールド: [runtime: 共有するHookRuntime, session_id: Envelopeへ入れるSession ID, utc_now/monotonic_ns/new_id: 時刻とIDの供給元,
#     last_result: 直近のdispatch結果（Hookが呼ばれなかった場合None）]
#   補足: [Hookの出力は候補・判断・Safety・実行へ一切戻さない。派生Envelopeはlast_resultの診断としてだけ保持する]
# }
class PerceptionHookBoundary:
    # {
    #   責務: [__init__: perception/output境界をread/injectだけを許す接続点としてruntimeへ宣言する]
    #   処理: [接続点を宣言し、Session IDが無ければこのboundary専用のIDを作る]
    #   引数: [runtime: Hookを実行するruntime, session_id: Session Journal等のSession ID（省略時は生成）,
    #     utc_now: UTC時刻の供給元, monotonic_ns: 単調時計, new_id: Envelope/trace ID生成器]
    #   戻り値: [なし]
    #   エラー: [ValueError: 同じ境界が別のmodeで既に宣言されている場合]
    # }
    def __init__(
        self,
        runtime: HookRuntime,
        *,
        session_id: str | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        runtime.declare_point(
            PERCEPTION_HOOK_STAGE, PERCEPTION_HOOK_BOUNDARY, PERCEPTION_HOOK_MODES
        )
        self.runtime = runtime
        self.session_id = session_id or f"session-{new_id()}"
        self.utc_now = utc_now
        self.monotonic_ns = monotonic_ns
        self.new_id = new_id
        self.last_result: HookDispatchResult | None = None

    # {
    #   責務: [run: 1回の候補統合結果をHookへ公開し、HALT方針のHookが失敗したら入力なしの保留にする]
    #   処理: [
    #     1: 境界に有効なHookがなければEnvelopeを作らずNoneを返す
    #     2: 観測と候補から座標なしのEnvelopeを作る。契約外の値（StageEnvelopeError・TypeError・ValueError）で作れない場合、HALT方針のHookがあれば保留、無ければlogへ残してNoneを返す
    #     3: dispatchし、haltedなら`hook_halted`の保留（ABSTAIN）を送出する
    #   ]
    #   引数: [observation: 判断対象の画面観測, candidates: 統合済みの操作候補]
    #   戻り値: [HookDispatchResult | None: dispatch結果。Hookを呼ばなかった場合None]
    #   エラー: [DecisionWithheld: HALT方針のHookが失敗した場合、またはそのHookへ渡すEnvelopeを作れなかった場合]
    # }
    def run(
        self,
        observation: ScreenObservation,
        candidates: Sequence[ActionCandidate],
    ) -> HookDispatchResult | None:
        self.last_result = None
        if not self.runtime.has_hooks(PERCEPTION_HOOK_STAGE, PERCEPTION_HOOK_BOUNDARY):
            return None
        try:
            envelope = self._build_envelope(observation, candidates)
        except (StageEnvelopeError, TypeError, ValueError) as error:
            # 候補の値が契約外でも、HALT方針のHookがなければCoreの判断は止めない。
            if self.runtime.has_halting_hooks(
                PERCEPTION_HOOK_STAGE, PERCEPTION_HOOK_BOUNDARY
            ):
                raise _withheld("hook_envelope_unavailable") from None
            _LOGGER.warning(
                "perception hook envelope skipped: error_type=%s code=%s",
                type(error).__name__,
                getattr(error, "code", None),
            )
            return None
        result = self.runtime.dispatch(envelope)
        self.last_result = result
        if result.halted:
            raise _withheld("hook_halted")
        return result

    # {
    #   責務: [_build_envelope: 画面と候補から、座標・矩形・保持時間を含まないperception/output Envelopeを作る]
    #   処理: [文字列を上限長へ切り詰め、候補を先頭から上限件数まで入れ、producer_kind=coreのEnvelopeを作る]
    #   引数: [observation: 画面観測, candidates: 統合済み候補]
    #   戻り値: [StageEnvelope: 検証済みEnvelope]
    #   エラー: [StageEnvelopeError: 文字列や値がv1契約に違反する場合, TypeError: 候補の文字列fieldが文字列でない場合]
    # }
    def _build_envelope(
        self,
        observation: ScreenObservation,
        candidates: Sequence[ActionCandidate],
    ) -> StageEnvelope:
        payload: dict[str, Any] = {
            "screen_id": str(observation.screen_id)[:_MAX_TEXT_LENGTH],
            "width": observation.width,
            "height": observation.height,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "action_id": candidate.action_id[:_MAX_TEXT_LENGTH],
                    "kind": candidate.kind[:_MAX_TEXT_LENGTH],
                    "label": candidate.label[:_MAX_TEXT_LENGTH],
                    "confidence": candidate.confidence,
                    "dangerous": candidate.dangerous,
                }
                for candidate in candidates[:PERCEPTION_HOOK_MAX_CANDIDATES]
            ],
        }
        timestamp = self.utc_now().astimezone(timezone.utc)
        return StageEnvelope(
            envelope_id=f"core-{self.new_id()}",
            stage=PERCEPTION_HOOK_STAGE,
            boundary=PERCEPTION_HOOK_BOUNDARY,
            session_id=self.session_id,
            timestamp_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            monotonic_ns=self.monotonic_ns(),
            trace_id=f"trace-{self.new_id()}",
            producer_kind="core",
            producer_id="core.decision_pipeline",
            producer_version="1",
            payload_schema=PERCEPTION_HOOK_PAYLOAD_SCHEMA,
            payload_schema_version=PERCEPTION_HOOK_PAYLOAD_SCHEMA_VERSION,
            payload=payload,
        )


# {
#   責務: [_withheld: Hook境界の失敗を、操作を含まない保留結果の例外にする]
#   処理: [ABSTAINと理由code、provider_id=hook_runtimeでDecisionDispositionを作る]
#   引数: [reason: 保留の理由code]
#   戻り値: [DecisionWithheld: 呼び出し側が送出する例外]
# }
def _withheld(reason: str) -> DecisionWithheld:
    return DecisionWithheld(
        DecisionDisposition(PlanningOutcome.ABSTAIN, (reason,), "hook_runtime")
    )
