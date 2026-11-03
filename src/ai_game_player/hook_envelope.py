"""Hookが入力Envelopeから出力Envelopeを作るための補助関数。"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ai_game_player.hook_registration import HookRegistration
from ai_game_player.stage_envelope import StageEnvelope


_UNSET: Any = object()


# {
#   責務: [derive_hook_envelope: 入力Envelopeから、Hook出力として必要なprovenanceを付けた新しい検証済みEnvelopeを作る]
#   処理: [
#     1: producer_kindを`hook`、producer_id/producer_versionを登録値、parent_envelope_idとderived_fromを入力IDにする
#     2: 指定されたpayload・stage・boundary・payload契約・confidence・metadataだけを置き換え、他は入力から引き継ぐ
#     3: `dataclasses.replace()`でStageEnvelopeを作り直し、作成時の厳密な検証を通す
#   ]
#   引数: [source: Hookが受け取った入力Envelope, registration: 出力するHookの登録,
#     payload/stage/boundary/payload_schema/payload_schema_version/confidence/metadata: 置き換える値（省略時は入力のまま）,
#     envelope_id: 新しいID（省略時はUUID4）, timestamp_utc: 作成時刻（省略時は現在のUTC）]
#   戻り値: [StageEnvelope: Hook runtimeの出力検査を満たすprovenanceを持つEnvelope]
#   エラー: [StageEnvelopeError: 置き換え後の値がv1契約に違反する場合]
# }
def derive_hook_envelope(
    source: StageEnvelope,
    registration: HookRegistration,
    *,
    payload: Mapping[str, Any] = _UNSET,
    stage: str = _UNSET,
    boundary: str = _UNSET,
    payload_schema: str = _UNSET,
    payload_schema_version: int = _UNSET,
    confidence: float | None = _UNSET,
    metadata: Mapping[str, Any] = _UNSET,
    envelope_id: str | None = None,
    timestamp_utc: str | None = None,
) -> StageEnvelope:
    changes: dict[str, Any] = {
        "envelope_id": envelope_id or f"hook-{uuid.uuid4().hex}",
        "timestamp_utc": timestamp_utc or _utc_now_text(),
        "producer_kind": "hook",
        "producer_id": registration.hook_id,
        "producer_version": registration.hook_version,
        "parent_envelope_id": source.envelope_id,
        "derived_from": (source.envelope_id,),
        "monotonic_ns": None,
    }
    for name, value in (
        ("payload", payload),
        ("stage", stage),
        ("boundary", boundary),
        ("payload_schema", payload_schema),
        ("payload_schema_version", payload_schema_version),
        ("confidence", confidence),
        ("metadata", metadata),
    ):
        if value is not _UNSET:
            changes[name] = value
    return dataclasses.replace(source, **changes)


# {
#   責務: [_utc_now_text: StageEnvelopeの`timestamp_utc`字面で現在時刻を返す]
#   処理: [UTCのmicrosecond精度ISO 8601文字列の末尾を`Z`にする]
#   引数: [なし]
#   戻り値: [str: 例 `2026-11-04T09:30:00.123456Z`]
# }
def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
