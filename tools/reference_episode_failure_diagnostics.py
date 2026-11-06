"""明示検証の失敗時だけ、close前の固定診断を保存する。原因修復や観測には使わない。"""

import json
import os
from pathlib import Path
import re
from typing import Any, cast

from ai_game_player.atomic_json import stage_json_write
from tools.reference_startup_correlation import SCHEMA, classify_startup_failure

MAX_DIAGNOSTIC_BYTES = 64 * 1024
CI_CONTEXT_VARIABLES = (
    ("event", "GITHUB_EVENT_NAME"),
    ("run_id", "GITHUB_RUN_ID"),
    ("run_attempt", "GITHUB_RUN_ATTEMPT"),
    ("sha", "GITHUB_SHA"),
)


# {
#   責務: [ci_context: CI実行の識別子だけを環境変数から取り出す],
#   処理: [固定4変数を64字までの文字列で返す。無い変数はNone。秘密や利用者情報は読まない],
#   引数: []
#   戻り値: [dict[str, str | None]: event/run_id/run_attempt/sha]
# }
def ci_context() -> dict[str, str | None]:
    context: dict[str, str | None] = {}
    for key, variable in CI_CONTEXT_VARIABLES:
        value = os.environ.get(variable)
        # 識別子として妥当な文字だけを受け入れ、それ以外は記録しない。
        context[key] = (
            value if value and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,64}", value) else None
        )
    return context


def _bounded_section(value: object, maximum_bytes: int) -> object:
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode("utf-8")) <= maximum_bytes:
            return cast(object, json.loads(encoded))
    except Exception:
        pass
    return {"status": "unavailable", "reason": "invalid_or_oversized_metadata"}


# {
#   責務: [_frame_section: 保全済みFrameの時刻と既存認識器の結果を相関記録用に整える],
#   処理: [Sourceが失敗時に残した最小probe(撮影時刻とgame_2048 state)だけを読み、閾値や認識は変えない。probeが無ければ型を欠落させ分類側でfail-closedにする],
#   引数: [episode: 失敗したepisode, preserved_ms: Frame保全の確認時刻(ms)]
#   戻り値: [dict: frame_sha256/png_sha256/captured_ms/preserved_ms/empty_board/board_accepted]
# }
def _frame_section(episode: Any, preserved_ms: float | None) -> dict[str, object]:
    probe = getattr(getattr(episode, "source", None), "_failure_frame_probe", None)
    digests = getattr(episode, "_failure_header_digests", None)
    digests = digests if isinstance(digests, dict) else {}
    captured_ms = None
    empty_board = board_accepted = None
    if isinstance(probe, dict):
        captured_at = probe.get("captured_at")
        if type(captured_at) in (int, float) and captured_at > 0:
            try:
                captured_ms = episode.launcher.server.relative_ms(
                    int(captured_at * 1_000_000_000)
                )
            except Exception:
                captured_ms = None
        state = probe.get("state")
        if isinstance(state, dict) and state:
            empty_board = state.get("unavailable_reason") == "empty_nonterminal"
            board_accepted = state.get("state_available") is True
    return {
        "frame_sha256": digests.get("frame_sha256"),
        "png_sha256": digests.get("png_sha256"),
        "captured_ms": captured_ms,
        "preserved_ms": preserved_ms,
        "empty_board": empty_board,
        "board_accepted": board_accepted,
    }


# {
#   責務: [build_correlation: 同一世代のserver/Frame/CDPの証拠を結び、原因カテゴリを付けた相関記録を作る],
#   処理: [launcherが返したparts(世代ID・前後snapshot・CDP時刻)とFrame節を1つのrecordへまとめ、classify_startup_failureを適用する],
#   引数: [episode: 失敗したepisode, browser: CDP結果(correlation_partsを除いたもの), parts: launcher由来の相関部品, preserved_ms: Frame保全時刻]
#   戻り値: [dict: reference-startup-correlation/v1。判定は記録だけで入力や失敗判定には戻さない]
# }
def build_correlation(
    episode: Any, browser: dict, parts: dict, preserved_ms: float | None
) -> dict[str, object]:
    cdp: dict[str, object] = {
        "status": browser.get("status"),
        "started_ms": parts.get("cdp_started_ms"),
        "finished_ms": parts.get("cdp_finished_ms"),
        "expression_sha256": browser.get("expression_sha256"),
    }
    if browser.get("status") == "observed":
        cdp["render"] = browser.get("render")
    record: dict[str, object] = {
        "schema": SCHEMA,
        "correlation_id": parts.get("correlation_id"),
        "episode": getattr(episode, "episode_number", None),
        "seed": getattr(episode.launcher.server, "test_seed", None),
        "server": parts.get("server"),
        "expected_body_sha256": parts.get("expected_body_sha256"),
        "frame": _frame_section(episode, preserved_ms),
        "cdp": cdp,
        "target_matches_generation": parts.get("target_matches_generation") is True,
        "browser_version": parts.get("browser_version"),
        "ci_context": ci_context(),
    }
    record["classification"] = classify_startup_failure(record)
    return record


