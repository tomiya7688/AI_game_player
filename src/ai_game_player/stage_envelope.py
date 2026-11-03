from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from ai_game_player.experience import ArtifactReference


STAGE_ENVELOPE_SCHEMA_VERSION = 1
# 文書全体のUTF-8 canonical JSON byte数上限。大きな画像・モデル出力はArtifact Storeへ置きArtifactReferenceで参照する。
STAGE_ENVELOPE_MAX_ENCODED_BYTES = 1_048_576
# Envelope root objectを深さ1とする入れ子の上限。payload objectは深さ2から始まる。
STAGE_ENVELOPE_MAX_DEPTH = 32
STAGE_ENVELOPE_MAX_DERIVED_FROM = 16
STAGE_ENVELOPE_MAX_ARTIFACT_REFS = 64
STAGE_ENVELOPE_INTEGER_MIN = -9_223_372_036_854_775_808
STAGE_ENVELOPE_INTEGER_MAX = 9_223_372_036_854_775_807
# v1の公式stage ID。Plugin独自stageは`ext.`接頭辞の名前空間だけで表す。
CORE_STAGE_IDS = frozenset(
    {
        "capture",
        "perception",
        "state",
        "reasoning",
        "decision",
        "safety",
        "execution",
        "outcome",
        "evaluation",
    }
)
STAGE_BOUNDARIES = frozenset({"input", "output"})
PRODUCER_KINDS = frozenset({"core", "hook", "provider", "replay"})
STAGE_ENVELOPE_FIELDS = (
    "schema_version",
    "envelope_id",
    "stage",
    "boundary",
    "session_id",
    "frame_id",
    "turn_id",
    "timestamp_utc",
    "monotonic_ns",
    "trace_id",
    "correlation_id",
    "parent_envelope_id",
    "producer_kind",
    "producer_id",
    "producer_version",
    "derived_from",
    "payload_schema",
    "payload_schema_version",
    "payload",
    "confidence",
    "metadata",
    "artifact_refs",
)
# {
#   責務: [_OPAQUE_ID_PATTERN: session・trace・envelope等の外部生成IDとして受け付ける文字列を制限する]
#   処理: [英数字で始まり英数字と`_ . : -`だけを含む1-128文字に一致させ、空白・制御文字・区切り記号を除外する]
# }
_OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# {
#   責務: [_NAMESPACED_ID_PATTERN: stage・producer・payload schema・metadata keyに使う名前空間付き小文字IDを制限する]
#   処理: [小文字英字で始まり小文字英数字と`_ . -`だけを含む1-128文字に一致させる]
# }
_NAMESPACED_ID_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_EXTENSION_STAGE_PREFIX = "ext."
# {
#   責務: [_VERSION_TEXT_PATTERN: producerの版表記として受け付ける短い文字列を制限する]
#   処理: [英数字で始まり英数字と`. + _ -`だけを含む1-64文字に一致させる]
# }
_VERSION_TEXT_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
# {
#   責務: [_UTC_TIMESTAMP_PATTERN: v1で受け付けるUTC時刻の字面を年月日時分秒・任意の1-9桁小数・末尾Zに制限する]
#   処理: [各数値部分をgroupで取り出し、暦として正しいかは_validate_utc_timestampで確認する]
# }
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?Z"
)


