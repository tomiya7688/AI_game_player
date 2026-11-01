"""検証済みの数値Knowledgeを不変のversion付きデータとして保持する。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from typing import Any, cast

from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier


KNOWLEDGE_SCHEMA_VERSION = 1
MAX_KNOWLEDGE_BYTES = 1024 * 1024
MAX_ENTRIES = 4096
MAX_SOURCES = 1024
MAX_COUNT = 1_000_000_000_000
SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "version",
        "scope",
        "created_at",
        "expires_at",
        "sample_count",
        "uncertainty",
        "action_outcome_stats",
        "candidate_priors",
        "candidate_reliability",
        "transition_priors",
        "weight_hints",
        "threshold_hints",
        "roi_hints",
        "source_experience_range",
        "update_rule",
        "update_rule_version",
        "builder_state",
    }
)
STATS_FIELDS = frozenset(
    {
        "count",
        "success",
        "no_effect",
        "failure",
        "unknown",
        "reward_mean",
        "reward_ewma",
        "alpha",
        "beta",
    }
)
WEIGHT_HINT_KEYS = frozenset(
    {
        "utility",
        "recognition",
        "knowledge_prior",
        "progress",
        "survival",
        "resources",
        "risk",
        "repetition",
    }
)
THRESHOLD_HINT_KEYS = frozenset({"recognition_minimum", "decision_margin"})


# {
#   責務: [KnowledgeSnapshot: 検証済み数値知識とその出所を、変更不能な正規化JSONとして保持する]
#   フィールド: [_canonical_json: 別名を残さないUTF-8 canonical bytes,
#     scope/version/snapshot_id: 隔離範囲と版の識別,
#     sample_count/uncertainty: 学習証拠件数と残る不確かさ,
#     created_at/expires_at: UTC作成時刻と任意の利用期限]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    _canonical_json: bytes = field(repr=False)
    scope: KnowledgeScope = field(init=False)
    version: str = field(init=False)
    snapshot_id: str = field(init=False)
    sample_count: int = field(init=False)
    uncertainty: float = field(init=False)
    created_at: str = field(init=False)
    expires_at: str | None = field(init=False)

    # {
    #   責務: [__post_init__: 読み込んだKnowledgeを契約と照合し、可変入力から独立させる]
    #   処理: [容量、重複キー、schema、scope、数値・件数・出所の整合性を検証して正規化する]
    #   引数: [self: 検証前のJSON bytesを持つ構築中インスタンス]
    #   戻り値: [なし]
    #   エラー: [ValueError: schema不一致、破損、不正な値、容量超過]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self._canonical_json, bytes)
            or len(self._canonical_json) > MAX_KNOWLEDGE_BYTES
        ):
            raise ValueError("knowledge snapshot exceeds its byte contract")
        try:
            value = json.loads(self._canonical_json, object_pairs_hook=_unique_object)
        except RecursionError as error:
            raise ValueError("knowledge JSON nesting exceeds contract") from error
        _validate_snapshot(value)
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        object.__setattr__(self, "_canonical_json", canonical)
        object.__setattr__(self, "scope", KnowledgeScope.from_dict(value["scope"]))
        for name in (
            "version",
            "snapshot_id",
            "sample_count",
            "uncertainty",
            "created_at",
            "expires_at",
        ):
            object.__setattr__(self, name, value[name])

    # {
    #   責務: [from_dict: 数値データと不透明な参照だけを含むKnowledgeを構築する]
    #   処理: [JSON化し通常コンストラクターで全契約を検証する]
    #   引数: [value: schema v1のKnowledge辞書。渡した辞書の変更はSnapshotへ影響しない]
    #   戻り値: [KnowledgeSnapshot: 不変の検証済みSnapshot]
    #   エラー: [ValueError/TypeError: JSON化できない値、非有限数、不正な契約]
    # }
    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "KnowledgeSnapshot":
        try:
            payload = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except RecursionError as error:
            raise ValueError("knowledge JSON nesting exceeds contract") from error
        return cls(payload)

    # {
    #   責務: [to_dict: BuilderやAdapterへ独立したデータコピーを渡す]
    #   処理: [保持する正規化bytesをJSON辞書へ復元する]
    #   引数: [self: 検証済みSnapshot]
    #   戻り値: [dict: Snapshot本体を変更しない独立コピー]
    # }
    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._canonical_json))

    # {
    #   責務: [to_bytes: Artifact保存と内容比較に使うcanonical bytesを返す]
    #   処理: [不変のUTF-8 JSONを返す]
    #   引数: [self: 検証済みSnapshot]
    #   戻り値: [bytes: SHA-256計算対象の正規化表現]
    # }
    def to_bytes(self) -> bytes:
        return self._canonical_json

    # {
    #   責務: [sha256: Snapshotの内容を評価や昇格の対象へ結び付ける]
    #   処理: [保存するcanonical bytesのSHA-256を計算する]
    #   引数: [self: 検証済みSnapshot]
    #   戻り値: [str: ArtifactReferenceと照合できる内容digest]
    # }
    @property
    def sha256(self) -> str:
        return sha256(self._canonical_json).hexdigest()

    # {
    #   責務: [is_expired: retention期限を過ぎたKnowledgeの利用を止める]
    #   処理: [UTCの比較時刻とexpires_atを比較し、期限なしの場合は未失効とする]
    #   引数: [now: timezone付きUTC比較時刻。Noneは現在UTC]
    #   戻り値: [bool: 期限ちょうどまたは期限後ならTrue]
    #   エラー: [ValueError: 比較時刻がtimezone無しまたはUTC以外]
    # }
    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() != timezone.utc.utcoffset(
            current
        ):
            raise ValueError("knowledge read time must be UTC-aware")
        return self.expires_at is not None and current >= _timestamp(self.expires_at)


# {
#   責務: [_unique_object: JSONの同名キーによる契約のすり替えを拒否する]
#   処理: [出現済みキーを検出したら読込を停止する]
#   引数: [pairs: JSON decoderからのキー値ペア]
#   戻り値: [dict: 重複しないJSON object]
#   エラー: [ValueError: 重複キーがある]
# }
def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate knowledge field")
        result[key] = value
    return result


# {
#   責務: [_fields: 固定構造への余分な自由文・raw frame・secret fieldの混入を拒否する]
#   処理: [辞書のキー集合を契約と完全照合する]
#   引数: [value: 対象object, expected: 固定フィールド集合]
#   戻り値: [なし]
#   エラー: [ValueError: object以外、不足または余分なキー]
# }
def _fields(value: Any, expected: set[str] | frozenset[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("knowledge fields do not match contract")


# {
#   責務: [_count: 有界の非負整数件数だけを受理する]
#   処理: [boolやfloatを整数として扱わず、指定上限を確認する]
#   引数: [value: 件数またはsequence, maximum: 許可する上限]
#   戻り値: [int: 検証済み件数]
#   エラー: [ValueError: 型・範囲が不正]
# }
def _count(value: Any, maximum: int = MAX_COUNT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("knowledge count is outside contract")
    return value


# {
#   責務: [_number: 有界の有限な数値だけを受理する]
#   処理: [bool・非数値・NaN・Infinity・範囲外を拒否する]
#   引数: [value: 数値, lower/upper: 許可範囲の両端]
#   戻り値: [float: 検証済みの値]
#   エラー: [ValueError: 型・有限性・範囲が不正]
# }
def _number(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    if (
        type(value) not in (float, int)
        or not lower <= value <= upper
        or not isfinite(value)
    ):
        raise ValueError("knowledge number is outside contract")
    return float(value)


# {
#   責務: [_timestamp: 長さを制限したUTC時刻だけを受理する]
#   処理: [ISO形式を解析し、UTC offsetとtimezoneの存在を確認する]
#   引数: [value: UTC作成時刻または期限]
#   戻り値: [datetime: 比較に使うtimezone付きUTC時刻]
#   エラー: [ValueError: 時刻形式またはUTC条件が不正]
# }
def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise ValueError("knowledge timestamp must be UTC")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("knowledge timestamp must be UTC") from None
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise ValueError("knowledge timestamp must be UTC")
    return result


# {
#   責務: [_id_map: 不透明な識別子をキーとする有界の数値表を検証する]
#   処理: [項目数と全キーを検証して参照用dictを返す]
#   引数: [value: 候補・統計・hint等の表, maximum: 許可項目数]
#   戻り値: [dict: 検証対象の表]
#   エラー: [ValueError: 表・キー・容量が不正]
# }
def _id_map(value: Any, maximum: int = MAX_ENTRIES) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > maximum:
        raise ValueError("knowledge table exceeds contract")
    for key in value:
        validate_identifier(key, "entry_id")
    return value


# {
#   責務: [_validate_snapshot: Snapshot全体のschemaと件数・出所・確率の整合性を検査する]
#   処理: [scopeと時刻、固定数値表、各参照範囲を検証し、Safety設定やraw contentの混入を拒否する]
#   引数: [value: JSONから復元したSnapshot全体]
#   戻り値: [なし]
#   エラー: [ValueError: schemaまたは相互関係が不正]
# }
def _validate_snapshot(value: Any) -> None:
    _fields(value, SNAPSHOT_FIELDS)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != KNOWLEDGE_SCHEMA_VERSION
    ):
        raise ValueError("unsupported knowledge schema version")
    KnowledgeScope.from_dict(value["scope"])
    for name in ("snapshot_id", "version", "update_rule", "update_rule_version"):
        validate_identifier(value[name], name)
    created = _timestamp(value["created_at"])
    if value["expires_at"] is not None and _timestamp(value["expires_at"]) <= created:
        raise ValueError("knowledge expiry must follow creation")
    sample_count = _count(value["sample_count"])
    _number(value["uncertainty"])
    _validate_statistics(value["action_outcome_stats"], sample_count)
    for prior in _id_map(value["candidate_priors"]).values():
        _number(prior, -1, 1)
    for reliability in _id_map(value["candidate_reliability"]).values():
        _number(reliability)
    for name, allowed in (
        ("weight_hints", WEIGHT_HINT_KEYS),
        ("threshold_hints", THRESHOLD_HINT_KEYS),
    ):
        table = _id_map(value[name])
        if set(table) - allowed:
            raise ValueError("unknown knowledge hint")
        for hint in table.values():
            _number(hint, 0, 10 if name == "weight_hints" else 1)
    for roi in _id_map(value["roi_hints"]).values():
        if not isinstance(roi, list) or len(roi) != 4:
            raise ValueError("knowledge ROI must have normalized xywh")
        x, y, width, height = (_number(part) for part in roi)
        if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("knowledge ROI lies outside normalized frame")
    _validate_transitions(value["transition_priors"], sample_count)
    _validate_sources(value["source_experience_range"], sample_count)
    _fields(value["builder_state"], {"source_watermarks", "correlation_watermarks"})
    for table in value["builder_state"].values():
        for sequence in _id_map(table, MAX_SOURCES).values():
            _count(sequence)


# {
#   責務: [_validate_statistics: 行動成果の分類と数値推定を相互照合する]
#   処理: [unknownをsuccessやno-effectと合算せず分類合計と証拠上限を検査する]
#   引数: [table: 操作ID別統計, sample_count: 元Experienceの総件数]
#   戻り値: [なし]
#   エラー: [ValueError: 件数・平均・Betaパラメーターが不正]
# }
def _validate_statistics(table: Any, sample_count: int) -> None:
    total = 0
    for stats in _id_map(table).values():
        _fields(stats, STATS_FIELDS)
        count = _count(stats["count"])
        if (
            sum(
                _count(stats[key])
                for key in ("success", "no_effect", "failure", "unknown")
            )
            != count
        ):
            raise ValueError("knowledge outcome counts do not add up")
        total += count
        for name in ("reward_mean", "reward_ewma"):
            _number(stats[name], -1, 1)
        for name in ("alpha", "beta"):
            if _number(stats[name], 0, MAX_COUNT + 1) <= 0:
                raise ValueError("knowledge Beta parameters must be positive")
    if total > sample_count:
        raise ValueError("knowledge action counts exceed evidence count")


# {
#   責務: [_validate_transitions: 条件付き遷移表の重複と確率質量を検査する]
#   処理: [同じfrom/action/toの重複を拒否し、from/action別の質量が1を超えないことを確認する]
#   引数: [rows: 遷移の配列, sample_count: 元Experienceの総件数]
#   戻り値: [なし]
#   エラー: [ValueError: 行構造・確率・件数・重複が不正]
# }
def _validate_transitions(rows: Any, sample_count: int) -> None:
    if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
        raise ValueError("knowledge transition table exceeds contract")
    seen = set()
    masses: dict[tuple[str, ...], float] = {}
    for row in rows:
        _fields(row, {"from_state", "action_id", "to_state", "probability", "count"})
        key = tuple(
            validate_identifier(row[name], name)
            for name in ("from_state", "action_id", "to_state")
        )
        if key in seen or _count(row["count"]) > sample_count:
            raise ValueError("duplicate or unsupported knowledge transition")
        seen.add(key)
        masses[key[:2]] = masses.get(key[:2], 0.0) + _number(row["probability"])
    if any(mass > 1 + 1e-12 for mass in masses.values()):
        raise ValueError("knowledge transition probability exceeds one")


# {
#   責務: [_validate_sources: 元Experience範囲の重複・件数・digestを検証する]
#   処理: [同じsourceの区間重複を拒否し、元件数の合計とsample_countを照合する]
#   引数: [rows: opaque source範囲配列, sample_count: Snapshot全体の証拠件数]
#   戻り値: [なし]
#   エラー: [ValueError: 範囲・SHA・件数が不正または重複]
# }
def _validate_sources(rows: Any, sample_count: int) -> None:
    if not isinstance(rows, list) or len(rows) > MAX_SOURCES:
        raise ValueError("knowledge source ranges exceed contract")
    intervals: dict[str, list[tuple[int, int]]] = {}
    total = 0
    for row in rows:
        _fields(
            row,
            {"source_id", "first_sequence", "last_sequence", "event_count", "sha256"},
        )
        source_id = validate_identifier(row["source_id"], "source_id")
        first, last, count = (
            _count(row[name])
            for name in ("first_sequence", "last_sequence", "event_count")
        )
        if first > last or count > last - first + 1 or count == 0:
            raise ValueError("knowledge source range is inconsistent")
        digest = row["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("knowledge source digest is invalid")
        intervals.setdefault(source_id, []).append((first, last))
        total += count
    for ranges in intervals.values():
        ordered = sorted(ranges)
        if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("knowledge source ranges overlap")
    if total != sample_count:
        raise ValueError("knowledge source count does not match sample_count")