# {
#   責務: [_preserved_ms: Frame保全を確認した時刻(server開始からの相対ms)を取る],
#   処理: [相関記録が無効、またはlauncherが対応しない時はNoneにし、CDP呼出しを妨げない],
#   引数: [episode: 失敗したepisode]
#   戻り値: [float | None]
# }
def _preserved_ms(episode: Any) -> float | None:
    try:
        snapshot = episode.launcher.server.correlation_snapshot()
        return snapshot["now_ms"] if snapshot is not None else None
    except Exception:
        return None


def collect_episode_failure_diagnostics(
    episode: Any, *, frame_preserved: bool
) -> dict[str, object] | None:
    browser_enabled = getattr(episode, "_save_browser_diagnostics", False) is True
    heartbeat_enabled = getattr(episode, "_save_heartbeat_diagnostics", False) is True
    if not browser_enabled and not heartbeat_enabled:
        return None
    if getattr(episode, "_failure_diagnostics_attempted", False):
        cached = getattr(episode, "_failure_diagnostics", None)
        return cached if isinstance(cached, dict) else None
    episode._failure_diagnostics_attempted = True
    result: dict[str, object] = {
        "event": "reference_game_failure_diagnostics",
        "schema_version": 1,
        "diagnostic_only": True,
        "failure_status_unchanged": True,
        "frame_preserved_before_browser_probe": frame_preserved is True,
    }
    # Snapshot first: the later CDP call can itself change browser/scheduling state.
    if heartbeat_enabled:
        try:
            runtime = getattr(
                getattr(getattr(episode, "pipeline", None), "executor", None),
                "fail_safe_runtime",
                None,
            )
            result["heartbeat"] = (
                runtime.heartbeat_diagnostic_snapshot() if runtime is not None else None
            )
        except Exception as error:
            result["heartbeat"] = {
                "status": "unavailable",
                "error_type": type(error).__name__[:64],
            }
    if browser_enabled:
        if frame_preserved is True:
            try:
                preserved_ms = _preserved_ms(episode)
                browser = episode.launcher.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=True
                )
                parts = browser.pop("correlation_parts", None)
                result["browser"] = browser
                if isinstance(parts, dict):
                    try:
                        result["correlation"] = build_correlation(
                            episode, browser, parts, preserved_ms
                        )
                    except Exception as error:
                        result["correlation"] = {
                            "status": "unavailable",
                            "error_type": type(error).__name__[:64],
                        }
            except Exception as error:
                result["browser"] = {
                    "status": "unavailable",
                    "error_type": type(error).__name__[:64],
                }
        else:
            result["browser"] = {
                "status": "unavailable",
                "reason": "failure_frame_not_preserved",
            }
    for name, maximum_bytes in (
        ("heartbeat", 32 * 1024),
        ("browser", 16 * 1024),
        ("correlation", 24 * 1024),
    ):
        if name in result:
            result[name] = _bounded_section(result[name], maximum_bytes)
    try:
        encoded = json.dumps(
            result, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
            result = {
                "event": "reference_game_failure_diagnostics",
                "diagnostic_only": True,
                "status": "unavailable",
                "reason": "diagnostic_byte_limit",
                "failure_status_unchanged": True,
            }
        path: Path = episode._failure_diagnostics_path
        staged = stage_json_write(path, result)
        try:
            staged.publish()
        finally:
            staged.discard()
    except Exception as error:
        result["persistence_error_type"] = type(error).__name__[:64]
    episode._failure_diagnostics = result
    try:
        print(
            json.dumps(
                result, ensure_ascii=True, separators=(",", ":"), allow_nan=False
            ),
            flush=True,
        )
    except Exception:
        pass
    return result