# {
#   責務: [StageEnvelopeError: StageEnvelopeの検証・復号が失敗した理由を機械判定できるcodeと一緒に呼び出し元へ伝える]
#   フィールド: [code: `malformed`（形・型・値の違反）、`unsupported_version`（未知または移行不能な版）、`limit_exceeded`（byte数・深さ・件数上限超過）のいずれか]
# }
class StageEnvelopeError(ValueError):
    MALFORMED = "malformed"
    UNSUPPORTED_VERSION = "unsupported_version"
    LIMIT_EXCEEDED = "limit_exceeded"

    # {
    #   責務: [__init__: 失敗分類codeと人が読める理由を保持する例外を作る]
    #   処理: [codeを属性へ保存し、理由文の先頭にcodeを付けてValueErrorへ渡す]
    #   引数: [code: 失敗分類（MALFORMED・UNSUPPORTED_VERSION・LIMIT_EXCEEDEDのいずれか）, message: 違反したfieldと条件の説明]
    #   戻り値: [なし]
    # }
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
# {
#   責務: [StageEnvelope: Pipeline stage境界の中間dataを、Core内部classに依存しない版付き・言語中立なv1契約として保持する]
#   フィールド: [
#     schema_version: Envelope契約の版（v1では1だけ）
#     envelope_id/stage/boundary: このEnvelopeの識別子、公式stage IDまたは`ext.`付き拡張stage、stageへの入力側か出力側か
#     session_id/frame_id/turn_id: 所属Sessionと任意のframe・turn識別子
#     timestamp_utc/monotonic_ns: 作成時のUTC時刻と任意の同一process内単調時刻（ns）
#     trace_id/correlation_id/parent_envelope_id: 一連の判断を結ぶtrace、任意の相関ID、直接の元Envelope
#     producer_kind/producer_id/producer_version/derived_from: 作成者の種類・ID・版と、入力に使った元Envelope ID一覧（provenance）
#     payload_schema/payload_schema_version/payload: payload自身の契約IDと版、不変化したJSON object
#     confidence: 0以上1以下の確信度。不明ならNone
#     metadata: Coreが解釈せず保存だけする拡張用JSON object。keyは名前空間付きID
#     artifact_refs: Artifact Storeに置いた大きなdataへの参照
#   ]
#   補足: [変更版は`dataclasses.replace()`で作り直し、__post_init__で再検証される。payload/metadataは読み取り専用に凍結される]
# }
class StageEnvelope:
    envelope_id: str
    stage: str
    boundary: str
    session_id: str
    timestamp_utc: str
    trace_id: str
    producer_kind: str
    producer_id: str
    producer_version: str
    payload_schema: str
    payload_schema_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    frame_id: str | None = None
    turn_id: str | None = None
    monotonic_ns: int | None = None
    correlation_id: str | None = None
    parent_envelope_id: str | None = None
    derived_from: tuple[str, ...] = ()
    confidence: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactReference, ...] = ()
    schema_version: int = STAGE_ENVELOPE_SCHEMA_VERSION

    # {
    #   責務: [__post_init__: 作成・置換直後のEnvelopeがv1契約を満たすか検証し、可変な入れ子値を不変化する]
    #   処理: [
    #     1: schema_versionが対応版か確認し、識別・trace・provenanceの各fieldの字面と相互関係を検証する
    #     2: 時刻・単調時刻・confidenceの値域を確認する
    #     3: payload/metadataをJSON値として深さ・数値範囲を検証して凍結し、配列fieldをtupleへ正規化する
    #   ]
    #   引数: [self: 検証対象のEnvelope]
    #   戻り値: [なし: 成功時は正規化した値を保持する]
    #   エラー: [StageEnvelopeError: 版が未対応ならunsupported_version、上限超過ならlimit_exceeded、それ以外の違反はmalformed]
    # }
    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        self._validate_identity()
        self._validate_trace_and_provenance()
        _validate_utc_timestamp(self.timestamp_utc)
        if self.monotonic_ns is not None:
            _require_integer_in_range(
                self.monotonic_ns, "monotonic_ns", 0, STAGE_ENVELOPE_INTEGER_MAX
            )
        _validate_confidence(self.confidence)
        _require_integer_in_range(
            self.payload_schema_version,
            "payload_schema_version",
            1,
            STAGE_ENVELOPE_INTEGER_MAX,
        )
        object.__setattr__(
            self, "payload", _freeze_root_object(self.payload, "payload")
        )
        _validate_metadata_keys(self.metadata)
        object.__setattr__(
            self, "metadata", _freeze_root_object(self.metadata, "metadata")
        )
        object.__setattr__(
            self, "artifact_refs", _normalize_artifact_refs(self.artifact_refs)
        )

    # {
    #   責務: [_validate_identity: Envelopeの所在を表すID・stage・boundary・session/frame/turnの字面を検証する]
    #   処理: [不透明IDの形式、公式または`ext.`付きstage ID、boundaryの列挙値を確認する]
    #   引数: [self: 検証対象のEnvelope]
    #   戻り値: [なし]
    #   エラー: [StageEnvelopeError(malformed): いずれかのfieldが形式外の場合]
    # }
    def _validate_identity(self) -> None:
        _require_opaque_id(self.envelope_id, "envelope_id")
        _require_opaque_id(self.session_id, "session_id")
        for name in ("frame_id", "turn_id"):
            _require_optional_opaque_id(getattr(self, name), name)
        _validate_stage_id(self.stage)
        _require_member(self.boundary, "boundary", STAGE_BOUNDARIES)

    # {
    #   責務: [_validate_trace_and_provenance: trace・相関・親子関係とproducer情報が一貫しているか検証する]
    #   処理: [
    #     1: trace/correlation/parent IDの形式を確認し、parentが自分自身を指さないことを確かめる
    #     2: producerの種類・ID・版の形式を確認する
    #     3: derived_fromを上限件数以内・重複なし・自己参照なしのtupleへ正規化する
    #   ]
    #   引数: [self: 検証対象のEnvelope]
    #   戻り値: [なし: derived_fromをtupleとして保持する]
    #   エラー: [StageEnvelopeError: 件数上限超過はlimit_exceeded、それ以外はmalformed]
    # }
    def _validate_trace_and_provenance(self) -> None:
        _require_opaque_id(self.trace_id, "trace_id")
        _require_optional_opaque_id(self.correlation_id, "correlation_id")
        _require_optional_opaque_id(self.parent_envelope_id, "parent_envelope_id")
        if self.parent_envelope_id == self.envelope_id:
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED,
                "parent_envelope_id must not reference itself",
            )
        _require_member(self.producer_kind, "producer_kind", PRODUCER_KINDS)
        _require_namespaced_id(self.producer_id, "producer_id")
        _require_pattern(
            self.producer_version, "producer_version", _VERSION_TEXT_PATTERN
        )
        _require_namespaced_id(self.payload_schema, "payload_schema")
        object.__setattr__(
            self,
            "derived_from",
            _normalize_derived_from(self.derived_from, self.envelope_id),
        )

    # {
    #   責務: [to_dict: EnvelopeをJSON encoderや他言語へ渡せる素のdict・list・scalarだけの記録へ変換する]
    #   処理: [凍結したpayload/metadataを独立したdict/listへ戻し、全fieldを欠かさず含めるdictを作る]
    #   引数: [self: 変換する検証済みEnvelope]
    #   戻り値: [dict[str, Any]: STAGE_ENVELOPE_FIELDSを全て持つv1 JSON object。任意fieldの未設定はNone]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "envelope_id": self.envelope_id,
            "stage": self.stage,
            "boundary": self.boundary,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "turn_id": self.turn_id,
            "timestamp_utc": self.timestamp_utc,
            "monotonic_ns": self.monotonic_ns,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "parent_envelope_id": self.parent_envelope_id,
            "producer_kind": self.producer_kind,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "derived_from": list(self.derived_from),
            "payload_schema": self.payload_schema,
            "payload_schema_version": self.payload_schema_version,
            "payload": _thaw_json_value(self.payload),
            "confidence": self.confidence,
            "metadata": _thaw_json_value(self.metadata),
            "artifact_refs": [reference.to_dict() for reference in self.artifact_refs],
        }

    @classmethod
    # {
    #   責務: [from_dict: JSON由来のobjectを、未知field・欠落field・型違いを拒否して検証済みEnvelopeへ復元する]
    #   処理: [
    #     1: objectであること、版がv1であること、field集合がv1定義と完全一致することを確認する
    #     2: 配列fieldとartifact参照を型確認してから、各値を__post_init__の検証付きで組み立てる
    #   ]
    #   引数: [cls: 生成するEnvelope型, document: JSON parser等が返した1件のEnvelope object]
    #   戻り値: [StageEnvelope: v1契約に適合する不変Envelope]
    #   エラー: [StageEnvelopeError: 版違いはunsupported_version、上限超過はlimit_exceeded、それ以外はmalformed]
    #   補足: [古い版の移行はstage_envelope_codec.migrate_stage_envelope_documentで先に行う]
    # }
    def from_dict(cls, document: Mapping[str, Any]) -> StageEnvelope:
        if not isinstance(document, Mapping):
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED, "stage envelope must be an object"
            )
        _validate_schema_version(document.get("schema_version"))
        _require_exact_fields(document)
        derived_from = _require_array(document["derived_from"], "derived_from")
        artifact_documents = _require_array(document["artifact_refs"], "artifact_refs")
        if len(artifact_documents) > STAGE_ENVELOPE_MAX_ARTIFACT_REFS:
            raise StageEnvelopeError(
                StageEnvelopeError.LIMIT_EXCEEDED,
                "artifact_refs exceeds the item limit",
            )
        values = {name: document[name] for name in STAGE_ENVELOPE_FIELDS}
        values["derived_from"] = tuple(derived_from)
        values["artifact_refs"] = tuple(
            _artifact_reference_from_dict(item) for item in artifact_documents
        )
        return cls(**values)


