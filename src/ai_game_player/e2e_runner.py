import json
import time
from pathlib import Path
from typing import Callable

from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation
from ai_game_player.no_allowed_candidates_error import NoAllowedCandidatesError
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.run_control import ExecutionCancelled


def append_e2e_event(path: Path, event: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


# {
#   責務: [run_continuous_e2e: 操作・観測・完了確認を繰り返し、停止理由と実入力の有無をJSONLへ記録する]
#   処理: [
#     1: 各操作後の画面変化と到達条件を記録し、停止・時間・回数・同一画面の上限で終了する
#     2: 判断呼出しで許可候補が空になった場合だけ、既存の実入力と実行世代の継続を条件に完了を再確認する
#     3: 再確認が厳密なTrueなら追加入力せず成功を記録する。別種の例外や確認失敗は失敗として残す
#     4: 実行後probeを同じ実行世代の明示観測として完了し、最後の操作結果も保存する
#   ]
#   引数: [
#     pipeline: 判断と入力を実行する経路。controllerの実行状態と直近の判断履歴も参照する
#     observation_probe: 操作後の画面を読み、画面照合値を含む観測を返す関数
#     log_path: 操作・エラー・終了結果を追記するJSONLファイル
#     milestone_probe: ゲームの到達条件を確認する関数。省略時は到達による終了を行わない
#     loop_guard: 同一画面の反復を検出する監視器。省略時は4回で停止する監視器を作る
#     max_steps: 判断・操作を試みる最大回数。1以上を指定する
#     max_wall_seconds: 実行開始からの制限秒数。正の値を指定する
#     minimum_duration_seconds: 指定秒数の継続を成功条件とする値。0なら継続時間による成功判定を行わない
#     settle_seconds: 入力から画面観測まで待つ秒数。0以上を指定する
#     step_delay_seconds: 次の判断まで待つ秒数。0以上を指定する
#     purpose: 判断経路へ渡す操作目的の説明文
#   ]
#   戻り値: [dict: success、完了したsteps、elapsed_seconds、stop_reason、live_input_verifiedを含む終了記録]
#   エラー: [ValueError: 回数・時間の設定値が範囲外の場合。実行中の例外は失敗記録へ変換するが、ログ書込失敗は呼出元へ伝わる]
# }
def run_continuous_e2e(
    pipeline: DecisionPipeline,
    observation_probe: Callable[[], ScreenObservation],
    log_path: Path,
    *,
    milestone_probe: Callable[[], bool] | None = None,
    loop_guard: LoopGuard | None = None,
    max_steps: int = 1000,
    max_wall_seconds: float = 900.0,
    minimum_duration_seconds: float = 0.0,
    settle_seconds: float = 0.05,
    step_delay_seconds: float = 0.0,
    purpose: str = "continue the sample game",
) -> dict[str, object]:
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    if max_wall_seconds <= 0:
        raise ValueError("max_wall_seconds must be positive")
    if minimum_duration_seconds < 0:
        raise ValueError("minimum_duration_seconds must not be negative")
    if settle_seconds < 0 or step_delay_seconds < 0:
        raise ValueError("delays must not be negative")

    active_guard = loop_guard or LoopGuard(limit=4)
    active_guard.reset()
    started = time.monotonic()
    live_verified = False
    completed_steps = 0

    for step_index in range(1, max_steps + 1):
        elapsed = time.monotonic() - started
        if elapsed >= max_wall_seconds:
            return _finish(
                log_path,
                False,
                completed_steps,
                started,
                "wall_time_limit",
                live_verified,
            )
        if minimum_duration_seconds and elapsed >= minimum_duration_seconds:
            return _finish(
                log_path,
                True,
                completed_steps,
                started,
                "duration_reached",
                live_verified,
            )
        if not pipeline.controller.is_running:
            return _finish(
                log_path,
                False,
                completed_steps,
                started,
                "controller_stopped",
                live_verified,
            )

        step_started = time.monotonic()
        expected_rearm_token = pipeline.controller.rearm_token
        try:
            try:
                result = pipeline.run_and_execute(purpose=purpose)
            except NoAllowedCandidatesError as exc:
                try:
                    milestone_after_empty = _milestone_after_empty_candidates(
                        exc,
                        pipeline,
                        milestone_probe,
                        completed_steps,
                        live_verified,
                        expected_rearm_token,
                    )
                except Exception as probe_error:
                    append_e2e_event(
                        log_path,
                        {
                            "event": "error",
                            "step": step_index,
                            "error": str(probe_error),
                            "preceding_error": str(exc),
                            "category": "milestone_probe_failure",
                        },
                    )
                    return _finish(
                        log_path,
                        False,
                        completed_steps,
                        started,
                        "milestone_probe_failure",
                        live_verified,
                    )
                if milestone_after_empty:
                    append_e2e_event(
                        log_path,
                        {
                            "event": "milestone",
                            "step": step_index,
                            "completed_steps": completed_steps,
                            "trigger": "no_allowed_candidates",
                        },
                    )
                    return _finish(
                        log_path,
                        True,
                        completed_steps,
                        started,
                        "milestone_reached",
                        live_verified,
                    )
                raise
            live_verified = live_verified or bool(
                result.executed and result.mode == "live"
            )
            if settle_seconds:
                time.sleep(settle_seconds)
            after = observation_probe()
            complete = getattr(pipeline, "complete_execution_observation", None)
            if callable(complete):
                complete(after, expected_rearm_token=expected_rearm_token)
            trace_entries = pipeline.engine.trace.recent(1)
            if not trace_entries:
                raise RuntimeError("decision trace missing after execution")
            trace = trace_entries[-1]
            context = trace.get("context", {})
            if not isinstance(context, dict):
                raise RuntimeError("decision context missing from trace")
            decision = trace.get("decision", {})
            if not isinstance(decision, dict):
                decision = {}
            state = context.get("state", {})
            if not isinstance(state, dict):
                state = {}
            candidates = context.get("candidates", [])
            candidate_ids = (
                [
                    str(candidate.get("action_id", ""))
                    for candidate in candidates
                    if isinstance(candidate, dict) and candidate.get("action_id")
                ]
                if isinstance(candidates, list)
                else []
            )
            before_signature = str(state.get("signature", ""))
            after_signature = str(after.features.get("signature", ""))
            state_changed = bool(
                before_signature
                and after_signature
                and before_signature != after_signature
            )
            milestone = bool(milestone_probe and milestone_probe())
            outcome_status = (
                "success" if milestone else "ongoing" if state_changed else "unknown"
            )
            outcome_confidence = 1.0 if milestone else 0.9 if state_changed else 0.25
            pipeline.record_safety_outcome(
                outcome_status,
                outcome_confidence,
                "milestone reached"
                if milestone
                else "screen signature changed"
                if state_changed
                else "no screen change detected",
            )
            append_e2e_event(
                log_path,
                {
                    "event": "step",
                    "step": step_index,
                    "observation_id": str(state.get("screen_id", "")),
                    "frame_signature": before_signature,
                    "recognized_elements": int(
                        state.get("detected_element_count", 0) or 0
                    ),
                    "candidate_actions": candidate_ids,
                    "selected_action": str(decision.get("action_id", result.action_id)),
                    "provider": str(decision.get("provider", "")),
                    "reason": str(decision.get("reason", "")),
                    "execution": {
                        "action_id": result.action_id,
                        "executed": result.executed,
                        "mode": result.mode,
                        "detail": result.detail,
                    },
                    "outcome": {
                        "status": outcome_status,
                        "confidence": outcome_confidence,
                        "state_changed": state_changed,
                        "after_signature": after_signature,
                    },
                    "latency_ms": round((time.monotonic() - step_started) * 1000.0, 3),
                },
            )
            completed_steps = step_index

            if milestone:
                return _finish(
                    log_path,
                    True,
                    completed_steps,
                    started,
                    "milestone_reached",
                    live_verified,
                )
            if active_guard.observe(after):
                return _finish(
                    log_path,
                    False,
                    completed_steps,
                    started,
                    "repeated_observation",
                    live_verified,
                )
            if step_delay_seconds:
                time.sleep(step_delay_seconds)
        except Exception as exc:
            category = _classify_error(exc)
            append_e2e_event(
                log_path,
                {
                    "event": "error",
                    "step": step_index,
                    "error": str(exc),
                    "category": category,
                },
            )
            return _finish(
                log_path, False, completed_steps, started, category, live_verified
            )

    return _finish(
        log_path, False, completed_steps, started, "step_limit", live_verified
    )


# {
#   責務: [_milestone_after_empty_candidates: 入力前の候補なし例外に限り、ゲームが完了したかを再確認する]
#   処理: [1: 既存の実入力・完了済み操作・実行世代を確認する, 2: 到達条件の厳密なTrueと確認後の同一実行世代を要求する]
#   引数: [
#     error: 今回の判断・入力経路で発生した例外。候補なし以外なら再確認しない
#     pipeline: 停止要求を確認する操作経路。入力操作や状態変更は行わない
#     milestone_probe: ゲームの到達条件を返す関数。未指定なら再確認しない
#     completed_steps: 例外より前に観測と記録まで終えた操作数
#     live_verified: 先行する操作で実入力の実行結果を確認済みか
#     expected_rearm_token: 今回の判断開始前に取得した実行世代。確認中の停止と再開を検出する
#   ]
#   戻り値: [bool: 候補なしの後に到達条件が成立し、停止要求もない場合だけTrue]
#   エラー: [ExecutionCancelled: 停止・旧世代はFalseへ変換する, その他のException: 到達条件の読取失敗は呼出元へ伝え、成功扱いを防ぐ]
# }
def _milestone_after_empty_candidates(
    error: Exception,
    pipeline: DecisionPipeline,
    milestone_probe: Callable[[], bool] | None,
    completed_steps: int,
    live_verified: bool,
    expected_rearm_token: int,
) -> bool:
    if (
        not isinstance(error, NoAllowedCandidatesError)
        or completed_steps == 0
        or not live_verified
        or not pipeline.controller.is_running
        or milestone_probe is None
    ):
        return False
    try:
        pipeline.controller.ensure_running(expected_rearm_token)
        if milestone_probe() is not True:
            return False
        pipeline.controller.ensure_running(expected_rearm_token)
    except ExecutionCancelled:
        return False
    return True


def _finish(
    path: Path,
    success: bool,
    steps: int,
    started: float,
    stop_reason: str,
    live_verified: bool,
) -> dict[str, object]:
    report = {
        "event": "summary",
        "success": success,
        "steps": steps,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stop_reason": stop_reason,
        "live_input_verified": live_verified,
    }
    append_e2e_event(path, report)
    return report


def _classify_error(exc: Exception) -> str:
    text = str(exc).casefold()
    if "safety" in text or "verification" in text or "blocked" in text:
        return "safety_block"
    if "candidate" in text or "候補" in text:
        return "recognition_or_candidate_failure"
    if "window" in text or "capture" in text or "getwindowrect" in text:
        return "target_or_capture_failure"
    if "停止" in text or "stopped" in text:
        return "controller_stopped"
    return "execution_failure"
