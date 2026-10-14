from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from ai_game_player.campaign_contract import CAMPAIGN_ERROR_DETAIL_LIMIT, CampaignState


def append_campaign_event(path: Path, event: dict[str, object]) -> None:
    # {
    #   責務: [append_campaign_event: campaign eventを1行のUTF-8 JSONLとして追記する]
    #   処理: [保存先directoryを作り、UTC時刻を加えたevent objectをsort済みJSONで追記してflushする]
    #   引数: [path: campaign event logの保存先, event: raw frameや画像を含めない状態・操作・metric記録]
    #   戻り値: []
    #   エラー: [OSError: log directoryまたはevent fileへ書き込めない場合]
    # }
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()


def episode_event(event_name: str, episode_number: int, reason: str, state: CampaignState) -> dict[str, object]:
    # {
    #   責務: [episode_event: episode開始・再起動eventにstate signatureとmetricを付ける]
    #   引数: [event_name: JSONLへ記録するevent名, episode_number: campaign内のepisode番号, reason: episodeを開始した理由, state: 新しいwindowで認識した初期状態]
    #   戻り値: [dict[str, object]: 画面画像を含まないepisode lifecycle event]
    # }
    return {
        "event": event_name,
        "episode": episode_number,
        "reason": reason,
        "state_signature": state.signature,
        "terminal": state.terminal,
        "score": state.score,
        "max_tile": state.max_tile,
    }


def update_metrics(
    state: CampaignState,
    last_score: int | None,
    peak_score: int | None,
    max_tile: int,
) -> tuple[int | None, int | None, int]:
    # {
    #   責務: [update_metrics: 新しいscreen stateのscoreと最大tileをcampaign全体のmetricへ統合する]
    #   引数: [state: 今回captureしたゲーム状態, last_score: 直近に認識したscore, peak_score: campaign中の最大score, max_tile: campaign中の最大tile]
    #   戻り値: [tuple[int | None, int | None, int]: 更新した現在score、peak score、最大tile]
    # }
    if state.score is not None:
        last_score = state.score
        peak_score = state.score if peak_score is None else max(peak_score, state.score)
    if state.max_tile is not None:
        max_tile = max(max_tile, state.max_tile)
    return last_score, peak_score, max_tile


def latency_summary(values_ms: list[float]) -> dict[str, float | int | None]:
    # {
    #   責務: [latency_summary: actionから次screen observationまでのlatency集計値を返す]
    #   引数: [values_ms: step単位で測定した有限のmillisecond値]
    #   戻り値: [dict[str, float | int | None]: sample数、平均、p50、p95、最大latency。sampleがなければ値はNone]
    # }
    if not values_ms:
        return {"samples": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values_ms)

    # {
    #   責務: [percentile: 順序付けたlatency標本からnearest-rank percentileを返す]
    #   引数: [fraction: 0から1の範囲で指定する割合]
    #   戻り値: [float: 指定割合に対応するmillisecond latencyを小数第3位で丸めた値]
    # }
    def percentile(fraction: float) -> float:
        rank = max(1, math.ceil(fraction * len(ordered)))
        return round(ordered[rank - 1], 3)

    return {
        "samples": len(ordered),
        "mean": round(sum(ordered) / len(ordered), 3),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "max": round(ordered[-1], 3),
    }


def classify_campaign_error(error: Exception) -> str:
    # {
    #   責務: [classify_campaign_error: exceptionの種類と具体語からcampaign hard failureを分類する]
    #   引数: [error: episode factory・capture・recognition・Safety・execution処理が送出した例外]
    #   戻り値: [str: campaign reportへ保存する既知failure category]
    # }
    from ai_game_player.campaign_contract import CampaignFailure

    if isinstance(error, CampaignFailure):
        return error.category
    if isinstance(error, OSError):
        return "artifact_write_failure"
    description = f"{type(error).__name__} {error}".casefold()
    if "safety" in description or "blocked" in description:
        return "safety_block"
    if "candidate" in description or "recognition" in description or "board" in description:
        return "recognition_failure"
    if "window" in description or "capture" in description or "hwnd" in description:
        return "capture_failure"
    if "input" in description or "execution" in description:
        return "execution_failure"
    if "decision" in description or "provider" in description or "reliability" in description or "verification" in description:
        return "decision_failure"
    return "unexpected_failure"


def bounded_error_detail(detail: str) -> str:
    # {
    #   責務: [bounded_error_detail: error reportへ保存する説明を上限文字数で切り詰める]
    #   引数: [detail: exceptionまたはcleanupの原因を示す文字列]
    #   戻り値: [str: 最大CAMPAIGN_ERROR_DETAIL_LIMIT文字のerror説明]
    # }
    return detail[:CAMPAIGN_ERROR_DETAIL_LIMIT]
