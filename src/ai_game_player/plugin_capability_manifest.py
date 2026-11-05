"""Plugin Capability Manifest v1：PluginのHook接続点・mode・payload契約・予算・権限IDを宣言する版付き契約。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ai_game_player.hook_registration import (
    HOOK_MAX_INJECTED_PER_HOOK,
    HOOK_MAX_TIMEOUT_NS,
    HookMode,
)
from ai_game_player.stage_envelope import (
    CORE_STAGE_IDS,
    STAGE_BOUNDARIES,
    STAGE_ENVELOPE_INTEGER_MAX,
    STAGE_ENVELOPE_MAX_ENCODED_BYTES,
    StageEnvelopeError,
)
from ai_game_player.stage_envelope_codec import (
    _decode_utf8_text,
    _object_without_duplicate_keys,
    _parse_binary64_number,
    _reject_excessive_nesting,
    _reject_non_finite_constant,
)


PLUGIN_MANIFEST_VERSION = 1
PLUGIN_MANIFEST_MAX_ENCODED_BYTES = 65_536
PLUGIN_MANIFEST_MAX_HOOKS = 32
PLUGIN_MANIFEST_MAX_PERMISSIONS = 16
PLUGIN_MANIFEST_MAX_PAYLOAD_SCHEMAS = 16
PLUGIN_MANIFEST_MAX_INJECT_TARGETS = 16
PLUGIN_MANIFEST_MAX_VERSIONS = 16
PLUGIN_MANIFEST_MAX_DATA_CLASSES = 32
PLUGIN_MANIFEST_MAX_PUBLISHER_LENGTH = 128
PLUGIN_MANIFEST_MAX_DESCRIPTION_LENGTH = 512
# in-process runtime（#151）が登録・dispatch時に判定できる権限。modeごとに1つ。
HOOK_PERMISSIONS = frozenset({"hook.read", "hook.modify", "hook.inject"})
# 宣言はできるが、同じprocessでは制限できないため隔離runtime（#235）まで付与しない権限。
ISOLATION_REQUIRED_PERMISSIONS = frozenset(
    {"model.invoke", "network.connect", "filesystem.read", "filesystem.write"}
)
# どの実行形態でも付与しない権限。直接のOS入力とSafety Required経路の無効化はPluginへ渡さない。
FORBIDDEN_PERMISSIONS = frozenset({"input.direct", "safety.override"})
KNOWN_PERMISSIONS = (
    HOOK_PERMISSIONS | ISOLATION_REQUIRED_PERMISSIONS | FORBIDDEN_PERMISSIONS
)
# Safety判定と実入力の境界。Pluginはこのstageを観測だけでき、加工・派生はできない。
PLUGIN_READ_ONLY_STAGES = frozenset({"safety", "execution"})
PLUGIN_MANIFEST_FIELDS = (
    "manifest_version",
    "plugin_id",
    "plugin_version",
    "compatibility",
    "permissions",
    "hooks",
    "audit",
)
_HOOK_FIELDS = (
    "hook_id",
    "mode",
    "stage",
    "boundary",
    "input_payload_schemas",
    "inject_targets",
    "budget",
    "data_classes",
)
_BUDGET_FIELDS = ("timeout_ns", "max_output_bytes", "max_injected")
_PAYLOAD_GRANT_FIELDS = ("payload_schema", "versions")
_INJECT_TARGET_FIELDS = ("stage", "boundary", "payload_schema", "versions")
_COMPATIBILITY_FIELDS = ("stage_envelope_schema_versions",)
_AUDIT_FIELDS = ("publisher", "description")
# StageEnvelopeのproducer_id・payload_schemaと同じ名前空間付きIDの字面。
_NAMESPACED_ID_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_VERSION_TEXT_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_EXTENSION_STAGE_PATTERN = re.compile(r"ext\.[a-z][a-z0-9_.-]{0,123}")
_PERMISSION_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


# {
#   責務: [PluginManifestError: Manifestの復号・検証・登録が失敗した理由を機械判定できるcodeと一緒に伝える]
#   フィールド: [code: malformed・unsupported_version・limit_exceeded・unknown_permission・forbidden_permission・
#     undeclared_permission（StageEnvelope v1と共通の3つ＋権限の3つ）、またはPolicy登録時のincompatible・conflictのいずれか]
# }
class PluginManifestError(ValueError):
    MALFORMED = "malformed"
    UNSUPPORTED_VERSION = "unsupported_version"
    LIMIT_EXCEEDED = "limit_exceeded"
    UNKNOWN_PERMISSION = "unknown_permission"
    FORBIDDEN_PERMISSION = "forbidden_permission"
    UNDECLARED_PERMISSION = "undeclared_permission"
    INCOMPATIBLE = "incompatible"
    CONFLICT = "conflict"

    # {
    #   責務: [__init__: 失敗分類codeと人が読める理由を保持する例外を作る]
    #   処理: [codeを属性へ保存し、理由文の先頭にcodeを付けてValueErrorへ渡す]
    #   引数: [code: 失敗分類, message: 違反したfieldと条件の説明]
    #   戻り値: [なし]
    # }
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# {
#   責務: [PayloadSchemaGrant: Hookが受け取ってよいpayload契約IDと版の組を保持する]
#   フィールド: [payload_schema: StageEnvelopeのpayload_schema, versions: 許可するpayload_schema_version（昇順・重複なし）]
# }
@dataclass(frozen=True)
class PayloadSchemaGrant:
    payload_schema: str
    versions: tuple[int, ...]

    # {
    #   責務: [allows: payload契約IDと版がこの許可に含まれるか返す]
    #   処理: [IDの完全一致と版の包含を確認する]
    #   引数: [payload_schema: 判定するpayload契約ID, version: その版]
    #   戻り値: [bool: 許可されていればTrue]
    # }
    def allows(self, payload_schema: str, version: int) -> bool:
        return payload_schema == self.payload_schema and version in self.versions

    # {
    #   責務: [to_dict: 許可を素のJSON値へ変換する]
    #   処理: [版の組をlistにする]
    #   引数: [self: 許可]
    #   戻り値: [dict[str, Any]: Manifest文書の1要素]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {"payload_schema": self.payload_schema, "versions": list(self.versions)}


# {
#   責務: [InjectTarget: inject Hookが作ってよい派生Envelopeのstage・境界・payload契約を保持する]
#   フィールド: [stage/boundary: 派生Envelopeの接続点（Safety・Executionは不可）, payload_schema/versions: 作ってよいpayload契約と版]
# }
@dataclass(frozen=True)
class InjectTarget:
    stage: str
    boundary: str
    payload_schema: str
    versions: tuple[int, ...]

    # {
    #   責務: [allows: 派生Envelopeの接続点とpayload契約がこの許可に一致するか返す]
    #   処理: [stage・boundary・payload契約IDの完全一致と版の包含を確認する]
    #   引数: [stage/boundary: 派生Envelopeの接続点, payload_schema/version: そのpayload契約と版]
    #   戻り値: [bool: 許可されていればTrue]
    # }
    def allows(
        self, stage: str, boundary: str, payload_schema: str, version: int
    ) -> bool:
        return (
            stage == self.stage
            and boundary == self.boundary
            and payload_schema == self.payload_schema
            and version in self.versions
        )

    # {
    #   責務: [to_dict: 許可を素のJSON値へ変換する]
    #   処理: [版の組をlistにする]
    #   引数: [self: 許可]
    #   戻り値: [dict[str, Any]: Manifest文書の1要素]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "boundary": self.boundary,
            "payload_schema": self.payload_schema,
            "versions": list(self.versions),
        }


# {
#   責務: [HookBudget: Hook登録が要求してよい予算の上限を保持する]
#   フィールド: [timeout_ns: 1回の呼び出しの上限（ns）, max_output_bytes: 出力1件のbyte上限, max_injected: injectの1回の件数上限（inject以外は0）]
# }
@dataclass(frozen=True)
class HookBudget:
    timeout_ns: int
    max_output_bytes: int
    max_injected: int

    # {
    #   責務: [to_dict: 予算を素のJSON値へ変換する]
    #   処理: [3つの上限を辞書にする]
    #   引数: [self: 予算]
    #   戻り値: [dict[str, Any]: Manifest文書のbudget]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_ns": self.timeout_ns,
            "max_output_bytes": self.max_output_bytes,
            "max_injected": self.max_injected,
        }


# {
#   責務: [HookCapability: 1つのHookに許す接続点・mode・入力/出力のpayload契約・予算・扱うdata classを保持する]
#   フィールド: [
#     hook_id: HookRegistrationのhook_id（plugin_id自身か`<plugin_id>.`で始まる）
#     mode: read/modify/injectのいずれか1つ
#     stage/boundary: 接続してよいstage境界
#     input_payload_schemas: 受け取ってよいpayload契約と版
#     inject_targets: injectが作ってよい派生Envelope（inject以外は空）
#     budget: 登録が要求してよい予算の上限
#     data_classes: 扱うdata classの宣言（#224の語彙。v1では監査用で判定には使わない）
#   ]
# }
@dataclass(frozen=True)
class HookCapability:
    hook_id: str
    mode: HookMode
    stage: str
    boundary: str
    input_payload_schemas: tuple[PayloadSchemaGrant, ...]
    inject_targets: tuple[InjectTarget, ...]
    budget: HookBudget
    data_classes: tuple[str, ...]

    # {
    #   責務: [accepts_input: Hookへ渡すEnvelopeのpayload契約が許可されているか返す]
    #   処理: [input_payload_schemasのどれか1つが一致するか確認する]
    #   引数: [payload_schema/version: Envelopeのpayload契約と版]
    #   戻り値: [bool: 許可されていればTrue]
    # }
    def accepts_input(self, payload_schema: str, version: int) -> bool:
        return any(
            grant.allows(payload_schema, version)
            for grant in self.input_payload_schemas
        )

    # {
    #   責務: [accepts_injected: inject出力の接続点とpayload契約が許可されているか返す]
    #   処理: [inject_targetsのどれか1つが一致するか確認する]
    #   引数: [stage/boundary/payload_schema/version: 派生Envelopeの値]
    #   戻り値: [bool: 許可されていればTrue]
    # }
    def accepts_injected(
        self, stage: str, boundary: str, payload_schema: str, version: int
    ) -> bool:
        return any(
            target.allows(stage, boundary, payload_schema, version)
            for target in self.inject_targets
        )

    # {
    #   責務: [to_dict: Hookの宣言を素のJSON値へ変換する]
    #   処理: [入れ子の許可と予算も辞書・listにする]
    #   引数: [self: 宣言]
    #   戻り値: [dict[str, Any]: Manifest文書のhooksの1要素]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "mode": self.mode.value,
            "stage": self.stage,
            "boundary": self.boundary,
            "input_payload_schemas": [
                grant.to_dict() for grant in self.input_payload_schemas
            ],
            "inject_targets": [target.to_dict() for target in self.inject_targets],
            "budget": self.budget.to_dict(),
            "data_classes": list(self.data_classes),
        }


# {
#   責務: [PluginCapabilityManifest: 1つのPluginが要求する権限とHookごとの接続範囲を、言語中立なv1契約として保持する]
#   フィールド: [
#     plugin_id/plugin_version: Pluginの名前空間IDと版（Hook登録のhook_versionと一致させ、監査に使う）
#     stage_envelope_schema_versions: 対応するStageEnvelope契約の版
#     permissions: 要求する権限ID（昇順・重複なし）。hook.*は使うmodeと一致する
#     hooks: Hookごとの宣言（hook_id昇順）
#     publisher/description: 監査用の公開者名と説明（制御文字なし）
#     manifest_version: Manifest契約の版（v1では1だけ）
#   ]
#   補足: [直接構築した値は検証されない。from_dict・decode_plugin_capability_manifestで作り、Policyは登録時にto_dictの往復で再検証する]
# }
@dataclass(frozen=True)
class PluginCapabilityManifest:
    plugin_id: str
    plugin_version: str
    stage_envelope_schema_versions: tuple[int, ...]
    permissions: tuple[str, ...]
    hooks: tuple[HookCapability, ...]
    publisher: str
    description: str
    manifest_version: int = PLUGIN_MANIFEST_VERSION

    # {
    #   責務: [from_dict: JSON文書をv1の全規則で検証し、正規化した不変Manifestを作る]
    #   処理: [
    #     1: manifest_versionの型と版を確認し、最上位fieldが過不足なく揃っているか確認する
    #     2: plugin_id・plugin_version・compatibility・permissions・hooks・auditの順に検証する（配列は先頭から、最初の違反で止める）
    #     3: hook_idの重複と、hook.*権限と使うmodeの一致を確認する
    #   ]
    #   引数: [document: 復号済みのJSON object]
    #   戻り値: [PluginCapabilityManifest: 権限と版を昇順にし、hooksをhook_id順に並べた検証済みManifest]
    #   エラー: [PluginManifestError: 違反の分類codeを持つ]
    # }
    @classmethod
    def from_dict(cls, document: Any) -> PluginCapabilityManifest:
        if not isinstance(document, Mapping):
            _malformed("manifest must be a JSON object")
        version = document.get("manifest_version")
        if type(version) is not int:
            _malformed("manifest_version must be an integer")
        if version != PLUGIN_MANIFEST_VERSION:
            raise PluginManifestError(
                PluginManifestError.UNSUPPORTED_VERSION,
                f"manifest_version {version} is not supported",
            )
        _require_exact_fields(document, PLUGIN_MANIFEST_FIELDS, "manifest")
        plugin_id = _namespaced_id(document["plugin_id"], "plugin_id")
        plugin_version = _version_text(document["plugin_version"], "plugin_version")
        compatibility = _object(document["compatibility"], "compatibility")
        _require_exact_fields(compatibility, _COMPATIBILITY_FIELDS, "compatibility")
        envelope_versions = _versions(
            compatibility["stage_envelope_schema_versions"],
            "stage_envelope_schema_versions",
        )
        permissions = _permissions(document["permissions"])
        hooks = _hooks(document["hooks"], plugin_id)
        used = {f"hook.{hook.mode.value}" for hook in hooks}
        declared = {item for item in permissions if item in HOOK_PERMISSIONS}
        missing = sorted(used - declared)
        if missing:
            raise PluginManifestError(
                PluginManifestError.UNDECLARED_PERMISSION,
                f"hooks use undeclared permission {missing[0]}",
            )
        if declared - used:
            _malformed("hook permissions must match the modes used by hooks")
        audit = _object(document["audit"], "audit")
        _require_exact_fields(audit, _AUDIT_FIELDS, "audit")
        publisher = _text(
            audit["publisher"], "publisher", 1, PLUGIN_MANIFEST_MAX_PUBLISHER_LENGTH
        )
        description = _text(
            audit["description"],
            "description",
            0,
            PLUGIN_MANIFEST_MAX_DESCRIPTION_LENGTH,
        )
        return cls(
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            stage_envelope_schema_versions=envelope_versions,
            permissions=tuple(sorted(permissions)),
            hooks=tuple(sorted(hooks, key=lambda hook: hook.hook_id)),
            publisher=publisher,
            description=description,
        )

    # {
    #   責務: [hook: hook_idに対応するHook宣言を返す]
    #   処理: [hooksを走査して一致を探す]
    #   引数: [hook_id: HookRegistrationのhook_id]
    #   戻り値: [HookCapability | None: 宣言がなければNone]
    # }
    def hook(self, hook_id: str) -> HookCapability | None:
        for hook in self.hooks:
            if hook.hook_id == hook_id:
                return hook
        return None

    # {
    #   責務: [to_dict: Manifestを素のJSON値へ変換する]
    #   処理: [全fieldをv1文書の形の辞書・listにする]
    #   引数: [self: Manifest]
    #   戻り値: [dict[str, Any]: from_dictで同じ値に戻る文書]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "compatibility": {
                "stage_envelope_schema_versions": list(
                    self.stage_envelope_schema_versions
                )
            },
            "permissions": list(self.permissions),
            "hooks": [hook.to_dict() for hook in self.hooks],
            "audit": {"publisher": self.publisher, "description": self.description},
        }

    # {
    #   責務: [digest: 監査記録で同じ宣言を特定できるSHA-256を返す]
    #   処理: [from_dictで正規化し直した値を、key昇順・空白なし・非ASCIIそのままのUTF-8 JSONにしてhashする（直接構築した値も同じ順序になる）]
    #   引数: [self: Manifest]
    #   戻り値: [str: 小文字16進のSHA-256]
    #   エラー: [PluginManifestError: 直接構築した値がv1の規則に違反する場合]
    # }
    def digest(self) -> str:
        normalized = PluginCapabilityManifest.from_dict(self.to_dict())
        encoded = json.dumps(
            normalized.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


# {
#   責務: [decode_plugin_capability_manifest: 外部から受け取ったManifestのbytes/文字列を上限付きで復号し検証する]
#   処理: [
#     1: 64KiBを超える入力をparse前に拒否し、UTF-8・BOMなし・入れ子深さ32以下をStageEnvelopeと同じ規則で確認する
#     2: 重複key・NaN/Infinity・binary64のoverflow/underflowを拒否してJSONを読む
#     3: from_dictでv1の全規則を検証する
#   ]
#   引数: [data: Manifest文書のUTF-8 bytesまたは文字列]
#   戻り値: [PluginCapabilityManifest: 検証済みManifest]
#   エラー: [PluginManifestError: 復号・検証の失敗。StageEnvelope共通の失敗は同じcodeへ写す]
# }
def decode_plugin_capability_manifest(data: bytes | str) -> PluginCapabilityManifest:
    if isinstance(data, (bytes, bytearray, str)) and (
        len(data) > PLUGIN_MANIFEST_MAX_ENCODED_BYTES
    ):
        raise PluginManifestError(
            PluginManifestError.LIMIT_EXCEEDED, "manifest exceeds the byte limit"
        )
    try:
        text = _decode_utf8_text(data)
        if len(text.encode("utf-8")) > PLUGIN_MANIFEST_MAX_ENCODED_BYTES:
            raise PluginManifestError(
                PluginManifestError.LIMIT_EXCEEDED, "manifest exceeds the byte limit"
            )
        _reject_excessive_nesting(text)
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_parse_binary64_number,
        )
    except StageEnvelopeError as error:
        raise PluginManifestError(error.code, str(error)) from error
    except ValueError as error:
        if isinstance(error, PluginManifestError):
            raise
        raise PluginManifestError(
            PluginManifestError.MALFORMED, f"invalid JSON: {error}"
        ) from error
    return PluginCapabilityManifest.from_dict(document)


# {
#   責務: [_malformed: malformedの例外を送出する]
#   処理: [codeをmalformedにして送出する]
#   引数: [message: 違反の説明]
#   戻り値: [なし（常に送出）]
#   エラー: [PluginManifestError(malformed): 常に]
# }
def _malformed(message: str) -> Any:
    raise PluginManifestError(PluginManifestError.MALFORMED, message)


# {
#   責務: [_object: 値がJSON objectか確認する]
#   処理: [Mapping以外を拒否する]
#   引数: [value: 検査値, name: エラー文のfield名]
#   戻り値: [Mapping[str, Any]: 同じ値]
#   エラー: [PluginManifestError(malformed): objectでない場合]
# }
def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _malformed(f"{name} must be an object")
    return value


# {
#   責務: [_array: 値がJSON配列で件数が範囲内か確認する]
#   処理: [list/tuple以外をmalformed、下限未満をmalformed、上限超過をlimit_exceededにする]
#   引数: [value: 検査値, name: field名, minimum/maximum: 許容件数]
#   戻り値: [list[Any]: 要素のlist]
#   エラー: [PluginManifestError: 型・件数の違反]
# }
def _array(value: Any, name: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        _malformed(f"{name} must be an array")
    if len(value) > maximum:
        raise PluginManifestError(
            PluginManifestError.LIMIT_EXCEEDED,
            f"{name} has more than {maximum} items",
        )
    if len(value) < minimum:
        _malformed(f"{name} must have at least {minimum} items")
    return list(value)


# {
#   責務: [_require_exact_fields: objectのkeyが定義どおり過不足ないか確認する]
#   処理: [未知keyと欠落keyを探す]
#   引数: [value: 検査するobject, fields: 必須かつ唯一のkey, name: エラー文の対象名]
#   戻り値: [なし]
#   エラー: [PluginManifestError(malformed): 未知・欠落keyがある場合]
# }
def _require_exact_fields(
    value: Mapping[str, Any], fields: tuple[str, ...], name: str
) -> None:
    keys = set(value)
    if keys != set(fields):
        unknown = sorted(str(key) for key in keys - set(fields))
        missing = sorted(set(fields) - keys)
        _malformed(f"{name} fields differ: unknown={unknown} missing={missing}")


# {
#   責務: [_pattern_text: 値が字面規則に一致する文字列か確認する]
#   処理: [str以外と全体一致しない値を拒否する]
#   引数: [value: 検査値, name: field名, pattern: 全体一致させる正規表現]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): 型・字面の違反]
# }
def _pattern_text(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _malformed(f"{name} has an invalid format")
    return value


# {
#   責務: [_namespaced_id: 名前空間付き小文字IDを検査する]
#   処理: [StageEnvelopeと同じ字面に一致させる]
#   引数: [value: 検査値, name: field名]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): 字面の違反]
# }
def _namespaced_id(value: Any, name: str) -> str:
    return _pattern_text(value, name, _NAMESPACED_ID_PATTERN)


# {
#   責務: [_version_text: 版表記を検査する]
#   処理: [StageEnvelopeのproducer_versionと同じ字面に一致させる]
#   引数: [value: 検査値, name: field名]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): 字面の違反]
# }
def _version_text(value: Any, name: str) -> str:
    return _pattern_text(value, name, _VERSION_TEXT_PATTERN)


# {
#   責務: [_text: 監査用の自由文字列を検査する]
#   処理: [型・文字数（code point）・制御文字・対のないsurrogateを確認する]
#   引数: [value: 検査値, name: field名, minimum/maximum: 許容文字数]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): いずれかの違反]
# }
def _text(value: Any, name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        _malformed(f"{name} must be a string of {minimum}-{maximum} characters")
    if _CONTROL_CHARACTER_PATTERN.search(value):
        _malformed(f"{name} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _malformed(f"{name} must not contain unpaired surrogates")
    return value


# {
#   責務: [_int_in_range: 予算・版などの整数を検査する]
#   処理: [boolを除く整数で閉区間内か確認する]
#   引数: [value: 検査値, name: field名, minimum/maximum: 許容範囲]
#   戻り値: [int: 同じ値]
#   エラー: [PluginManifestError(malformed): 型・範囲の違反]
# }
def _int_in_range(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _malformed(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


# {
#   責務: [_versions: 版番号の配列を検査し昇順の組にする]
#   処理: [1-16件、各要素が1以上int64以下の整数で重複しないか確認する]
#   引数: [value: 検査値, name: field名]
#   戻り値: [tuple[int, ...]: 昇順の版]
#   エラー: [PluginManifestError: 型・件数・範囲・重複の違反]
# }
def _versions(value: Any, name: str) -> tuple[int, ...]:
    items = _array(value, name, 1, PLUGIN_MANIFEST_MAX_VERSIONS)
    versions = [
        _int_in_range(item, name, 1, STAGE_ENVELOPE_INTEGER_MAX) for item in items
    ]
    if len(set(versions)) != len(versions):
        _malformed(f"{name} must not contain duplicates")
    return tuple(sorted(versions))


# {
#   責務: [_permissions: 権限IDの配列を検査する]
#   処理: [
#     1: 1-16件の配列で、各要素が権限IDの字面に一致し重複しないか確認する
#     2: 先頭から、未知の権限をunknown_permission、付与しない権限をforbidden_permissionにする
#   ]
#   引数: [value: permissions field]
#   戻り値: [list[str]: 文書順の権限ID]
#   エラー: [PluginManifestError: 字面・件数・重複・未知・禁止の違反]
# }
def _permissions(value: Any) -> list[str]:
    items = _array(value, "permissions", 1, PLUGIN_MANIFEST_MAX_PERMISSIONS)
    seen: set[str] = set()
    for item in items:
        permission = _pattern_text(item, "permission", _PERMISSION_PATTERN)
        if permission in seen:
            _malformed(f"permission {permission} is duplicated")
        seen.add(permission)
        if permission not in KNOWN_PERMISSIONS:
            raise PluginManifestError(
                PluginManifestError.UNKNOWN_PERMISSION,
                f"permission {permission} is not defined in v1",
            )
        if permission in FORBIDDEN_PERMISSIONS:
            raise PluginManifestError(
                PluginManifestError.FORBIDDEN_PERMISSION,
                f"permission {permission} is never granted to plugins",
            )
    return items


# {
#   責務: [_stage: stage IDを検査する]
#   処理: [公式stage IDか`ext.`付き拡張stageか確認する]
#   引数: [value: 検査値, name: field名]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): どちらでもない場合]
# }
def _stage(value: Any, name: str) -> str:
    if not isinstance(value, str) or not (
        value in CORE_STAGE_IDS or _EXTENSION_STAGE_PATTERN.fullmatch(value)
    ):
        _malformed(f"{name} must be a core stage id or an ext. stage id")
    return value


# {
#   責務: [_boundary: stage境界を検査する]
#   処理: [input/outputのどちらかか確認する]
#   引数: [value: 検査値, name: field名]
#   戻り値: [str: 同じ値]
#   エラー: [PluginManifestError(malformed): どちらでもない場合]
# }
def _boundary(value: Any, name: str) -> str:
    if not isinstance(value, str) or value not in STAGE_BOUNDARIES:
        _malformed(f"{name} must be input or output")
    return value


# {
#   責務: [_payload_grants: Hookの入力payload契約の配列を検査する]
#   処理: [1-16件、各要素のfield・ID・版を確認し、同じpayload契約IDの重複を拒否する]
#   引数: [value: input_payload_schemas field]
#   戻り値: [tuple[PayloadSchemaGrant, ...]: 文書順の許可]
#   エラー: [PluginManifestError: いずれかの違反]
# }
def _payload_grants(value: Any) -> tuple[PayloadSchemaGrant, ...]:
    items = _array(
        value, "input_payload_schemas", 1, PLUGIN_MANIFEST_MAX_PAYLOAD_SCHEMAS
    )
    grants: list[PayloadSchemaGrant] = []
    for item in items:
        entry = _object(item, "input_payload_schemas item")
        _require_exact_fields(
            entry, _PAYLOAD_GRANT_FIELDS, "input_payload_schemas item"
        )
        grants.append(
            PayloadSchemaGrant(
                _namespaced_id(entry["payload_schema"], "payload_schema"),
                _versions(entry["versions"], "versions"),
            )
        )
    if len({grant.payload_schema for grant in grants}) != len(grants):
        _malformed("input_payload_schemas must not repeat a payload_schema")
    return tuple(grants)


# {
#   責務: [_inject_targets: injectの出力先の配列を検査する]
#   処理: [
#     1: injectなら1-16件、それ以外は0件を要求する
#     2: 各要素のfield・stage・境界・payload契約・版を確認し、Safety/Execution stageをforbidden_permissionで拒否する
#     3: 同じ(stage, boundary, payload_schema)の重複を拒否する
#   ]
#   引数: [value: inject_targets field, mode: Hookのmode]
#   戻り値: [tuple[InjectTarget, ...]: 文書順の出力先]
#   エラー: [PluginManifestError: いずれかの違反]
# }
def _inject_targets(value: Any, mode: HookMode) -> tuple[InjectTarget, ...]:
    minimum = 1 if mode is HookMode.INJECT else 0
    items = _array(value, "inject_targets", 0, PLUGIN_MANIFEST_MAX_INJECT_TARGETS)
    if mode is not HookMode.INJECT and items:
        _malformed("only inject hooks may declare inject_targets")
    if len(items) < minimum:
        _malformed("inject hooks must declare at least one inject target")
    targets: list[InjectTarget] = []
    for item in items:
        entry = _object(item, "inject_targets item")
        _require_exact_fields(entry, _INJECT_TARGET_FIELDS, "inject_targets item")
        stage = _stage(entry["stage"], "inject target stage")
        boundary = _boundary(entry["boundary"], "inject target boundary")
        payload_schema = _namespaced_id(entry["payload_schema"], "payload_schema")
        versions = _versions(entry["versions"], "versions")
        if stage in PLUGIN_READ_ONLY_STAGES:
            raise PluginManifestError(
                PluginManifestError.FORBIDDEN_PERMISSION,
                f"plugins must not create {stage} envelopes",
            )
        targets.append(InjectTarget(stage, boundary, payload_schema, versions))
    keys = {(item.stage, item.boundary, item.payload_schema) for item in targets}
    if len(keys) != len(targets):
        _malformed(
            "inject_targets must not repeat a stage, boundary and payload_schema"
        )
    return tuple(targets)


# {
#   責務: [_budget: Hookの予算上限を検査する]
#   処理: [fieldの過不足と、HookRegistrationと同じ範囲を確認する。inject以外のmax_injectedは0だけ]
#   引数: [value: budget field, mode: Hookのmode]
#   戻り値: [HookBudget: 検証済みの上限]
#   エラー: [PluginManifestError(malformed): 型・範囲の違反]
# }
def _budget(value: Any, mode: HookMode) -> HookBudget:
    entry = _object(value, "budget")
    _require_exact_fields(entry, _BUDGET_FIELDS, "budget")
    maximum_injected = HOOK_MAX_INJECTED_PER_HOOK if mode is HookMode.INJECT else 0
    return HookBudget(
        _int_in_range(entry["timeout_ns"], "timeout_ns", 1, HOOK_MAX_TIMEOUT_NS),
        _int_in_range(
            entry["max_output_bytes"],
            "max_output_bytes",
            1,
            STAGE_ENVELOPE_MAX_ENCODED_BYTES,
        ),
        _int_in_range(entry["max_injected"], "max_injected", 0, maximum_injected),
    )


# {
#   責務: [_data_classes: 扱うdata classの宣言を検査する]
#   処理: [0-32件の配列で、各要素が名前空間付きIDで重複しないか確認する]
#   引数: [value: data_classes field]
#   戻り値: [tuple[str, ...]: 昇順のdata class]
#   エラー: [PluginManifestError: 型・件数・字面・重複の違反]
# }
def _data_classes(value: Any) -> tuple[str, ...]:
    items = _array(value, "data_classes", 0, PLUGIN_MANIFEST_MAX_DATA_CLASSES)
    classes = [_namespaced_id(item, "data class") for item in items]
    if len(set(classes)) != len(classes):
        _malformed("data_classes must not contain duplicates")
    return tuple(sorted(classes))


# {
#   責務: [_hooks: Hook宣言の配列を検査する]
#   処理: [
#     1: 1-32件の配列か確認する
#     2: 各Hookのfield、hook_id（plugin_id自身か`<plugin_id>.`接頭辞）、mode、接続点、Safety/Execution stageでのread以外の禁止、
#        入力payload契約、inject出力先、予算、data classを順に確認する
#     3: hook_idの重複を拒否する
#   ]
#   引数: [value: hooks field, plugin_id: 検証済みのplugin_id]
#   戻り値: [list[HookCapability]: 文書順のHook宣言]
#   エラー: [PluginManifestError: いずれかの違反]
# }
def _hooks(value: Any, plugin_id: str) -> list[HookCapability]:
    items = _array(value, "hooks", 1, PLUGIN_MANIFEST_MAX_HOOKS)
    hooks: list[HookCapability] = []
    for item in items:
        entry = _object(item, "hooks item")
        _require_exact_fields(entry, _HOOK_FIELDS, "hooks item")
        hook_id = _namespaced_id(entry["hook_id"], "hook_id")
        if hook_id != plugin_id and not hook_id.startswith(f"{plugin_id}."):
            _malformed("hook_id must be inside the plugin_id namespace")
        mode_text = entry["mode"]
        if not isinstance(mode_text, str) or mode_text not in {
            mode.value for mode in HookMode
        }:
            _malformed("mode must be read, modify or inject")
        mode = HookMode(mode_text)
        stage = _stage(entry["stage"], "stage")
        boundary = _boundary(entry["boundary"], "boundary")
        if stage in PLUGIN_READ_ONLY_STAGES and mode is not HookMode.READ:
            raise PluginManifestError(
                PluginManifestError.FORBIDDEN_PERMISSION,
                f"{stage} boundaries accept read hooks only",
            )
        hooks.append(
            HookCapability(
                hook_id=hook_id,
                mode=mode,
                stage=stage,
                boundary=boundary,
                input_payload_schemas=_payload_grants(entry["input_payload_schemas"]),
                inject_targets=_inject_targets(entry["inject_targets"], mode),
                budget=_budget(entry["budget"], mode),
                data_classes=_data_classes(entry["data_classes"]),
            )
        )
    if len({hook.hook_id for hook in hooks}) != len(hooks):
        _malformed("hook_id must be unique within a manifest")
    return hooks
