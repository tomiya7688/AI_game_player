"""Retention Coreの容量予算・hard limit・TTLを表す検証済みpolicy。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ai_game_player.retention_index import MAX_RETENTION_INDEX_ENTRIES

RETENTION_POLICY_SCHEMA_VERSION = 1
_ARTIFACT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "soft_budget_bytes",
        "hard_limit_bytes",
        "default_ttl_ms",
        "ttl_ms_by_type",
        "max_entries",
    }
)


# {
#   責務: [RetentionPolicyError: retention policyが契約外で、GCや受入判断に使えないことを通知する]
#   フィールド: [なし: ValueErrorの理由文だけを持つ]
# }
class RetentionPolicyError(ValueError):
    """The retention policy violates its contract."""


# {
#   責務: [RetentionPolicy: Experience Artifactの保持容量とTTLの上限を1つの不変値として固定する]
#   フィールド: [soft_budget_bytes: GCを開始し、GC後に目標とする使用byte上限,
#     hard_limit_bytes: これを超える新規書込みを拒否する絶対上限（soft以上）,
#     default_ttl_ms: 型別指定がないArtifactのTTL（ms）。Noneは無期限,
#     ttl_ms_by_type: artifact_typeごとのTTL（ms）。Noneを値に持つ型は無期限,
#     max_entries: 索引に保持できる記録数の上限]
#   処理: [expiry_forで登録時刻からTTL期限を決める]
# }
@dataclass(frozen=True)
class RetentionPolicy:
    soft_budget_bytes: int
    hard_limit_bytes: int
    default_ttl_ms: int | None = None
    ttl_ms_by_type: Mapping[str, int | None] = field(default_factory=dict)
    max_entries: int = MAX_RETENTION_INDEX_ENTRIES

    # {
    #   責務: [__post_init__: 矛盾する容量・TTL・件数上限を持つpolicyを生成時点で拒否する]
    #   処理: [正整数とsoft<=hardを確認し、TTLの型と範囲を検査して型別TTLを読取専用に固定する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [RetentionPolicyError: いずれかの値が契約外]
    # }
    def __post_init__(self) -> None:
        _require_positive_int(self.soft_budget_bytes, "soft_budget_bytes")
        _require_positive_int(self.hard_limit_bytes, "hard_limit_bytes")
        if self.soft_budget_bytes > self.hard_limit_bytes:
            raise RetentionPolicyError(
                "soft_budget_bytes must not exceed hard_limit_bytes"
            )
        _require_ttl(self.default_ttl_ms, "default_ttl_ms")
        if not isinstance(self.ttl_ms_by_type, Mapping):
            raise RetentionPolicyError("ttl_ms_by_type must be a mapping")
        for artifact_type, ttl_ms in self.ttl_ms_by_type.items():
            if not isinstance(artifact_type, str) or not _ARTIFACT_TYPE.fullmatch(
                artifact_type
            ):
                raise RetentionPolicyError("ttl_ms_by_type keys must be artifact types")
            _require_ttl(ttl_ms, f"ttl_ms_by_type[{artifact_type}]")
        _require_positive_int(self.max_entries, "max_entries")
        if self.max_entries > MAX_RETENTION_INDEX_ENTRIES:
            raise RetentionPolicyError("max_entries exceeds the retention index limit")
        object.__setattr__(
            self, "ttl_ms_by_type", MappingProxyType(dict(self.ttl_ms_by_type))
        )

    # {
    #   責務: [expiry_for: 新しく登録するArtifactのTTL期限を決める]
    #   処理: [型別TTLを優先し、なければ既定TTLを使う。無期限なら期限を付けない]
    #   引数: [artifact_type: 登録するArtifactの意味型, created_at_ms: 登録時刻のUNIXエポックms]
    #   戻り値: [int | None: 期限のUNIXエポックms。Noneは無期限]
    # }
    def expiry_for(self, artifact_type: str, created_at_ms: int) -> int | None:
        if artifact_type in self.ttl_ms_by_type:
            ttl_ms = self.ttl_ms_by_type[artifact_type]
        else:
            ttl_ms = self.default_ttl_ms
        return None if ttl_ms is None else created_at_ms + ttl_ms

    # {
    #   責務: [to_dict: policyを言語非依存JSON契約のobjectへ変換する]
    #   処理: [型別TTLはキー順に並べ、schemaと同じキー名で出力する]
    #   引数: [なし]
    #   戻り値: [dict: config/retention_policy.schema.json に従うobject]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RETENTION_POLICY_SCHEMA_VERSION,
            "soft_budget_bytes": self.soft_budget_bytes,
            "hard_limit_bytes": self.hard_limit_bytes,
            "default_ttl_ms": self.default_ttl_ms,
            "ttl_ms_by_type": dict(sorted(self.ttl_ms_by_type.items())),
            "max_entries": self.max_entries,
        }

    # {
    #   責務: [from_dict: 設定JSONからpolicyを厳格に復元する]
    #   処理: [版とキー集合を検査し、値の検査は__post_init__へ委ねる]
    #   引数: [value: policy JSON object]
    #   戻り値: [RetentionPolicy: 検証済みpolicy]
    #   エラー: [RetentionPolicyError: objectでない、版・キー不一致、値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> RetentionPolicy:
        if not isinstance(value, Mapping):
            raise RetentionPolicyError("retention policy must be an object")
        keys = set(value)
        if keys != _POLICY_FIELDS:
            raise RetentionPolicyError(
                "invalid retention policy fields "
                f"(missing: {sorted(_POLICY_FIELDS - keys)}; "
                f"unknown: {sorted(map(str, keys - _POLICY_FIELDS))})"
            )
        version = value["schema_version"]
        if type(version) is not int or version != RETENTION_POLICY_SCHEMA_VERSION:
            raise RetentionPolicyError(
                f"unsupported retention policy version: {version!r}"
            )
        return cls(
            soft_budget_bytes=value["soft_budget_bytes"],
            hard_limit_bytes=value["hard_limit_bytes"],
            default_ttl_ms=value["default_ttl_ms"],
            ttl_ms_by_type=value["ttl_ms_by_type"],
            max_entries=value["max_entries"],
        )


# {
#   責務: [_require_positive_int: 容量・件数の値がboolでない正整数か検査する]
#   処理: [int型かつ1以上であることを確認する]
#   引数: [value: 検査値, name: エラー文に出すフィールド名]
#   戻り値: [なし]
#   エラー: [RetentionPolicyError: 正整数でない]
# }
def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise RetentionPolicyError(f"{name} must be a positive integer")


# {
#   責務: [_require_ttl: TTL値が無期限(None)または正整数msか検査する]
#   処理: [Noneを許可し、それ以外は正整数であることを確認する]
#   引数: [value: 検査するTTL, name: エラー文に出すフィールド名]
#   戻り値: [なし]
#   エラー: [RetentionPolicyError: TTLが契約外]
# }
def _require_ttl(value: object, name: str) -> None:
    if value is not None:
        _require_positive_int(value, name)
