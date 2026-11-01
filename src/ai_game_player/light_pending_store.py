"""Stop時の受理済み数値事実を退避する。Knowledgeの学習・公開は行わない。"""

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.experience import ExperienceEvent
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_snapshot import _unique_object
from ai_game_player.knowledge_light_validation import light_canonical_events
from ai_game_player.knowledge_update_sample import KnowledgeUpdateSample
from ai_game_player.cross_process_lock import cross_process_file_lock
from ai_game_player.bounded_file import read_regular_file


MAX_PENDING_EVENTS = 128
MAX_PENDING_BYTES = 1024 * 1024


# {
#   責務: [read_pending_deltas: scope/digestを照合し有限のcanonical事実を読む]
#   処理: [FIFO/symlinkを拒否し、通常FDの容量・件数を検証する。読むだけではactiveを変更しない]
#   引数: [path: 退避先, scope: 完全一致範囲] 戻り値: [tuple: 最大128件の事実]
#   エラー: [OSError/ValueError: 読込/種別/容量/契約不正]
# }
def read_pending_deltas(
    path: Path, scope: KnowledgeScope
) -> tuple[ExperienceEvent, ...]:
    try:
        payload = read_regular_file(path, max_bytes=MAX_PENDING_BYTES)
    except FileNotFoundError:
        return ()
    value = json.loads(payload, object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "scope", "events", "sha256"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or KnowledgeScope.from_dict(value["scope"]) != scope
        or not isinstance(value["events"], list)
        or len(value["events"]) > MAX_PENDING_EVENTS
    ):
        raise ValueError("invalid pending checkpoint")
    encoded = json.dumps(
        value["events"], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if sha256(encoded).hexdigest() != value["sha256"]:
        raise ValueError("pending checkpoint digest mismatch")
    events = tuple(ExperienceEvent.from_dict(event) for event in value["events"])
    for event in events:
        light_canonical_events((event,))
    return events


# {
#   責務: [write_pending_deltas: Stop済みSessionの受理した残りを一つの有限checkpointへ退避する]
#   処理: [scope lock内で既存とID/hash照合してmergeし、durable Championのwatermark以下だけ除外する。
#     件数・byteを制限してatomic置換する。Modeを迂回した学習は行わない]
#   引数: [path/scope: 隔離先, events: 受理済み事実, source_watermarks/correlation_watermarks: 現Championの消費系列]
#   戻り値: [int: 有限retention上限による明示drop件数]
#   エラー: [OSError/ValueError: 保存失敗/予算超過。旧checkpoint維持]
# }
def write_pending_deltas(
    path: Path,
    scope: KnowledgeScope,
    events: tuple[ExperienceEvent, ...],
    *,
    source_watermarks: dict[str, int] | None = None,
    correlation_watermarks: dict[str, int] | None = None,
) -> int:
    if len(events) > MAX_PENDING_EVENTS:
        raise ValueError("pending event capacity exceeded")
    with cross_process_file_lock(path.with_suffix(".lock")):
        merged: dict[str, dict[str, Any]] = {}
        for event in (*read_pending_deltas(path, scope), *events):
            sample = KnowledgeUpdateSample.from_event(event)
            if sample.source_sequence <= (source_watermarks or {}).get(
                sample.source_id, -1
            ) or sample.correlation_sequence <= (correlation_watermarks or {}).get(
                sample.correlation_id, -1
            ):
                continue
            value = light_canonical_events((event,))[0]
            previous = merged.get(event.event_id)
            if previous is not None and previous != value:
                raise ValueError("pending event identifier has conflicting facts")
            merged[event.event_id] = value
        values: list[dict[str, Any]] = []
        size = 2
        dropped = 0
        for value in merged.values():
            encoded_event = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            if (
                len(values) >= MAX_PENDING_EVENTS
                or size + len(encoded_event) > MAX_PENDING_BYTES - 2048
            ):
                dropped += 1
                continue
            values.append(value)
            size += len(encoded_event) + 1
        encoded = json.dumps(
            values, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        staged = stage_json_write(
            path,
            {
                "schema_version": 1,
                "scope": scope.to_dict(),
                "events": values,
                "sha256": sha256(encoded).hexdigest(),
            },
        )
        try:
            staged.publish()
        finally:
            staged.discard()
        return dropped
