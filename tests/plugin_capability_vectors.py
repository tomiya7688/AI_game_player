"""Plugin Capability Manifestの適合ケース（schemas/plugin_capability_manifest/v1）を参照実装で評価するtest helper。"""

from __future__ import annotations

import json
from typing import Any

from ai_game_player.experience import ArtifactReference
from ai_game_player.hook_registration import HookMode, HookRegistration
from ai_game_player.plugin_capability_manifest import (
    PluginManifestError,
    decode_plugin_capability_manifest,
)
from ai_game_player.plugin_capability_policy import (
    PluginCapabilityDenied,
    PluginCapabilityPolicy,
)
from ai_game_player.stage_envelope import StageEnvelope

_ARTIFACT_SHA256 = "0" * 64


# {
#   責務: [manifest_outcome: Manifest適合ケース1件を参照validatorで判定する]
#   処理: [textがあればそのまま、なければdocumentをJSON化して復号し、codeまたはvalidを返す]
#   引数: [case: conformance.jsonの1件]
#   戻り値: [str: validまたはPluginManifestErrorのcode]
# }
def manifest_outcome(case: dict[str, Any]) -> str:
    data = (
        case["text"]
        if "text" in case
        else json.dumps(case["document"], ensure_ascii=False)
    )
    try:
        decode_plugin_capability_manifest(data)
    except PluginManifestError as error:
        return error.code
    return "valid"


# {
#   責務: [enforcement_outcome: 権限判定の適合ケース1件を参照Policyで判定する]
#   処理: [
#     1: Manifestを順に復号してPolicyへ登録する
#     2: Hook登録、入力Envelope、出力Envelopeの順に判定し、最初の拒否codeを返す
#   ]
#   引数: [case: enforcement_conformance.jsonの1件]
#   戻り値: [str: allowまたはPluginManifestError/PluginCapabilityDeniedのcode]
# }
def enforcement_outcome(case: dict[str, Any]) -> str:
    try:
        policy = PluginCapabilityPolicy(
            stage_envelope_schema_version=case["stage_envelope_schema_version"]
        )
        for document in case["manifests"]:
            policy.add(
                decode_plugin_capability_manifest(
                    json.dumps(document, ensure_ascii=False)
                )
            )
        registration = _registration(case["registration"])
        policy.authorize_registration(registration)
        if case["input"] is not None:
            source = _envelope(case["input"], "env-in", None)
            policy.authorize_input(registration, source)
            if case["output"] is not None:
                output = _envelope(case["output"], "env-out", registration)
                policy.authorize_output(registration, output, source)
    except (PluginManifestError, PluginCapabilityDenied) as error:
        return error.code
    return "allow"


# {
#   責務: [_registration: ケースの登録値からHookRegistrationを作る]
#   処理: [callbackは呼ばれないため何もしない関数を入れる]
#   引数: [value: registrationの辞書]
#   戻り値: [HookRegistration: 判定対象の登録]
# }
def _registration(value: dict[str, Any]) -> HookRegistration:
    return HookRegistration(
        hook_id=value["hook_id"],
        hook_version=value["hook_version"],
        mode=HookMode(value["mode"]),
        stage=value["stage"],
        boundary=value["boundary"],
        callback=lambda envelope: None,
        timeout_ns=value["timeout_ns"],
        max_output_bytes=value["max_output_bytes"],
        max_injected=value["max_injected"],
    )


# {
#   責務: [_envelope: ケースのEnvelope要約から検証済みStageEnvelopeを作る]
#   処理: [Policyが読むfieldだけをケースから入れ、artifact_idsを同じsha256・media typeのArtifact参照にする]
#   引数: [value: Envelope要約, envelope_id: 作るEnvelopeのID, producer: 出力ならそのHook登録、入力ならNone]
#   戻り値: [StageEnvelope: 判定に使うEnvelope]
# }
def _envelope(
    value: dict[str, Any], envelope_id: str, producer: HookRegistration | None
) -> StageEnvelope:
    return StageEnvelope(
        envelope_id=envelope_id,
        stage=value["stage"],
        boundary=value["boundary"],
        session_id="session-1",
        timestamp_utc="2026-11-04T00:00:00Z",
        trace_id="trace-1",
        producer_kind="hook" if producer else "core",
        producer_id=producer.hook_id if producer else "core.frame_analyzer",
        producer_version=producer.hook_version if producer else "1",
        parent_envelope_id="env-in" if producer else None,
        derived_from=("env-in",) if producer else (),
        payload_schema=value["payload_schema"],
        payload_schema_version=value["payload_schema_version"],
        artifact_refs=tuple(
            ArtifactReference(artifact_id, _ARTIFACT_SHA256, "image/png", False)
            for artifact_id in value["artifact_ids"]
        ),
    )