# {
#   責務: [_validate_schema_version: 文書またはEnvelopeのschema_versionがこの実装の対応版か判定する]
#   処理: [boolを除く整数であることを確認し、対応版と異なる値を未対応版として拒否する]
#   引数: [value: schema_version fieldの値]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError: 整数でなければmalformed、対応版でなければunsupported_version]
# }
def _validate_schema_version(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "schema_version must be an integer"
        )
    if value != STAGE_ENVELOPE_SCHEMA_VERSION:
        raise StageEnvelopeError(
            StageEnvelopeError.UNSUPPORTED_VERSION,
            f"stage envelope schema_version {value} is not supported",
        )


# {
#   責務: [_require_exact_fields: 文書のfield集合がv1定義と過不足なく一致するか確認する]
#   処理: [欠落fieldと未知fieldをそれぞれ名前順に集め、どちらかがあれば理由に列挙して拒否する]
#   引数: [document: 版確認済みのEnvelope object]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 欠落または未知のfieldがある場合]
# }
def _require_exact_fields(document: Mapping[str, Any]) -> None:
    expected = set(STAGE_ENVELOPE_FIELDS)
    actual = set(document)
    missing = sorted(expected - actual)
    unknown = sorted(str(name) for name in actual - expected)
    if missing or unknown:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            f"stage envelope fields mismatch (missing={missing}, unknown={unknown})",
        )


