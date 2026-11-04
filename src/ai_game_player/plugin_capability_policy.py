"""Plugin Capability ManifestをHook runtimeの登録・入力・出力へ適用するdeny-by-defaultの権限判定。"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ai_game_player.hook_registration import HookMode, HookRegistration
from ai_game_player.plugin_capability_manifest import (
    HOOK_PERMISSIONS,
    HookCapability,
    PluginCapabilityManifest,
    PluginManifestError,
)
from ai_game_player.stage_envelope import STAGE_ENVELOPE_SCHEMA_VERSION, StageEnvelope


# {
#   責務: [PluginCapabilityDenied: Hookの登録・入力・出力が宣言された権限の外にあることを、機械判定できるcodeで伝える]
#   フィールド: [code: manifest_missing・version_mismatch・permission_unavailable・mode_not_granted・point_not_granted・
#     budget_exceeded・envelope_version_not_granted・payload_schema_not_granted・output_not_granted・artifact_ref_not_granted のいずれか]
# }
class PluginCapabilityDenied(ValueError):
    MANIFEST_MISSING = "manifest_missing"
    VERSION_MISMATCH = "version_mismatch"
    PERMISSION_UNAVAILABLE = "permission_unavailable"
    MODE_NOT_GRANTED = "mode_not_granted"
    POINT_NOT_GRANTED = "point_not_granted"
    BUDGET_EXCEEDED = "budget_exceeded"
    PAYLOAD_SCHEMA_NOT_GRANTED = "payload_schema_not_granted"
    OUTPUT_NOT_GRANTED = "output_not_granted"
    ARTIFACT_REF_NOT_GRANTED = "artifact_ref_not_granted"
    ENVELOPE_VERSION_NOT_GRANTED = "envelope_version_not_granted"

    # {
    #   責務: [__init__: 拒否の分類codeと人が読める理由を保持する例外を作る]
    #   処理: [codeを属性へ保存し、理由文の先頭にcodeを付けてValueErrorへ渡す]
    #   引数: [code: 拒否の分類, message: 対象Hookと条件の説明（payloadは含めない）]
    #   戻り値: [なし]
    # }
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# {
#   責務: [PluginCapabilityDecision: Hook登録の許可・拒否を、payloadを含まない監査用の値として保持する]
#   フィールド: [
#     hook_id/hook_version/mode/stage/boundary: 判定したHook登録の値
#     allowed: 許可したか
#     code: 拒否のcode。許可ならNone
#     plugin_id/plugin_version/manifest_sha256: 照合したManifest。見つからなければNone
#   ]
# }
@dataclass(frozen=True)
class PluginCapabilityDecision:
    hook_id: str
    hook_version: str
    mode: str
    stage: str
    boundary: str
    allowed: bool
    code: str | None
    plugin_id: str | None
    plugin_version: str | None
    manifest_sha256: str | None

    # {
    #   責務: [to_dict: 判定記録をJSON化できる辞書へ変換する]
    #   処理: [全fieldを素の値で返す]
    #   引数: [self: 記録]
    #   戻り値: [dict[str, Any]: 監査用の辞書]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "hook_version": self.hook_version,
            "mode": self.mode,
            "stage": self.stage,
            "boundary": self.boundary,
            "allowed": self.allowed,
            "code": self.code,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "manifest_sha256": self.manifest_sha256,
        }


# {
#   責務: [_Grant: 1つのhook_idに対する検証済みManifestとHook宣言とdigestの組を保持する]
#   フィールド: [manifest: 宣言したManifest, hook: そのHookの宣言, digest: ManifestのSHA-256]
# }
@dataclass(frozen=True)
class _Grant:
    manifest: PluginCapabilityManifest
    hook: HookCapability
    digest: str


# {
#   責務: [PluginCapabilityPolicy: 登録されたManifestだけを根拠に、Hookの登録・入力Envelope・出力Envelopeを許可または拒否する（deny-by-default）]
#   フィールド: [
#     _grants: hook_idごとの宣言（どのManifestにもないhook_idは常に拒否）
#     _plugins: 登録済みplugin_id
#     _stage_envelope_schema_version: このruntimeが扱うStageEnvelopeの版（Manifestの互換範囲と照合する）
#     _decisions: 直近の登録判定の監査記録（上限件数まで）
#   ]
#   補足: [同じprocess内の方針判定であり、sandboxではない。Pluginはimportでfile・network・OS入力APIを直接使えるため、
#     model/network/filesystem権限を宣言したManifestは隔離runtime（#235）ができるまで登録時に拒否する]
# }
class PluginCapabilityPolicy:
    # {
    #   責務: [__init__: 空の判定表を作り、初期Manifestを登録する]
    #   処理: [版と記録上限を固定し、manifestsを順にaddする]
    #   引数: [manifests: 初期登録するManifest, stage_envelope_schema_version: runtimeのStageEnvelope版,
    #     max_decisions: 保持する登録判定記録の上限件数（古いものから捨てる）]
    #   戻り値: [なし]
    #   エラー: [ValueError: max_decisionsが範囲外の場合, PluginManifestError: addが拒否した場合]
    # }
    def __init__(
        self,
        manifests: Iterable[PluginCapabilityManifest] = (),
        *,
        stage_envelope_schema_version: int = STAGE_ENVELOPE_SCHEMA_VERSION,
        max_decisions: int = 256,
    ) -> None:
        if (
            type(stage_envelope_schema_version) is not int
            or stage_envelope_schema_version < 1
        ):
            raise ValueError("stage_envelope_schema_version must be a positive integer")
        if type(max_decisions) is not int or not 1 <= max_decisions <= 10_000:
            raise ValueError("max_decisions must be in [1, 10000]")
        self._stage_envelope_schema_version = stage_envelope_schema_version
        self._grants: dict[str, _Grant] = {}
        self._plugins: set[str] = set()
        self._decisions: deque[PluginCapabilityDecision] = deque(maxlen=max_decisions)
        self._lock = threading.Lock()
        for manifest in manifests:
            self.add(manifest)

    # {
    #   責務: [add: Manifestを再検証して判定表へ加える]
    #   処理: [
    #     1: to_dict→from_dictの往復で、直接構築された値もv1の全規則で再検証する
    #     2: runtimeのStageEnvelope版が互換範囲にない場合はincompatibleで拒否する
    #     3: plugin_idまたはhook_idが登録済みならconflictで拒否する（後勝ちで権限を差し替えない）
    #   ]
    #   引数: [manifest: 登録するManifest]
    #   戻り値: [なし]
    #   エラー: [PluginManifestError: 再検証・互換性・重複のいずれかで拒否した場合]
    # }
    def add(self, manifest: PluginCapabilityManifest) -> None:
        if not isinstance(manifest, PluginCapabilityManifest):
            raise PluginManifestError(
                PluginManifestError.MALFORMED,
                "manifest must be a PluginCapabilityManifest",
            )
        validated = PluginCapabilityManifest.from_dict(manifest.to_dict())
        if (
            self._stage_envelope_schema_version
            not in validated.stage_envelope_schema_versions
        ):
            raise PluginManifestError(
                PluginManifestError.INCOMPATIBLE,
                f"{validated.plugin_id} does not support stage envelope "
                f"v{self._stage_envelope_schema_version}",
            )
        digest = validated.digest()
        with self._lock:
            if validated.plugin_id in self._plugins:
                raise PluginManifestError(
                    PluginManifestError.CONFLICT,
                    f"plugin {validated.plugin_id} is already registered",
                )
            for hook in validated.hooks:
                if hook.hook_id in self._grants:
                    raise PluginManifestError(
                        PluginManifestError.CONFLICT,
                        f"hook {hook.hook_id} is already declared",
                    )
            self._plugins.add(validated.plugin_id)
            for hook in validated.hooks:
                self._grants[hook.hook_id] = _Grant(validated, hook, digest)

    # {
    #   責務: [decisions: 直近の登録判定の監査記録を返す]
    #   処理: [lock内で複製する]
    #   引数: [self: Policy]
    #   戻り値: [tuple[PluginCapabilityDecision, ...]: 古い順の記録]
    # }
    @property
    def decisions(self) -> tuple[PluginCapabilityDecision, ...]:
        with self._lock:
            return tuple(self._decisions)

    # {
    #   責務: [authorize_registration: Hook登録がManifestの宣言内にあるか判定し、結果を監査記録へ残す（HookRuntimeのaccess_policy）]
    #   処理: [
    #     1: hook_idの宣言、hook_versionとplugin_versionの一致、mode、接続点を確認する
    #     2: in-processで付与できない権限（model/network/filesystem）を宣言したManifestを拒否する
    #     3: timeout・出力byte数・inject件数（injectのみ）が宣言の上限以内か確認する
    #   ]
    #   引数: [registration: HookRuntime.registerへ渡された登録]
    #   戻り値: [なし]
    #   エラー: [PluginCapabilityDenied: 宣言の外にある場合（登録されない）]
    # }
    def authorize_registration(self, registration: HookRegistration) -> None:
        try:
            grant = self._grant_for(registration)
            unavailable = sorted(set(grant.manifest.permissions) - HOOK_PERMISSIONS)
            if unavailable:
                raise PluginCapabilityDenied(
                    PluginCapabilityDenied.PERMISSION_UNAVAILABLE,
                    f"{grant.manifest.plugin_id} requires {unavailable[0]}, "
                    "which needs the isolation runtime (#235)",
                )
            budget = grant.hook.budget
            if (
                registration.timeout_ns > budget.timeout_ns
                or registration.max_output_bytes > budget.max_output_bytes
                or (
                    registration.mode is HookMode.INJECT
                    and registration.max_injected > budget.max_injected
                )
            ):
                raise PluginCapabilityDenied(
                    PluginCapabilityDenied.BUDGET_EXCEEDED,
                    f"{registration.hook_id} requests more than its declared budget",
                )
        except PluginCapabilityDenied as error:
            self._record(registration, error.code)
            raise
        self._record(registration, None)

    # {
    #   責務: [authorize_input: Hookへ渡すEnvelopeが宣言した接続点・payload契約の範囲内か判定する（HookRuntimeのaccess_policy）]
    #   処理: [登録の宣言を引き直し、Envelopeのstage・境界とpayload契約・版を確認する]
    #   引数: [registration: 呼び出すHook, envelope: Hookへ渡す予定のEnvelope]
    #   戻り値: [なし]
    #   エラー: [PluginCapabilityDenied: 範囲外の場合（Hookは呼ばれない）]
    # }
    def authorize_input(
        self, registration: HookRegistration, envelope: StageEnvelope
    ) -> None:
        grant = self._grant_for(registration)
        self._require_envelope_version(grant, envelope)
        self._require_readable(grant.hook, envelope)

    # {
    #   責務: [authorize_output: Hookが返したEnvelopeが宣言した出力の範囲内か判定する（HookRuntimeのaccess_policy）]
    #   処理: [
    #     1: modifyは入力と同じ範囲（接続点・payload契約）にあるか確認する
    #     2: injectはinject_targetsのどれかに一致し、Artifact参照が入力Envelopeにあるものだけか確認する
    #     3: readの出力は常に拒否する
    #   ]
    #   引数: [registration: 出力したHook, output: 再検証済みの出力Envelope, source: Hookへ渡した入力Envelope]
    #   戻り値: [なし]
    #   エラー: [PluginCapabilityDenied: 範囲外の場合（その出力は使われない）]
    # }
    def authorize_output(
        self,
        registration: HookRegistration,
        output: StageEnvelope,
        source: StageEnvelope,
    ) -> None:
        grant = self._grant_for(registration)
        self._require_envelope_version(grant, output)
        hook = grant.hook
        if hook.mode is HookMode.MODIFY:
            self._require_readable(hook, output)
            return
        if hook.mode is not HookMode.INJECT or not hook.accepts_injected(
            output.stage,
            output.boundary,
            output.payload_schema,
            output.payload_schema_version,
        ):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.OUTPUT_NOT_GRANTED,
                f"{registration.hook_id} may not produce "
                f"{output.stage}/{output.boundary} {output.payload_schema}",
            )
        if any(ref not in source.artifact_refs for ref in output.artifact_refs):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.ARTIFACT_REF_NOT_GRANTED,
                f"{registration.hook_id} referenced an artifact it did not receive",
            )

    # {
    #   責務: [_grant_for: 登録に対応する宣言を探し、版・mode・接続点が一致するか確認する]
    #   処理: [hook_idで宣言を引き、plugin_version・mode・stage/boundaryを順に照合する]
    #   引数: [registration: 判定するHook登録]
    #   戻り値: [_Grant: 一致した宣言]
    #   エラー: [PluginCapabilityDenied: 宣言がない、または一致しない場合]
    # }
    def _grant_for(self, registration: HookRegistration) -> _Grant:
        with self._lock:
            grant = self._grants.get(registration.hook_id)
        if grant is None:
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.MANIFEST_MISSING,
                f"no manifest declares hook {registration.hook_id}",
            )
        if registration.hook_version != grant.manifest.plugin_version:
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.VERSION_MISMATCH,
                f"{registration.hook_id} version differs from its manifest",
            )
        if registration.mode is not grant.hook.mode:
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.MODE_NOT_GRANTED,
                f"{registration.hook_id} is not declared as {registration.mode.value}",
            )
        if (registration.stage, registration.boundary) != (
            grant.hook.stage,
            grant.hook.boundary,
        ):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.POINT_NOT_GRANTED,
                f"{registration.hook_id} is not declared at "
                f"{registration.stage}/{registration.boundary}",
            )
        return grant

    # {
    #   責務: [_require_readable: Envelopeが宣言の接続点とpayload契約の範囲内か確認する]
    #   処理: [stage・境界の一致と、payload契約・版の許可を確認する]
    #   引数: [hook: Hookの宣言, envelope: 判定するEnvelope]
    #   戻り値: [なし]
    #   エラー: [PluginCapabilityDenied: point_not_grantedまたはpayload_schema_not_granted]
    # }
    # {
    #   責務: [_require_envelope_version: EnvelopeのStageEnvelope版が、このruntimeとManifestの互換範囲の両方に一致するか確認する]
    #   処理: [schema_versionをruntimeの版と比べ、Manifestのstage_envelope_schema_versionsに含まれるか確認する]
    #   引数: [grant: 照合した宣言, envelope: 判定するEnvelope]
    #   戻り値: [なし]
    #   エラー: [PluginCapabilityDenied(envelope_version_not_granted): どちらかに一致しない場合]
    # }
    def _require_envelope_version(self, grant: _Grant, envelope: StageEnvelope) -> None:
        version = envelope.schema_version
        if (
            version != self._stage_envelope_schema_version
            or version not in grant.manifest.stage_envelope_schema_versions
        ):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.ENVELOPE_VERSION_NOT_GRANTED,
                f"{grant.hook.hook_id} may not receive stage envelope v{version}",
            )

    @staticmethod
    def _require_readable(hook: HookCapability, envelope: StageEnvelope) -> None:
        if (envelope.stage, envelope.boundary) != (hook.stage, hook.boundary):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.POINT_NOT_GRANTED,
                f"{hook.hook_id} is not declared at {envelope.stage}/{envelope.boundary}",
            )
        if not hook.accepts_input(
            envelope.payload_schema, envelope.payload_schema_version
        ):
            raise PluginCapabilityDenied(
                PluginCapabilityDenied.PAYLOAD_SCHEMA_NOT_GRANTED,
                f"{hook.hook_id} may not read {envelope.payload_schema} "
                f"v{envelope.payload_schema_version}",
            )

    # {
    #   責務: [_record: 登録判定を監査記録へ追加する]
    #   処理: [hook_idで宣言を引き、登録値・結果・Manifestの識別子を記録にし、上限を超えたら古いものから捨てる]
    #   引数: [registration: 判定したHook登録, code: 拒否のcode（許可はNone）]
    #   戻り値: [なし]
    # }
    def _record(
        self,
        registration: HookRegistration,
        code: str | None,
    ) -> None:
        with self._lock:
            grant = self._grants.get(registration.hook_id)
        decision = PluginCapabilityDecision(
            hook_id=registration.hook_id,
            hook_version=registration.hook_version,
            mode=registration.mode.value,
            stage=registration.stage,
            boundary=registration.boundary,
            allowed=code is None,
            code=code,
            plugin_id=grant.manifest.plugin_id if grant else None,
            plugin_version=grant.manifest.plugin_version if grant else None,
            manifest_sha256=grant.digest if grant else None,
        )
        with self._lock:
            self._decisions.append(decision)
