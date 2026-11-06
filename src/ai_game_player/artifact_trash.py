"""Artifact objectをcrash安全に削除するためのtrash（intent record→rename→掃除→ack）。"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.retention_index import RetentionIndexError
from ai_game_player.retention_root_lock import is_junction, require_plain_directory

TRASH_DIRECTORY_NAME: Final = ".orphan-trash"
TRASH_SCHEMA_VERSION: Final = 1
MAX_TRASH_INTENT_BYTES: Final = 16 * 1024
MAX_TRASH_INTENTS: Final = 10_000

KIND_OBJECT: Final = "object"
KIND_STAGING: Final = "staging"
REASON_SAFE_ORPHAN: Final = "safe_orphan"
REASON_STALE_STAGING: Final = "stale_staging"
REASON_RETENTION_REMOVE: Final = "retention_remove"
STATE_PENDING: Final = "pending"
STATE_RENAMED: Final = "renamed"
STATE_SWEPT: Final = "swept"

# audit対象のreason。retention_removeはCollectorの通常削除で、audit不要のためswept後にintentを消す。
AUDITED_REASONS: Final = frozenset({REASON_SAFE_ORPHAN, REASON_STALE_STAGING})
_KINDS = frozenset({KIND_OBJECT, KIND_STAGING})
_REASONS = AUDITED_REASONS | {REASON_RETENTION_REMOVE}
_STATES = frozenset({STATE_PENDING, STATE_RENAMED, STATE_SWEPT})
_INTENT_ID = re.compile(r"^[0-9a-f]{32}$")
_INTENT_FILE = re.compile(r"^([0-9a-f]{32})\.intent\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHARD = re.compile(r"^[0-9a-f]{2}$")
_SOURCE = re.compile(r"^[0-9a-f]{2}/(?:[0-9a-f]{64}|\.pending-[A-Za-z0-9_]{1,64})$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TRIGGER = re.compile(r"^[a-z_]{1,32}$")
_ALLOWED_FILES = frozenset({"payload", "metadata.json"})
CURSOR_FILENAME: Final = "scan-cursor.json"
_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "kind",
        "sha256",
        "source",
        "size_bytes",
        "artifact_type",
        "sensitive",
        "data_class",
        "reason",
        "trigger",
        "state",
        "deleted_at_ms",
        "acknowledged",
    }
)


# {
#   責務: [TrashIntentError: trash intent recordが契約外で、安全に判断できないことを通知する]
#   フィールド: [なし: RetentionIndexErrorの理由文だけを持つ]
# }
class TrashIntentError(RetentionIndexError):
    """A trash intent record is corrupt; the trash must not be trusted."""


# {
#   責務: [TrashIntent: 1回の削除の意図と進捗を表す不変record（audit再生成の正本）]
#   フィールド: [id: 32桁hex（trash entry名と同じ）, kind: object/staging, sha256: objectのdigest（stagingはNone）,
#     source: 元の位置（shard/名前）, size_bytes: rename前に数えた合計byte,
#     artifact_type/sensitive/data_class: objectの分類（不明はNone）, reason: 削除理由,
#     trigger: 起動契機（None可）, state: pending/renamed/swept, deleted_at_ms: renamed以降の論理削除時刻,
#     acknowledged: 呼出元がauditを永続化済みか]
# }
@dataclass(frozen=True)
class TrashIntent:
    id: str
    kind: str
    sha256: str | None
    source: str
    size_bytes: int
    artifact_type: str | None
    sensitive: bool | None
    data_class: str | None
    reason: str
    trigger: str | None
    state: str
    deleted_at_ms: int | None = None
    acknowledged: bool = False

    # {
    #   責務: [__post_init__: 契約外の値を持つintentを生成時点で拒否する]
    #   処理: [ID・種別・digest・source形式・byte数・状態・時刻の型と範囲を検査する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [TrashIntentError: いずれかの値が契約外]
    # }
    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and _INTENT_ID.fullmatch(self.id), "id")
        _require(self.kind in _KINDS, "kind")
        _require(self.reason in _REASONS, "reason")
        _require(self.state in _STATES, "state")
        _require(
            isinstance(self.source, str) and _SOURCE.fullmatch(self.source), "source"
        )
        if self.kind == KIND_OBJECT:
            _require(
                isinstance(self.sha256, str) and _SHA256.fullmatch(self.sha256),
                "sha256",
            )
            _require(self.source.endswith(self.sha256), "source")
        else:
            _require(self.sha256 is None, "sha256")
        _require(type(self.size_bytes) is int and self.size_bytes >= 0, "size_bytes")
        for name in ("artifact_type", "data_class"):
            value = getattr(self, name)
            _require(value is None or _is_name(value), name)
        _require(self.sensitive is None or type(self.sensitive) is bool, "sensitive")
        _require(self.trigger is None or _is_trigger(self.trigger), "trigger")
        _require(type(self.acknowledged) is bool, "acknowledged")
        _require(
            self.deleted_at_ms is None
            or (type(self.deleted_at_ms) is int and self.deleted_at_ms >= 0),
            "deleted_at_ms",
        )
        _require(
            (self.state == STATE_PENDING) == (self.deleted_at_ms is None),
            "deleted_at_ms",
        )

    # {
    #   責務: [to_dict: intentを保存用のJSON objectへ変換する]
    #   処理: [schema_versionを含む全フィールドを出力する]
    #   引数: [なし]
    #   戻り値: [dict: 保存するJSON object]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRASH_SCHEMA_VERSION,
            "id": self.id,
            "kind": self.kind,
            "sha256": self.sha256,
            "source": self.source,
            "size_bytes": self.size_bytes,
            "artifact_type": self.artifact_type,
            "sensitive": self.sensitive,
            "data_class": self.data_class,
            "reason": self.reason,
            "trigger": self.trigger,
            "state": self.state,
            "deleted_at_ms": self.deleted_at_ms,
            "acknowledged": self.acknowledged,
        }

    # {
    #   責務: [from_dict: JSON objectからintentを厳格に復元する]
    #   処理: [objectとキー集合とschema_versionを検査し、値の検査は__post_init__へ委ねる]
    #   引数: [value: 保存されていたJSON object]
    #   戻り値: [TrashIntent: 検証済みintent]
    #   エラー: [TrashIntentError: objectでない・キー不一致・版不一致・値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> TrashIntent:
        if not isinstance(value, Mapping) or set(value) != _FIELDS:
            raise TrashIntentError("trash intent has invalid fields")
        version = value["schema_version"]
        if type(version) is not int or version != TRASH_SCHEMA_VERSION:
            raise TrashIntentError(f"unsupported trash intent version: {version!r}")
        return cls(**{name: value[name] for name in _FIELDS - {"schema_version"}})


# {
#   責務: [TrashSweepReport: sweepが進めた作業と、消さずに残した異常を報告する]
#   フィールド: [swept: 掃除を完了したintent ID, abandoned: rename前に停止していて取り消したintent ID,
#     diagnostics: (名前, 理由)の消さなかったもの, stopped_by_deadline: 期限で打切ったか]
# }
@dataclass(frozen=True)
class TrashSweepReport:
    swept: tuple[str, ...]
    abandoned: tuple[str, ...]
    diagnostics: tuple[tuple[str, str], ...]
    stopped_by_deadline: bool


# {
#   責務: [ArtifactTrash: Store root直下の.orphan-trashで、object削除をintent→rename→掃除の順に行いcrash後も再開できるようにする]
#   フィールド: [root: Store root, directory: root/.orphan-trash, clock: UNIXエポックmsを返す関数,
#     rmtree: entryを削除する差替可能な関数（テストで途中停止を注入する）]
#   補足: [全操作は呼出側がroot lockを保持している間だけ行う。trashがlink・junctionなら何もしない]
# }
class ArtifactTrash:
    # {
    #   責務: [__init__: 管理rootからtrashの位置と依存を決める]
    #   処理: [directoryの作成や検査は各操作時に行う]
    #   引数: [root: Artifact Storeのroot, clock: ms時計, rmtree: entry削除関数]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], int] | None = None,
        rmtree: Callable[[Path], None] = shutil.rmtree,
    ) -> None:
        self.root = Path(root)
        self.directory = self.root / TRASH_DIRECTORY_NAME
        self._clock = clock if clock is not None else _wall_clock_ms
        self._rmtree = rmtree

    # {
    #   責務: [exists: trash directoryが作られているかを、安全検査付きで返す]
    #   処理: [存在する場合だけ実directoryか検査する]
    #   引数: [なし]
    #   戻り値: [bool: Trueならtrashが存在する]
    #   エラー: [RetentionRootUnsafeError: link・directory以外]
    # }
    def exists(self) -> bool:
        require_plain_directory(self.root, "retention root")
        if not os.path.lexists(self.directory):
            return False
        require_plain_directory(self.directory, "orphan trash")
        return True

    # {
    #   責務: [ensure: trash directoryを安全検査してから作る]
    #   処理: [検査→作成→再検査の順で、linkへの書込みを避ける]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [RetentionRootUnsafeError: link・directory以外, OSError: 作成失敗]
    # }
    def ensure(self) -> None:
        self.exists()
        self.directory.mkdir(parents=True, exist_ok=True)
        require_plain_directory(self.directory, "orphan trash")

    # {
    #   責務: [list_intents: 全intentを厳格に読み、ID順に返す]
    #   処理: [trashが無ければ空。件数・サイズ・形式を検査し、1件でも不正なら全体を拒否する]
    #   引数: [なし]
    #   戻り値: [tuple: ID順のTrashIntent]
    #   エラー: [TrashIntentError: 破損・上限超過, RetentionRootUnsafeError: trashがlink等]
    # }
    def list_intents(self) -> tuple[TrashIntent, ...]:
        if not self.exists():
            return ()
        intents = []
        for path in sorted(self.directory.iterdir()):
            match = _INTENT_FILE.fullmatch(path.name)
            if match is None:
                continue
            if len(intents) >= MAX_TRASH_INTENTS:
                raise TrashIntentError("trash intents exceed the limit")
            intent = self._read_intent(path)
            if intent.id != match.group(1):
                raise TrashIntentError(f"intent id differs from file name: {path.name}")
            intents.append(intent)
        return tuple(intents)

    # {
    #   責務: [residual_bytes: まだ物理的に残り得るtrash entryのbyte数を返す（使用量計測に加える）]
    #   処理: [swept以外のintentのうち、entryが実在するものだけのsize_bytesを合計する]
    #   引数: [なし]
    #   戻り値: [int: 残存byte]
    #   エラー: [TrashIntentError: intent不正（fail-closed）, RetentionRootUnsafeError: trashがlink等]
    # }
    def residual_bytes(self) -> int:
        return sum(
            intent.size_bytes
            for intent in self.list_intents()
            if intent.state != STATE_SWEPT
            and os.path.lexists(self.directory / intent.id)
        )

    # {
    #   責務: [discard: object/staging directoryをcrash安全に削除する]
    #   処理: [1: intent(pending)を書く 2: trashへrename 3: intentをrenamedへ更新（論理削除の完了点）
    #     4: entryをrmtreeしintentを後始末する。rmtree失敗は例外にせず次回sweepに任せる]
    #   引数: [source: root/shard/名前 のdirectory, kind: object/staging, sha256: objectのdigest,
    #     reason: 削除理由, trigger: 起動契機, artifact_type/sensitive/data_class: 分類]
    #   戻り値: [TrashIntent: 論理削除済みのintent]
    #   エラー: [ValueError: sourceがroot直下の想定配置でない, RetentionCapacityError: intent上限,
    #     RetentionRootUnsafeError: trashがlink等, OSError: intent書込みまたはrename失敗（元は不変）]
    # }
    def discard(
        self,
        source: Path,
        *,
        kind: str,
        sha256: str | None,
        reason: str,
        trigger: str | None = None,
        artifact_type: str | None = None,
        sensitive: bool | None = None,
        data_class: str | None = None,
    ) -> TrashIntent:
        relative = self._relative_source(source)
        self.ensure()
        if len(self.list_intents()) >= MAX_TRASH_INTENTS:
            from ai_game_player.retention_collector import RetentionCapacityError

            raise RetentionCapacityError("orphan trash intent limit reached")
        intent = TrashIntent(
            id=uuid.uuid4().hex,
            kind=kind,
            sha256=sha256,
            source=relative,
            size_bytes=_tree_size(source),
            artifact_type=artifact_type,
            sensitive=sensitive,
            data_class=data_class,
            reason=reason,
            trigger=trigger,
            state=STATE_PENDING,
        )
        self._write_intent(intent)
        entry = self.directory / intent.id
        try:
            os.rename(source, entry)
        except OSError:
            self._intent_path(intent.id).unlink(missing_ok=True)
            raise
        # renameは成立済み。永続化の確認失敗は例外にせず次回sweepに任せる。
        with contextlib.suppress(OSError):
            _fsync_directory(source.parent)
            _fsync_directory(self.directory)
        renamed = replace(intent, state=STATE_RENAMED, deleted_at_ms=self._clock())
        try:
            self._write_intent(renamed)
        except OSError:
            # renameは済んでおり論理的には削除済み。例外にすると呼出元が「未削除」と誤解するので、
            # pendingのまま返し、次回sweepがentryを見てrenamedへ進める。
            return intent
        return self._remove_entry(renamed)

    # {
    #   責務: [sweep: crashで中断した削除を検査付きで再開し、残存entryを掃除する]
    #   処理: [intentごとに状態とentryの有無を見て、pending取消・renamed更新・検査付きrmtree・
    #     swept後始末を行う。intentの無いentryや検査不合格のentryは消さずdiagnosticにする]
    #   引数: [deadline: time.monotonic基準の打切り時刻（Noneは無期限）,
    #     timer: 時刻関数（テストで差替可）]
    #   戻り値: [TrashSweepReport: 掃除した/取消した/残した内容]
    #   エラー: [TrashIntentError: intent不正, RetentionRootUnsafeError: trashがlink等, OSError: intent更新失敗]
    # }
    def sweep(
        self,
        *,
        deadline: float | None = None,
        timer: Callable[[], float] = time.monotonic,
    ) -> TrashSweepReport:
        swept: list[str] = []
        abandoned: list[str] = []
        diagnostics: list[tuple[str, str]] = []
        intents = self.list_intents()
        known = {intent.id for intent in intents}
        stopped = False
        for intent in intents:
            if deadline is not None and timer() >= deadline:
                stopped = True
                break
            entry = self.directory / intent.id
            present = os.path.lexists(entry)
            if intent.state == STATE_PENDING:
                if not present:
                    # renameに到達していない。元のobjectはshardに無傷で残っているので取り消す。
                    self._intent_path(intent.id).unlink(missing_ok=True)
                    abandoned.append(intent.id)
                    continue
                intent = replace(
                    intent, state=STATE_RENAMED, deleted_at_ms=self._clock()
                )
                self._write_intent(intent)
            if intent.state == STATE_RENAMED:
                if present:
                    problem = self._entry_problem(intent, entry)
                    if problem is not None:
                        diagnostics.append((intent.id, problem))
                        continue
                    if self._remove_entry(intent).state != STATE_SWEPT:
                        diagnostics.append((intent.id, "remove_failed"))
                        continue
                    swept.append(intent.id)
                else:
                    self._finish(intent)
                    swept.append(intent.id)
                continue
            self._finish(intent)
        for path in sorted(self.directory.iterdir()) if self.exists() else ():
            if _INTENT_ID.fullmatch(path.name) and path.name not in known:
                diagnostics.append((path.name, "entry_without_intent"))
        return TrashSweepReport(
            tuple(swept), tuple(abandoned), tuple(diagnostics), stopped
        )

    # {
    #   責務: [audit_intents: 呼出元がまだ永続化していない削除のintentを返す]
    #   処理: [auditの対象理由で、renamed以降かつ未ackのintentをID順に返す]
    #   引数: [なし]
    #   戻り値: [tuple: 未ackのTrashIntent]
    #   エラー: [TrashIntentError: intent不正]
    # }
    def audit_intents(self) -> tuple[TrashIntent, ...]:
        return tuple(
            intent
            for intent in self.list_intents()
            if intent.reason in AUDITED_REASONS
            and intent.state != STATE_PENDING
            and not intent.acknowledged
        )

    # {
    #   責務: [acknowledge: 永続化済みのauditに対応するintentを確認済みにする]
    #   処理: [sweep済みなら削除し、未完了なら確認済みフラグだけ立てて掃除を妨げない]
    #   引数: [ids: audit_id（intent ID）の列]
    #   戻り値: [なし]
    #   エラー: [TrashIntentError: ID形式またはintent不正, OSError: 更新失敗]
    # }
    def acknowledge(self, ids: Iterable[str]) -> None:
        for intent_id in ids:
            _require(
                isinstance(intent_id, str) and _INTENT_ID.fullmatch(intent_id), "id"
            )
            if not self.exists():
                return
            path = self._intent_path(intent_id)
            if not os.path.lexists(path):
                continue
            intent = self._read_intent(path)
            if intent.state == STATE_SWEPT:
                path.unlink(missing_ok=True)
            elif not intent.acknowledged:
                self._write_intent(replace(intent, acknowledged=True))

    # {
    #   責務: [_remove_entry: entryをrmtreeし、成功したらintentを後始末する]
    #   処理: [rmtree失敗は例外にせずrenamed状態のまま返す（次回sweepで再開）]
    #   引数: [intent: renamed状態のintent]
    #   戻り値: [TrashIntent: 後始末後のintent（失敗時は引数のまま）]
    # }
    def _remove_entry(self, intent: TrashIntent) -> TrashIntent:
        entry = self.directory / intent.id
        try:
            self._rmtree(entry)
        except OSError:
            return intent
        return self._finish(intent)

    # {
    #   責務: [_finish: 掃除済みのintentを後始末する]
    #   処理: [retention_removeまたは確認済みならintentを削除し、そうでなければswept状態で残す]
    #   引数: [intent: entryが無くなったintent]
    #   戻り値: [TrashIntent: swept状態のintent]
    #   エラー: [OSError: intent更新・削除失敗]
    # }
    def _finish(self, intent: TrashIntent) -> TrashIntent:
        if intent.reason == REASON_RETENTION_REMOVE or intent.acknowledged:
            self._intent_path(intent.id).unlink(missing_ok=True)
            return replace(intent, state=STATE_SWEPT)
        if intent.state != STATE_SWEPT:
            intent = replace(intent, state=STATE_SWEPT)
            self._write_intent(intent)
        return intent

    # {
    #   責務: [_entry_problem: 消してよいtrash entryかを検査し、問題があれば理由を返す]
    #   処理: [実directoryであること、audit対象ではpayload/metadata.jsonの通常file部分集合であること、
    #     objectでmetadataが残っていればsha256がintentと一致することを確認する]
    #   引数: [intent: 対象のintent, entry: trash内のentry]
    #   戻り値: [str | None: 問題の理由。Noneなら削除してよい]
    # }
    def _entry_problem(self, intent: TrashIntent, entry: Path) -> str | None:
        if entry.is_symlink() or is_junction(entry) or not entry.is_dir():
            return "entry_not_plain_directory"
        if intent.reason == REASON_RETENTION_REMOVE:
            return None
        try:
            names = os.listdir(entry)
            for name in names:
                if name not in _ALLOWED_FILES:
                    return "unexpected_file"
                child = entry / name
                if child.is_symlink() or is_junction(child) or not child.is_file():
                    return "entry_file_not_regular"
            if intent.kind == KIND_OBJECT and "metadata.json" in names:
                raw = json.loads(
                    read_regular_file(
                        entry / "metadata.json", max_bytes=MAX_TRASH_INTENT_BYTES
                    )
                )
                if not isinstance(raw, dict) or raw.get("sha256") != intent.sha256:
                    return "digest_mismatch"
        except (OSError, ValueError):
            return "entry_unreadable"
        return None

    # {
    #   責務: [_relative_source: 削除元がroot/shard/名前の想定配置か確認して相対表記を返す]
    #   処理: [親のshard名と祖父がrootであることを実pathで比較する]
    #   引数: [source: 削除元directory]
    #   戻り値: [str: shard/名前]
    #   エラー: [ValueError: 想定配置でない]
    # }
    def _relative_source(self, source: Path) -> str:
        shard = source.parent
        if _SHARD.fullmatch(shard.name) is None or os.path.normcase(
            os.path.abspath(shard.parent)
        ) != os.path.normcase(os.path.abspath(self.root)):
            raise ValueError(f"trash source is not under root/shard: {source}")
        relative = f"{shard.name}/{source.name}"
        if _SOURCE.fullmatch(relative) is None:
            raise ValueError(f"trash source has an unexpected name: {source}")
        return relative

    # {
    #   責務: [_intent_path: intent IDからrecord fileの位置を返す]
    #   処理: [trash直下の<id>.intent.jsonを組み立てる]
    #   引数: [intent_id: 32桁hex]
    #   戻り値: [Path: record file]
    # }
    def _intent_path(self, intent_id: str) -> Path:
        return self.directory / f"{intent_id}.intent.json"

    # {
    #   責務: [_write_intent: intentをfsync済みの一時fileから原子的に公開する]
    #   処理: [stage_json_writeで書いて置換し、一時fileを後始末する]
    #   引数: [intent: 保存するintent]
    #   戻り値: [なし]
    #   エラー: [OSError: 書込み・置換失敗]
    # }
    def _write_intent(self, intent: TrashIntent) -> None:
        staged = stage_json_write(self._intent_path(intent.id), intent.to_dict())
        try:
            staged.publish()
        finally:
            staged.discard()

    # {
    #   責務: [_read_intent: 1件のintentを通常fileとして上限付きで読み検証する]
    #   処理: [link・非通常file・容量超過・JSON破損をTrashIntentErrorへ統一する]
    #   引数: [path: intent file]
    #   戻り値: [TrashIntent: 検証済みintent]
    #   エラー: [TrashIntentError: 読込・検証に失敗]
    # }
    @staticmethod
    def _read_intent(path: Path) -> TrashIntent:
        if path.is_symlink() or is_junction(path):
            raise TrashIntentError(f"trash intent must not be a link: {path.name}")
        try:
            payload = read_regular_file(path, max_bytes=MAX_TRASH_INTENT_BYTES)
            return TrashIntent.from_dict(json.loads(payload.decode("utf-8")))
        except (OSError, ValueError) as error:
            if isinstance(error, TrashIntentError):
                raise
            raise TrashIntentError(
                f"trash intent cannot be read: {path.name}: {error}"
            ) from error


# {
#   責務: [_wall_clock_ms: 実時刻をUNIXエポックmsで返す既定clock]
#   処理: [time.time_nsをmsへ切り捨てる]
#   引数: [なし]
#   戻り値: [int: UNIXエポックms]
# }
def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


# {
#   責務: [_require: 条件が偽ならintentの該当フィールド名付きで拒否する]
#   処理: [真偽を評価し、偽ならTrashIntentErrorを送出する]
#   引数: [condition: 検査結果, name: フィールド名]
#   戻り値: [なし]
#   エラー: [TrashIntentError: 条件が偽]
# }
def _require(condition: object, name: str) -> None:
    if not condition:
        raise TrashIntentError(f"trash intent field is invalid: {name}")


# {
#   責務: [_is_name: 識別子形式の文字列か判定する]
#   処理: [str型かつ許可文字・長さの完全一致を確認する]
#   引数: [value: 検査値]
#   戻り値: [bool: Trueなら識別子]
# }
def _is_name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


# {
#   責務: [_is_trigger: 起動契機名の形式か判定する]
#   処理: [str型かつ小文字とアンダースコアだけの短い名前かを確認する]
#   引数: [value: 検査値]
#   戻り値: [bool: Trueなら契機名]
# }
def _is_trigger(value: object) -> bool:
    return isinstance(value, str) and _TRIGGER.fullmatch(value) is not None


# {
#   責務: [_tree_size: directory配下のfile byte数をlinkを辿らずに合計する]
#   処理: [os.walkで通常fileのlstat sizeを足す。読めない場合はOSErrorを送出する]
#   引数: [path: 対象directory]
#   戻り値: [int: 合計byte]
# }
def _tree_size(path: Path) -> int:
    total = 0
    for current, _, files in os.walk(path, followlinks=False, onerror=_raise):
        for name in files:
            # 読めないfileは数え漏れになるため、例外にしてrename前に削除を止める。
            total += os.lstat(os.path.join(current, name)).st_size
    return total


# {
#   責務: [_raise: os.walkの列挙エラーを握りつぶさず送出する]
#   処理: [受け取ったOSErrorをそのまま送出する]
#   引数: [error: os.walkが渡す例外]
#   戻り値: [なし]
#   エラー: [OSError: 常に送出]
# }
def _raise(error: OSError) -> None:
    raise error


# {
#   責務: [_fsync_directory: renameの永続化のためdirectoryをfsyncする（Windowsでは何もしない）]
#   処理: [POSIXのみ、読取りで開いてfsyncする]
#   引数: [path: 対象directory]
#   戻り値: [なし]
#   エラー: [OSError: 開く・同期に失敗]
# }
def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