# {
#   責務: [_require_array: JSON配列として届いたfield値をlistとして受け取る]
#   処理: [listまたはtupleだけを許可し、文字列等の反復可能値を配列と誤認しない]
#   引数: [value: 配列であるべきfield値, field_name: エラー表示に使うfield名]
#   戻り値: [list[Any]: 要素を保持した新しいlist]
#   エラー: [StageEnvelopeError(malformed): 配列でない場合]
# }
def _require_array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"{field_name} must be an array"
        )
    return list(value)


# {
#   責務: [_artifact_reference_from_dict: 1件のartifact参照objectを既存ArtifactReference契約で検証して復元する]
#   処理: [ArtifactReference.from_dictへ委譲し、その検証失敗をStageEnvelopeErrorのmalformedへ変換する]
#   引数: [value: artifact_refs配列の1要素]
#   戻り値: [ArtifactReference: 検証済み参照]
#   エラー: [StageEnvelopeError(malformed): objectでない、またはfieldが参照契約に違反する場合]
# }
def _artifact_reference_from_dict(value: Any) -> ArtifactReference:
    if not isinstance(value, Mapping):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "artifact_refs items must be objects"
        )
    try:
        return ArtifactReference.from_dict(value)
    except (ValueError, TypeError, KeyError) as error:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"invalid artifact reference: {error}"
        ) from error


