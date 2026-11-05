"""中断したadmissionを後から判定・回収できるpending admission recordの保存。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.retention_root_lock import is_junction

PENDING_DIRECTORY_NAME = ".admission-pending"
PENDING_SCHEMA_VERSION = 1
MAX_PENDING_RECORD_BYTES = 16 * 1024
MAX_PENDING_RECORDS = 1_000
_ADMISSION_ID = re.compile(r"^[0-9a-f]{32}$")
_RECORD_FILENAME = re.compile(r"^([0-9a-f]{32})\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ENTRY_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
SOURCE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FIELDS = frozenset(
    {
        "schema_version",
        "admission_id",
        "artifact_sha256",
        "created_object",
        "entry_id",
        "source_ref",
        "artifact_type",
        "media_type",
        "payload_size",
        "protected",
        "started_at_ms",
        "owner_pid",
    }
)


# {
#   責務: [AdmissionRecordError: pending recordが契約外で、回収を安全に判断できないことを通知する]
#   フィールド: [なし: ValueErrorの理由文だけを持つ]
# }
class AdmissionRecordError(ValueError):
    """A pending admission record is corrupt or unsafe; recovery must not proceed."""


# {
#   責務: [PendingAdmission: Storeへputする直前に残す、1回のadmissionの回収用記録の不変値]
#   フィールド: [admission_id: 32桁hexの一意ID, artifact_sha256: putするpayloadのdigest,
#     created_object: putの直前にobjectが無く、このadmissionが新規作成する予定ならTrue,
#     entry_id/source_ref/artifact_type/media_type/payload_size/protected: requestの要約（診断用）,
#     started_at_ms: 開始時刻のUNIXエポックms, owner_pid: 実行processのpid（診断metadata。lockの正当性判定には使わない）]
# }
@dataclass(frozen=True)
class PendingAdmission:
    admission_id: str
    artifact_sha256: str
    created_object: bool
    entry_id: str
    source_ref: str
    artifact_type: str
    media_type: str
    payload_size: int
    protected: bool
    started_at_ms: int
    owner_pid: int

    # {
    #   責務: [__post_init__: 契約外の値を持つpending recordを生成時点で拒否する]
    #   処理: [ID・digest・型の形式、真偽値、非負整数を検査する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [AdmissionRecordError: いずれかのフィールドが契約外]
    # }
    def __post_init__(self) -> None:
        _require_match(self.admission_id, _ADMISSION_ID, "admission_id")
        _require_match(self.artifact_sha256, _SHA256, "artifact_sha256")
        _require_match(self.entry_id, _ENTRY_ID, "entry_id")
        _require_match(self.source_ref, SOURCE_REF_PATTERN, "source_ref")
        _require_match(self.artifact_type, _ARTIFACT_TYPE, "artifact_type")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise AdmissionRecordError("media_type must be a non-empty string")
        for name in ("created_object", "protected"):
            if type(getattr(self, name)) is not bool:
                raise AdmissionRecordError(f"{name} must be a boolean")
        for name in ("payload_size", "started_at_ms", "owner_pid"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise AdmissionRecordError(f"{name} must be a non-negative integer")

    # {
    #   責務: [to_dict: recordを保存用のJSON objectへ変換する]
    #   処理: [schema_versionを含む全フィールドを出力する]
    #   引数: [なし]
    #   戻り値: [dict: 保存するJSON object]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PENDING_SCHEMA_VERSION,
            "admission_id": self.admission_id,
            "artifact_sha256": self.artifact_sha256,
            "created_object": self.created_object,
            "entry_id": self.entry_id,
            "source_ref": self.source_ref,
            "artifact_type": self.artifact_type,
            "media_type": self.media_type,
            "payload_size": self.payload_size,
            "protected": self.protected,
            "started_at_ms": self.started_at_ms,
            "owner_pid": self.owner_pid,
        }

    # {
    #   責務: [from_dict: JSON objectからpending recordを厳格に復元する]
    #   処理: [objectとキー集合とschema_versionを検査し、値の検査は__post_init__へ委ねる]
    #   引数: [value: 保存されていたJSON object]
    #   戻り値: [PendingAdmission: 検証済みrecord]
    #   エラー: [AdmissionRecordError: objectでない・キー不一致・版不一致・値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> PendingAdmission:
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise AdmissionRecordError("pending admission has invalid fields")
        version = value["schema_version"]
        if type(version) is not int or version != PENDING_SCHEMA_VERSION:
            raise AdmissionRecordError(
                f"unsupported pending admission version: {version!r}"
            )
        return cls(**{name: value[name] for name in _FIELDS - {"schema_version"}})


# {
#   責務: [PendingAdmissionStore: Store root直下の専用directoryへpending recordを原子的に保存・列挙・削除する]
#   フィールド: [directory: root直下の.admission-pending。symlink/junctionは拒否する]
#   補足: [呼出側(ArtifactAdmissionCoordinator)がroot lockを保持している間だけ使う]
# }
class PendingAdmissionStore:
    # {
    #   責務: [__init__: 管理rootから記録先directoryを決める]
    #   処理: [directoryの作成や検査は各操作時に行う]
    #   引数: [root: Artifact Storeのroot directory]
    #   戻り値: [なし]
    # }
    def __init__(self, root: Path) -> None:
        self.directory = Path(root) / PENDING_DIRECTORY_NAME

    # {
    #   責務: [write: pending recordをfsync済みの一時fileから原子的に公開する]
    #   処理: [記録先がlinkでないことを確認して作成し、stage_json_writeで置換する]
    #   引数: [record: 保存するpending record]
    #   戻り値: [なし]
    #   エラー: [AdmissionRecordError: 記録先がlink等, OSError: 書込み・置換失敗]
    # }
    def write(self, record: PendingAdmission) -> None:
        self._require_safe_directory()
        self.directory.mkdir(parents=True, exist_ok=True)
        staged = stage_json_write(
            self.directory / f"{record.admission_id}.json", record.to_dict()
        )
        try:
            staged.publish()
        finally:
            staged.discard()

    # {
    #   責務: [remove: 完了または回収済みのpending recordを削除する]
    #   処理: [既に無ければ何もせず、通常fileだけを削除する]
    #   引数: [admission_id: 削除するrecordのID]
    #   戻り値: [なし]
    #   エラー: [AdmissionRecordError: ID形式または記録先が不正, OSError: 削除失敗]
    # }
    def remove(self, admission_id: str) -> None:
        _require_match(admission_id, _ADMISSION_ID, "admission_id")
        self._require_safe_directory()
        path = self.directory / f"{admission_id}.json"
        if path.is_symlink() or is_junction(path):
            raise AdmissionRecordError(f"pending record must not be a link: {path}")
        path.unlink(missing_ok=True)

    # {
    #   責務: [list_records: 全pending recordを上限付きで厳格に読み、admission_id順に返す]
    #   処理: [記録先がlinkでないことと件数上限を確認し、各recordを16KiB以内で検証する。
    #     隠しfile名の一時fileは無視する]
    #   引数: [なし]
    #   戻り値: [tuple: admission_id順のPendingAdmission]
    #   エラー: [AdmissionRecordError: link・非通常file・容量超過・JSON破損・契約違反・件数超過。
    #     部分結果は返さない]
    # }
    def list_records(self) -> tuple[PendingAdmission, ...]:
        if not os.path.lexists(self.directory):
            return ()
        self._require_safe_directory()
        records = []
        for path in sorted(self.directory.iterdir()):
            match = _RECORD_FILENAME.fullmatch(path.name)
            if match is None:
                continue
            if len(records) >= MAX_PENDING_RECORDS:
                raise AdmissionRecordError("pending admission records exceed the limit")
            record = self._read_record(path)
            if record.admission_id != match.group(1):
                raise AdmissionRecordError(
                    f"pending record id does not match its file name: {path.name}"
                )
            records.append(record)
        return tuple(records)

    # {
    #   責務: [_read_record: 1件のrecordを通常fileとして上限付きで読み、検証して復元する]
    #   処理: [link・非通常file・容量超過・JSON破損を全てAdmissionRecordErrorへ統一する]
    #   引数: [path: record file]
    #   戻り値: [PendingAdmission: 検証済みrecord]
    #   エラー: [AdmissionRecordError: 読込・検証に失敗]
    # }
    @staticmethod
    def _read_record(path: Path) -> PendingAdmission:
        if path.is_symlink() or is_junction(path):
            raise AdmissionRecordError(f"pending record must not be a link: {path}")
        try:
            payload = read_regular_file(path, max_bytes=MAX_PENDING_RECORD_BYTES)
            return PendingAdmission.from_dict(json.loads(payload.decode("utf-8")))
        except (OSError, ValueError) as error:
            if isinstance(error, AdmissionRecordError):
                raise
            raise AdmissionRecordError(
                f"pending record cannot be read: {path.name}: {error}"
            ) from error

    # {
    #   責務: [_require_safe_directory: 記録先がlink・directory以外でないことを保証する]
    #   処理: [存在する場合だけsymlink/junction/非directoryを拒否する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [AdmissionRecordError: 記録先がlinkまたはdirectory以外]
    # }
    def _require_safe_directory(self) -> None:
        if os.path.lexists(self.directory) and (
            self.directory.is_symlink()
            or is_junction(self.directory)
            or not self.directory.is_dir()
        ):
            raise AdmissionRecordError(
                f"pending directory must be a plain directory: {self.directory}"
            )


# {
#   責務: [_require_match: 文字列フィールドが形式に一致するか検査する]
#   処理: [str型かつ正規表現の完全一致であることを確認する]
#   引数: [value: 検査値, pattern: 許可する形式, name: エラー文に出すフィールド名]
#   戻り値: [なし]
#   エラー: [AdmissionRecordError: 形式に一致しない]
# }
def _require_match(value: object, pattern: re.Pattern[str], name: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AdmissionRecordError(f"{name} has an invalid format")
