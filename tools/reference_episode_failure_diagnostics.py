"""明示検証の失敗時だけ、close前の固定診断を保存する。原因修復や観測には使わない。"""

import json
from pathlib import Path
from typing import Any, cast

from ai_game_player.atomic_json import stage_json_write

MAX_DIAGNOSTIC_BYTES = 64 * 1024


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
                result["browser"] = episode.launcher.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=True
                )
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
    for name, maximum_bytes in (("heartbeat", 32 * 1024), ("browser", 16 * 1024)):
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