# {
#   責務: [_normalize_artifact_refs: Envelopeが保持するartifact参照列を件数上限内のArtifactReference tupleにする]
#   処理: [配列であること、上限件数以内であること、全要素がArtifactReferenceであることを確認する]
#   引数: [value: artifact_refs fieldの値]
#   戻り値: [tuple[ArtifactReference, ...]: 入力順を保った不変列]
#   エラー: [StageEnvelopeError: 件数超過はlimit_exceeded、型違いはmalformed]
# }
def _normalize_artifact_refs(value: Any) -> tuple[ArtifactReference, ...]:
    references = _require_array(value, "artifact_refs")
    if len(references) > STAGE_ENVELOPE_MAX_ARTIFACT_REFS:
        raise StageEnvelopeError(
            StageEnvelopeError.LIMIT_EXCEEDED, "artifact_refs exceeds the item limit"
        )
    if any(not isinstance(reference, ArtifactReference) for reference in references):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            "artifact_refs must contain ArtifactReference values",
        )
    return tuple(references)


# {
#   責務: [_normalize_derived_from: provenanceの元Envelope ID列を検証し不変tupleにする]
#   処理: [配列・件数上限・各IDの形式・重複なし・自己参照なしを順に確認する]
#   引数: [value: derived_from fieldの値, envelope_id: 自己参照判定に使うこのEnvelopeのID]
#   戻り値: [tuple[str, ...]: 入力順を保ったID列]
#   エラー: [StageEnvelopeError: 件数超過はlimit_exceeded、それ以外はmalformed]
# }
def _normalize_derived_from(value: Any, envelope_id: str) -> tuple[str, ...]:
    identifiers = _require_array(value, "derived_from")
    if len(identifiers) > STAGE_ENVELOPE_MAX_DERIVED_FROM:
        raise StageEnvelopeError(
            StageEnvelopeError.LIMIT_EXCEEDED, "derived_from exceeds the item limit"
        )
    for identifier in identifiers:
        _require_opaque_id(identifier, "derived_from item")
    if len(set(identifiers)) != len(identifiers):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "derived_from items must be unique"
        )
    if envelope_id in identifiers:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            "derived_from must not reference the envelope itself",
        )
    return tuple(identifiers)


# {
#   責務: [_validate_stage_id: stage fieldが公式stage IDか、`ext.`名前空間の拡張stage IDかを判定する]
#   処理: [名前空間付きIDの字面を確認し、公式一覧にも`ext.`直後が小文字英字で始まる`ext.<name>`形式にも当たらない値を拒否する]
#   引数: [value: stage fieldの値]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 未定義stageまたは形式外の場合]
# }
def _validate_stage_id(value: Any) -> None:
    _require_namespaced_id(value, "stage")
    extension_name = (
        value[len(_EXTENSION_STAGE_PREFIX) :]
        if value.startswith(_EXTENSION_STAGE_PREFIX)
        else ""
    )
    is_extension = _NAMESPACED_ID_PATTERN.fullmatch(extension_name) is not None
    if value not in CORE_STAGE_IDS and not is_extension:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            f"stage {value!r} is not a v1 core or ext. stage",
        )


# {
#   責務: [_validate_metadata_keys: metadataの最上位keyが名前空間付きIDか確認し、拡張data同士の衝突を避ける]
#   処理: [objectであることを確認し、全keyを名前空間付きIDの規則で検証する]
#   引数: [value: metadata fieldの値]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): objectでない、またはkeyが形式外の場合]
# }
def _validate_metadata_keys(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "metadata must be an object"
        )
    for key in value:
        _require_namespaced_id(key, "metadata key")


