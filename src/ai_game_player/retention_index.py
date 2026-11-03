"""Retention Coreの保持索引（言語非依存JSON契約）を検証付きで復元・直列化する。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RETENTION_INDEX_SCHEMA_VERSION = 1
# 索引全体の読込上限。破損・巨大化した索引で無制限にメモリを使わない。
MAX_RETENTION_INDEX_BYTES = 32 * 1024 * 1024
# 1索引に保持できるentry数の絶対上限。policyの上限はこれ以下に制限する。
MAX_RETENTION_INDEX_ENTRIES = 100_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ENTRY_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_ENTRY_FIELDS = frozenset(
    {
        "entry_id",
        "artifact_sha256",
        "size_bytes",
        "artifact_type",
        "created_at_ms",
        "expires_at_ms",
        "protected",
    }
)
_INDEX_FIELDS = frozenset({"schema_version", "entries"})


# {
#   責務: [RetentionIndexError: 保持索引が契約外で、GCを安全に判断できないことを呼出側へ通知する]
#   フィールド: [なし: ValueErrorの理由文だけを持つ]
# }
class RetentionIndexError(ValueError):
    """The retention index is corrupt or violates its contract; GC must not proceed."""


# {
#   責務: [RetentionEntry: Artifact Store上の1 objectを参照する論理保持記録の不変値]
#   フィールド: [entry_id: 呼出側が付ける一意の記録ID, artifact_sha256: 参照するArtifactのSHA-256,
#     size_bytes: そのArtifactのpayload byte数, artifact_type: Artifactの意味型,
#     created_at_ms: 登録時刻のUNIXエポックms（UTC）, expires_at_ms: TTL期限のUNIXエポックms。Noneは無期限,
#     protected: Trueなら重複除去・TTL・容量超過のどの理由でも削除しない]
# }
@dataclass(frozen=True)
class RetentionEntry:
    entry_id: str
    artifact_sha256: str
    size_bytes: int
    artifact_type: str
    created_at_ms: int
    expires_at_ms: int | None
    protected: bool

    # {
    #   責務: [__post_init__: 契約外の値を持つ保持記録を生成時点で拒否する]
    #   処理: [ID・digest・型の形式、非負整数、期限が登録時刻以降であること、真偽値型を検査する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [RetentionIndexError: いずれかのフィールドが契約外]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not _ENTRY_ID.fullmatch(self.entry_id):
            raise RetentionIndexError("entry_id must be a lowercase identifier")
        if not isinstance(self.artifact_sha256, str) or not _SHA256.fullmatch(
            self.artifact_sha256
        ):
            raise RetentionIndexError("artifact_sha256 must be a lowercase SHA-256")
        if not isinstance(self.artifact_type, str) or not _ARTIFACT_TYPE.fullmatch(
            self.artifact_type
        ):
            raise RetentionIndexError("artifact_type must be a lowercase identifier")
        _require_non_negative_int(self.size_bytes, "size_bytes")
        _require_non_negative_int(self.created_at_ms, "created_at_ms")
        if self.expires_at_ms is not None:
            _require_non_negative_int(self.expires_at_ms, "expires_at_ms")
            if self.expires_at_ms < self.created_at_ms:
                raise RetentionIndexError(
                    "expires_at_ms must not precede created_at_ms"
                )
        if not isinstance(self.protected, bool):
            raise RetentionIndexError("protected must be a boolean")

    # {
    #   責務: [to_dict: 保持記録を言語非依存JSON契約のobjectへ変換する]
    #   処理: [全フィールドをschemaと同じキー名で出力する]
    #   引数: [なし]
    #   戻り値: [dict: config/retention_index.schema.json のentry形]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "artifact_sha256": self.artifact_sha256,
            "size_bytes": self.size_bytes,
            "artifact_type": self.artifact_type,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "protected": self.protected,
        }

    # {
    #   責務: [from_dict: JSON objectから保持記録を厳格に復元する]
    #   処理: [欠落・未知キーを拒否し、値の型と形式は__post_init__で検査する]
    #   引数: [value: 索引JSONのentry object]
    #   戻り値: [RetentionEntry: 検証済み保持記録]
    #   エラー: [RetentionIndexError: objectでない、キー不一致、値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> RetentionEntry:
        if not isinstance(value, Mapping):
            raise RetentionIndexError("retention entry must be an object")
        _require_exact_keys(value, _ENTRY_FIELDS, "retention entry")
        return cls(
            entry_id=value["entry_id"],
            artifact_sha256=value["artifact_sha256"],
            size_bytes=value["size_bytes"],
            artifact_type=value["artifact_type"],
            created_at_ms=value["created_at_ms"],
            expires_at_ms=value["expires_at_ms"],
            protected=value["protected"],
        )


# {
#   責務: [validate_entries: 保持記録の集合が索引全体の不変条件を満たすか検査する]
#   処理: [件数上限、entry_idの一意性、同じdigestを指す記録のsize一致を確認する]
#   引数: [entries: 検査する保持記録, max_entries: 許可する最大件数]
#   戻り値: [tuple: entry_id順に並べた検証済み保持記録]
#   エラー: [RetentionIndexError: 件数超過、ID重複、同一digestのsize不一致]
# }
def validate_entries(
    entries: Iterable[RetentionEntry], *, max_entries: int = MAX_RETENTION_INDEX_ENTRIES
) -> tuple[RetentionEntry, ...]:
    by_id: dict[str, RetentionEntry] = {}
    size_by_digest: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, RetentionEntry):
            raise RetentionIndexError("retention index contains a non-entry value")
        if len(by_id) >= max_entries:
            raise RetentionIndexError("retention index exceeds the entry limit")
        if entry.entry_id in by_id:
            raise RetentionIndexError(f"duplicate retention entry_id: {entry.entry_id}")
        known_size = size_by_digest.setdefault(entry.artifact_sha256, entry.size_bytes)
        if known_size != entry.size_bytes:
            raise RetentionIndexError(
                f"entries disagree on size for artifact {entry.artifact_sha256}"
            )
        by_id[entry.entry_id] = entry
    return tuple(by_id[entry_id] for entry_id in sorted(by_id))


# {
#   責務: [parse_retention_index: 保存済み索引bytesを上限付きで検証し、保持記録へ復元する]
#   処理: [byte上限、UTF-8 JSON、schema_version、キー集合、entry配列を順に検査し、全体不変条件を確認する]
#   引数: [payload: 索引ファイルの内容, max_entries: 許可する最大件数]
#   戻り値: [tuple: entry_id順の検証済み保持記録]
#   エラー: [RetentionIndexError: 容量超過・JSON破損・版不一致・契約違反。部分的な結果は返さない]
# }
def parse_retention_index(
    payload: bytes, *, max_entries: int = MAX_RETENTION_INDEX_ENTRIES
) -> tuple[RetentionEntry, ...]:
    if not isinstance(payload, bytes):
        raise RetentionIndexError("retention index payload must be bytes")
    if len(payload) > MAX_RETENTION_INDEX_BYTES:
        raise RetentionIndexError("retention index exceeds the byte limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionIndexError(
            f"retention index is not valid JSON: {error}"
        ) from error
    if not isinstance(document, Mapping):
        raise RetentionIndexError("retention index must be an object")
    _require_exact_keys(document, _INDEX_FIELDS, "retention index")
    version = document["schema_version"]
    if type(version) is not int or version != RETENTION_INDEX_SCHEMA_VERSION:
        raise RetentionIndexError(f"unsupported retention index version: {version!r}")
    raw_entries = document["entries"]
    if not isinstance(raw_entries, list):
        raise RetentionIndexError("retention index entries must be an array")
    if len(raw_entries) > max_entries:
        raise RetentionIndexError("retention index exceeds the entry limit")
    return validate_entries(
        (RetentionEntry.from_dict(item) for item in raw_entries),
        max_entries=max_entries,
    )


# {
#   責務: [retention_index_document: 保持記録を保存用の索引JSON objectへ変換する]
#   処理: [全体不変条件を再検査し、entry_id順の決定的な配列として出力する]
#   引数: [entries: 保存する保持記録]
#   戻り値: [dict: config/retention_index.schema.json に従う索引object]
#   エラー: [RetentionIndexError: 保存前の全体不変条件違反]
# }
def retention_index_document(entries: Iterable[RetentionEntry]) -> dict[str, Any]:
    return {
        "schema_version": RETENTION_INDEX_SCHEMA_VERSION,
        "entries": [entry.to_dict() for entry in validate_entries(entries)],
    }


# {
#   責務: [_require_exact_keys: JSON objectのキー集合が契約と完全一致するか検査する]
#   処理: [欠落キーと未知キーを集め、どちらかがあれば理由付きで拒否する]
#   引数: [value: 検査するobject, expected: 契約上のキー集合, label: エラー文に出す対象名]
#   戻り値: [なし]
#   エラー: [RetentionIndexError: キーの欠落または未知キー]
# }
def _require_exact_keys(
    value: Mapping[Any, Any], expected: frozenset[str], label: str
) -> None:
    keys = set(value)
    missing = expected - keys
    unknown = keys - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown: {', '.join(sorted(map(str, unknown)))}")
        raise RetentionIndexError(f"invalid {label} fields ({'; '.join(details)})")


# {
#   責務: [_require_non_negative_int: byte数・時刻の値がboolでない非負整数か検査する]
#   処理: [int型かつ0以上であることを確認する]
#   引数: [value: 検査値, name: エラー文に出すフィールド名]
#   戻り値: [なし]
#   エラー: [RetentionIndexError: 非負整数でない]
# }
def _require_non_negative_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise RetentionIndexError(f"{name} must be a non-negative integer")