# {
#   責務: [_validate_utc_timestamp: timestamp_utcがZ付きUTCの実在する日時か判定する]
#   処理: [字面を正規表現全体一致で確認し、年月日時分秒からdatetimeを作って暦上の妥当性を確かめる]
#   引数: [value: timestamp_utc fieldの値]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 文字列でない、形式外、または存在しない日時の場合]
# }
def _validate_utc_timestamp(value: Any) -> None:
    match = _UTC_TIMESTAMP_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            "timestamp_utc must be an RFC 3339 UTC time ending in Z",
        )
    try:
        datetime(*(int(part) for part in match.groups()))
    except ValueError as error:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, "timestamp_utc is not a calendar date-time"
        ) from error


# {
#   責務: [_validate_confidence: confidenceが未設定か、0以上1以下の有限数か判定する]
#   処理: [Noneを許可し、boolを除く数値が0以上1以下か比較で確認する（NaNは比較が偽になり、巨大な整数もOverflowErrorなしで拒否される）]
#   引数: [value: confidence fieldの値]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 数値でない、非有限、範囲外の場合]
# }
def _validate_confidence(value: Any) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            "confidence must be null or a number between 0 and 1",
        )


# {
#   責務: [_require_integer_in_range: field値がboolでない整数で指定範囲内か判定する]
#   処理: [型と下限・上限を確認する]
#   引数: [value: 判定する値, field_name: エラー表示名, minimum/maximum: 両端を含む許容範囲]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 整数でない、または範囲外の場合]
# }
def _require_integer_in_range(
    value: Any, field_name: str, minimum: int, maximum: int
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            f"{field_name} must be an integer between {minimum} and {maximum}",
        )


# {
#   責務: [_require_member: 列挙fieldの値が定義済み集合に含まれるか判定する]
#   処理: [文字列であり、許可集合のいずれかと一致することを確認する]
#   引数: [value: 判定する値, field_name: エラー表示名, allowed: v1で許可する値の集合]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 集合外の場合]
# }
def _require_member(value: Any, field_name: str, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED,
            f"{field_name} must be one of {sorted(allowed)}",
        )


# {
#   責務: [_require_pattern: 文字列fieldが指定の字面規則に全体一致するか判定する]
#   処理: [文字列であることを確認し、正規表現のfullmatchで部分一致や末尾改行を拒否する]
#   引数: [value: 判定する値, field_name: エラー表示名, pattern: 全体一致させる規則]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 文字列でない、または規則に一致しない場合]
# }
def _require_pattern(value: Any, field_name: str, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"{field_name} has an invalid format"
        )


# {
#   責務: [_require_opaque_id: 外部生成IDとして使う必須文字列fieldの字面を検証する]
#   処理: [不透明ID規則へ全体一致することを確認する]
#   引数: [value: 判定する値, field_name: エラー表示名]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 規則に一致しない場合]
# }
def _require_opaque_id(value: Any, field_name: str) -> None:
    _require_pattern(value, field_name, _OPAQUE_ID_PATTERN)


# {
#   責務: [_require_optional_opaque_id: 未設定を許す外部生成ID fieldの字面を検証する]
#   処理: [Noneはそのまま許可し、値がある場合だけ不透明ID規則で確認する]
#   引数: [value: 判定する値, field_name: エラー表示名]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 値があり規則に一致しない場合]
# }
def _require_optional_opaque_id(value: Any, field_name: str) -> None:
    if value is not None:
        _require_opaque_id(value, field_name)


# {
#   責務: [_require_namespaced_id: stage・producer・payload schema等の小文字名前空間IDを検証する]
#   処理: [名前空間付きID規則へ全体一致することを確認する]
#   引数: [value: 判定する値, field_name: エラー表示名]
#   戻り値: [なし]
#   エラー: [StageEnvelopeError(malformed): 規則に一致しない場合]
# }
def _require_namespaced_id(value: Any, field_name: str) -> None:
    _require_pattern(value, field_name, _NAMESPACED_ID_PATTERN)


# {
#   責務: [_freeze_root_object: payload・metadataのJSON objectを検証し、Hook等が元dataを書き換えられない不変値にする]
#   処理: [objectであることを確認し、Envelope rootを深さ1・このobjectを深さ2としてJSON値全体を検証・凍結する]
#   引数: [value: payloadまたはmetadataの値, field_name: エラー表示名]
#   戻り値: [Mapping[str, Any]: 入れ子のobjectをMappingProxyType、配列をtupleにした読み取り専用値]
#   エラー: [StageEnvelopeError: 深さ上限超過はlimit_exceeded、JSONでない値・非有限数・範囲外整数はmalformed]
# }
def _freeze_root_object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"{field_name} must be an object"
        )
    return _freeze_json_object(value, field_name, 2)


# {
#   責務: [_freeze_json_object: 1つのJSON objectを深さ・key型の規則で検証し、読み取り専用のmappingへ複製する]
#   処理: [深さ上限と文字列keyを確認し、各値を1段深い位置として_freeze_json_valueで凍結する]
#   引数: [value: 検証するobject, path: エラー表示用の位置, depth: Envelope rootを1としたこのobjectの深さ]
#   戻り値: [Mapping[str, Any]: 入力のkey順を保ったMappingProxyType]
#   エラー: [StageEnvelopeError: 深さ超過はlimit_exceeded、非文字列keyや不正な値はmalformed]
# }
def _freeze_json_object(
    value: Mapping[Any, Any], path: str, depth: int
) -> Mapping[str, Any]:
    if depth > STAGE_ENVELOPE_MAX_DEPTH:
        raise StageEnvelopeError(
            StageEnvelopeError.LIMIT_EXCEEDED, f"{path} exceeds the nesting depth limit"
        )
    if any(not isinstance(key, str) for key in value):
        raise StageEnvelopeError(
            StageEnvelopeError.MALFORMED, f"{path} object keys must be strings"
        )
    return MappingProxyType(
        {
            key: _freeze_json_value(item, f"{path}.{key}", depth + 1)
            for key, item in value.items()
        }
    )


# {
#   責務: [_freeze_json_value: 1つのJSON値を言語中立な型・範囲・深さ規則で検証しながら不変値へ複製する]
#   処理: [
#     1: 文字列・真偽値・null・int64範囲の整数・有限浮動小数をそのまま受け付ける
#     2: objectは文字列keyだけを許しMappingProxyTypeへ、配列はtupleへ再帰的に複製する
#     3: 深さはobject・配列だけを数え、container自身の深さが上限を超えたら拒否する（scalarは深さを増やさない）
#   ]
#   引数: [value: 検証するJSON値, path: エラー表示用の値の位置, depth: 値がcontainerだった場合の深さ（Envelope rootが1）]
#   戻り値: [Any: 検証済みの不変JSON値]
#   エラー: [StageEnvelopeError: 深さ超過はlimit_exceeded、それ以外の違反はmalformed]
# }
def _freeze_json_value(value: Any, path: str, depth: int) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not STAGE_ENVELOPE_INTEGER_MIN <= value <= STAGE_ENVELOPE_INTEGER_MAX:
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED,
                f"{path} integer is outside the int64 range",
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StageEnvelopeError(
                StageEnvelopeError.MALFORMED,
                f"{path} must not contain non-finite numbers",
            )
        return value
    if isinstance(value, Mapping):
        return _freeze_json_object(value, path, depth)
    if isinstance(value, (list, tuple)):
        if depth > STAGE_ENVELOPE_MAX_DEPTH:
            raise StageEnvelopeError(
                StageEnvelopeError.LIMIT_EXCEEDED,
                f"{path} exceeds the nesting depth limit",
            )
        return tuple(
            _freeze_json_value(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        )
    raise StageEnvelopeError(
        StageEnvelopeError.MALFORMED,
        f"{path} contains a non-JSON value of type {type(value).__name__}",
    )


# {
#   責務: [_thaw_json_value: 凍結済みJSON値を呼び出し元が自由に扱える独立したdict・listへ戻す]
#   処理: [Mappingをdictへ、tupleをlistへ再帰的に複製し、scalarはそのまま返す]
#   引数: [value: _freeze_json_valueが返した値]
#   戻り値: [Any: 元のEnvelopeと共有しない素のJSON値]
# }
def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value
