"""FileSystemArtifactStoreへRetention計画を、管理root内に限って安全に適用する。"""

from __future__ import annotations

import functools
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from ai_game_player.artifact_trash import (
    KIND_OBJECT,
    REASON_RETENTION_REMOVE,
    ArtifactTrash,
)
from ai_game_player.artifact_store import (
    ArtifactIntegrityError,
    FileSystemArtifactStore,
)
from ai_game_player.experience import ArtifactReference
from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.retention_index import (
    MAX_RETENTION_INDEX_BYTES,
    RetentionEntry,
    RetentionIndexError,
    parse_retention_index,
    retention_index_document,
)
from ai_game_player.retention_planner import RetentionPlan, plan_retention
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.retention_root_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    is_junction as _is_junction,
    retention_root_lock,
)

RETENTION_INDEX_FILENAME = "retention-index.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_Result = TypeVar("_Result")


# {
#   責務: [RetentionSafetyError: 削除先が管理root外・symlink経由・想定外の配置であるため処理を止めたことを通知する]
#   フィールド: [なし: RuntimeErrorの理由文だけを持つ]
# }
class RetentionSafetyError(RuntimeError):
    """A deletion target escaped the managed root; nothing was deleted."""


# {
#   責務: [RetentionCapacityError: 新しい記録を受け入れるとhard limitまたは件数上限を超えることを通知する]
#   フィールド: [なし: RuntimeErrorの理由文だけを持つ]
# }
class RetentionCapacityError(RuntimeError):
    """Admitting a new entry would exceed the hard limit or the entry limit."""


# {
#   責務: [RetentionFileSystem: Retention Collectorが使う索引読書きとobject削除を差し替え可能にする]
#   フィールド: [なし: テストや別backendが実装する操作契約]
# }
class RetentionFileSystem(Protocol):
    # {
    #   責務: [read_index: 索引ファイルを上限付きで読む]
    #   処理: [ファイルがなければNone、通常ファイルなら上限以内のbytesを返す]
    #   引数: [path: 索引ファイル, max_bytes: 読込上限byte]
    #   戻り値: [bytes | None: 索引内容。Noneは索引が未作成]
    #   エラー: [ValueError/OSError: 非通常ファイル・容量超過・読込失敗]
    # }
    def read_index(self, path: Path, *, max_bytes: int) -> bytes | None: ...

    # {
    #   責務: [write_index: 索引objectを原子的に置換保存する]
    #   処理: [同じディレクトリの一時ファイルへ書き同期してから置換する]
    #   引数: [path: 索引ファイル, document: 保存する索引object]
    #   戻り値: [なし]
    #   エラー: [OSError: 書込み・置換失敗]
    # }
    def write_index(self, path: Path, document: dict[str, Any]) -> None: ...

    # {
    #   責務: [remove_object: 安全検査済みのArtifact object directoryを削除する]
    #   処理: [directoryとその中身を削除する]
    #   引数: [path: 削除するobject directory]
    #   戻り値: [なし]
    #   エラー: [OSError: 削除失敗]
    # }
    def remove_object(self, path: Path) -> None: ...


# {
#   責務: [LocalRetentionFileSystem: ローカルdiskでRetentionFileSystem契約を実装する]
#   フィールド: [なし: 状態を持たない]
# }
class LocalRetentionFileSystem:
    # {
    #   責務: [read_index: 索引ファイルを非通常ファイル拒否・上限付きで読む]
    #   処理: [未作成ならNone、存在すれば共通の有界読込で読む]
    #   引数: [path: 索引ファイル, max_bytes: 読込上限byte]
    #   戻り値: [bytes | None: 索引内容。Noneは索引が未作成]
    #   エラー: [ValueError/OSError: 非通常ファイル・容量超過・読込失敗]
    # }
    def read_index(self, path: Path, *, max_bytes: int) -> bytes | None:
        if not os.path.lexists(path):
            return None
        return read_regular_file(path, max_bytes=max_bytes)

    # {
    #   責務: [write_index: 索引objectを一時ファイル経由で原子的に置換する]
    #   処理: [共有のstage_json_writeで同期済み一時ファイルを作り、公開後に後始末する]
    #   引数: [path: 索引ファイル, document: 保存する索引object]
    #   戻り値: [なし]
    #   エラー: [OSError: 書込み・置換失敗]
    # }
    def write_index(self, path: Path, document: dict[str, Any]) -> None:
        staged = stage_json_write(path, document)
        try:
            staged.publish()
        finally:
            staged.discard()

    # {
    #   責務: [remove_object: object directoryをcrash安全に削除する]
    #   処理: [削除直前にもsymlink・junctionでない実directoryかを再確認し、ArtifactTrashへ
    #     intent→rename→rmtreeの順で移す（途中で停止してもshardに半壊objectを残さない。Issue #790）]
    #   引数: [path: 安全検査済みのobject directory]
    #   戻り値: [なし]
    #   エラー: [RetentionSafetyError: 検査後にsymlink等へ差し替えられた, OSError: intent書込み・rename失敗
    #     （元のobjectは不変）。rmtree失敗は例外にせず、次回のtrash sweepが再開する]
    #   補足: [検査と削除の間の差替えを完全には排除できない。管理rootは同じユーザーの信頼できるprocessだけが書く前提]
    # }
    def remove_object(self, path: Path) -> None:
        if path.is_symlink() or _is_junction(path) or not path.is_dir():
            raise RetentionSafetyError(
                f"deletion target changed before removal: {path}"
            )
        ArtifactTrash(path.parent.parent).discard(
            path,
            kind=KIND_OBJECT,
            sha256=path.name,
            reason=REASON_RETENTION_REMOVE,
        )


# {
#   責務: [RetentionReport: 1回のcollect実行で適用した計画と、実際に削除したArtifactを返す]
#   フィールド: [plan: 適用したRetentionPlan, deleted_artifacts: 実際に削除したdigest（昇順）]
# }
@dataclass(frozen=True)
class RetentionReport:
    plan: RetentionPlan
    deleted_artifacts: tuple[str, ...]


# {
#   責務: [current_time_ms: 実時刻をUNIXエポックmsで返す既定clock]
#   処理: [time.time_nsをmsへ切り捨てる]
#   引数: [なし]
#   戻り値: [int: 現在時刻のUNIXエポックms]
# }
def current_time_ms() -> int:
    return time.time_ns() // 1_000_000


# {
#   責務: [_serialized: Collectorの書込み系メソッドを同じroot lock内で実行する装飾]
#   処理: [coordination_lockを取得してから元のメソッドを呼ぶ。同一threadの再入は許可される]
#   引数: [method: 索引またはStoreを変更するCollectorのメソッド]
#   戻り値: [Callable: lock付きで元のメソッドを呼ぶ同じ署名のメソッド]
# }
def _serialized(method: Callable[..., _Result]) -> Callable[..., _Result]:
    @functools.wraps(method)
    def locked(self: RetentionCollector, *args: Any, **kwargs: Any) -> _Result:
        with self.coordination_lock():
            return method(self, *args, **kwargs)

    return locked


# {
#   責務: [RetentionCollector: Artifact Storeの保持記録を登録し、budget/Protected/TTL/exact dedupのGCを適用する]
#   フィールド: [store: 管理対象のFileSystemArtifactStore, policy: 保持policy,
#     clock: UNIXエポックmsを返す差替可能な時計, file_system: 索引読書きと削除の差替可能な操作,
#     index_path: store root直下の索引ファイル]
#   処理: [register/set_protectedで索引を更新し、collectで計画→索引置換→object削除の順に適用する]
#   補足: [register/set_protected/collectは同じroot lock内で直列化される（Issue #789）。
#     Store.putを直接呼ぶ書込みは対象外で、admissionを使わない呼出しとの競合は防げない。
#     索引・metadataが不正なら何も変更せず停止する]
# }
class RetentionCollector:
    # {
    #   責務: [__init__: 管理rootと依存を固定する]
    #   処理: [時計とFS操作は未指定なら実時刻とローカルdiskを使う]
    #   引数: [store: 対象Store, policy: 保持policy, clock: ms時計, file_system: FS操作]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        store: FileSystemArtifactStore,
        policy: RetentionPolicy,
        *,
        clock: Callable[[], int] = current_time_ms,
        file_system: RetentionFileSystem | None = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.policy = policy
        self.clock = clock
        self.lock_timeout_seconds = lock_timeout_seconds
        self.file_system: RetentionFileSystem = (
            file_system if file_system is not None else LocalRetentionFileSystem()
        )
        self.index_path = Path(store.root) / RETENTION_INDEX_FILENAME

    # {
    #   責務: [load_entries: 保存済み索引を検証して保持記録を返す]
    #   処理: [索引がなければ空、あれば上限付きで読み、契約違反は部分結果なしで拒否する]
    #   引数: [なし]
    #   戻り値: [tuple: entry_id順の保持記録]
    #   エラー: [RetentionIndexError: 索引が破損・契約外・読込不能]
    # }
    def load_entries(self) -> tuple[RetentionEntry, ...]:
        try:
            payload = self.file_system.read_index(
                self.index_path, max_bytes=MAX_RETENTION_INDEX_BYTES
            )
        except (OSError, ValueError) as error:
            raise RetentionIndexError(
                f"retention index cannot be read: {error}"
            ) from error
        if payload is None:
            return ()
        return parse_retention_index(payload, max_entries=self.policy.max_entries)

    # {
    #   責務: [coordination_lock: このStore rootへの書込みを他process・他threadと直列化するlockを取る]
    #   処理: [root単位のOS advisory lockとprocess内mutexを、lock_timeout_seconds以内で取得する。
    #     同一threadの再入は許可される]
    #   引数: [なし]
    #   戻り値: [Iterator[float]: with本体の間lockを保持。値はlock取得にかかったms]
    #   エラー: [RetentionLockTimeoutError: 期限内に取得できない, RetentionRootUnsafeError: rootがlink等]
    # }
    @contextmanager
    def coordination_lock(self) -> Iterator[float]:
        with retention_root_lock(
            Path(self.store.root), timeout_seconds=self.lock_timeout_seconds
        ) as waited_ms:
            yield waited_ms

    # {
    #   責務: [measure: 索引とStore使用量を、metadata検証付きの1つの観測として返す]
    #   処理: [root lock内で索引を読み、索引digestと未索引objectのmetadataを検証して使用量を求める
    #     （collectなどの書込みと競合して不整合な観測にならない）]
    #   引数: [なし]
    #   戻り値: [RetentionSnapshot: 同じ時点の索引記録と使用量]
    #   エラー: [RetentionIndexError: 索引・metadata不正（判定不能のため受け入れ側はfail-closedにする）]
    # }
    @_serialized
    def measure(self) -> RetentionSnapshot:
        entries = self.load_entries()
        return RetentionSnapshot(entries, self._measure_usage(entries))

    # {
    #   責務: [discard_unindexed_object: 索引に登録されていないobjectだけを、安全検査後に削除する]
    #   処理: [1: root lock内で索引を読み直し、digestが登録済みなら何もしない
    #     2: root・shard・objectがsymlink/junctionでなくroot直下の正規配置であることを確認して削除する]
    #   引数: [digest: 削除候補のArtifact SHA-256]
    #   戻り値: [bool: Trueなら削除した。Falseは索引登録済みまたはobjectが存在しない]
    #   エラー: [RetentionIndexError: 索引不正, RetentionSafetyError: 削除先がroot外・symlink,
    #     OSError: 削除失敗]
    # }
    def discard_unindexed_object(self, digest: str) -> bool:
        with self.coordination_lock():
            if any(entry.artifact_sha256 == digest for entry in self.load_entries()):
                return False
            if not os.path.lexists(self.store.object_path(digest)):
                return False
            self.file_system.remove_object(self.safe_object_path(digest))
            return True

    # {
    #   責務: [register: Store内の既存Artifactを参照する保持記録を、容量上限を守って追加する]
    #   処理: [1: 索引とStore metadataを検証し、記録数の上限を確認する
    #     2: put済みArtifactを含むStore使用量がhard limitを超えていないか確認する
    #     3: policyのTTLで期限を決め、索引を原子的に置換する]
    #   引数: [reference: put済みArtifactの参照, entry_id: 一意な記録ID, protected: 削除禁止にするか]
    #   戻り値: [RetentionEntry: 追加した保持記録]
    #   エラー: [RetentionIndexError: 索引・metadata不正またはID重複,
    #     RetentionCapacityError: hard limitまたは件数上限超過, ArtifactIntegrityError: 参照とStoreが不一致]
    # }
    @_serialized
    def register(
        self, reference: ArtifactReference, *, entry_id: str, protected: bool = False
    ) -> RetentionEntry:
        entries = self.load_entries()
        if any(entry.entry_id == entry_id for entry in entries):
            raise RetentionIndexError(f"duplicate retention entry_id: {entry_id}")
        if len(entries) >= self.policy.max_entries:
            raise RetentionCapacityError("retention index entry limit reached")
        metadata = self.store.read_metadata(reference.sha256)
        if (
            metadata.media_type != reference.media_type
            or metadata.sensitive != reference.sensitive
        ):
            raise ArtifactIntegrityError(
                "reference metadata does not match stored metadata"
            )
        # put済みのArtifactは未索引objectとして既に使用量へ含まれるため、合計だけを比較する。
        if self._measure_usage(entries).total_bytes > self.policy.hard_limit_bytes:
            raise RetentionCapacityError("store exceeds the hard limit")
        now_ms = self._now_ms()
        entry = RetentionEntry(
            entry_id=entry_id,
            artifact_sha256=metadata.sha256,
            size_bytes=metadata.size_bytes,
            artifact_type=metadata.artifact_type,
            created_at_ms=now_ms,
            expires_at_ms=self.policy.expiry_for(metadata.artifact_type, now_ms),
            protected=protected,
        )
        self.file_system.write_index(
            self.index_path, retention_index_document((*entries, entry))
        )
        return entry

    # {
    #   責務: [can_admit: 新しいpayloadをStoreへ書く前に、hard limitを超えないか判定する]
    #   処理: [索引とmetadataから現在使用量を求め、既存digestなら追加容量0として比較する]
    #   引数: [size_bytes: 書込み予定payloadのbyte数, sha256: 既知なら書込み予定のdigest]
    #   戻り値: [bool: Trueなら書込み後もhard limit以内。Falseなら書込みを止めるべき]
    #   エラー: [RetentionIndexError: 索引・metadata不正（判定不能のため受け入れない）]
    # }
    def can_admit(self, size_bytes: int, *, sha256: str | None = None) -> bool:
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        entries = self.load_entries()
        usage = self._measure_usage(entries)
        known = {entry.artifact_sha256 for entry in entries} | usage.unindexed_digests
        additional = 0 if sha256 is not None and sha256 in known else size_bytes
        return usage.total_bytes + additional <= self.policy.hard_limit_bytes

    # {
    #   責務: [needs_collection: いまGCを実行すると削除できる記録があるか判定する]
    #   処理: [collectと同じ入力で純粋な計画を作り、外す記録が1件以上あるかを返す。
    #     Protectedや参照中だけで予算超過している状態では、GCで解決しないためFalseになる]
    #   引数: [live_references: Experience/Event Journalなどが参照中のArtifact]
    #   戻り値: [bool: TrueならcollectでGCすべき。容量超過の有無はplanのover_budgetで確認する]
    #   エラー: [RetentionIndexError: 索引・metadata不正, TypeError: 参照の型が不正]
    # }
    def needs_collection(
        self, live_references: Iterable[ArtifactReference] = ()
    ) -> bool:
        return bool(self.plan(live_references).removed_entries)

    # {
    #   責務: [plan: 現在の索引・Store使用量・時刻・外部参照から、副作用なしでGC計画を作る]
    #   処理: [外部参照の型を検査し、索引と全metadataを検証して使用量を求め、plan_retentionへ渡す]
    #   引数: [live_references: Experience/Event Journalなどが参照中のArtifact]
    #   戻り値: [RetentionPlan: collectが適用するのと同じ計画]
    #   エラー: [RetentionIndexError: 索引・metadata不正, TypeError: 参照の型が不正]
    # }
    def plan(self, live_references: Iterable[ArtifactReference] = ()) -> RetentionPlan:
        live = _live_digests(live_references)
        entries = self.load_entries()
        usage = self._measure_usage(entries)
        return plan_retention(
            entries,
            self.policy,
            now_ms=self._now_ms(),
            live_artifact_sha256=live,
            unindexed_bytes=usage.unindexed_bytes,
        )

    # {
    #   責務: [set_protected: 既存の保持記録のProtected状態を切り替える]
    #   処理: [索引を検証し、対象記録だけを置き換えて原子的に保存する]
    #   引数: [entry_id: 対象記録ID, protected: 新しいProtected状態]
    #   戻り値: [RetentionEntry: 更新後の記録]
    #   エラー: [RetentionIndexError: 索引不正または記録が存在しない]
    # }
    @_serialized
    def set_protected(self, entry_id: str, protected: bool) -> RetentionEntry:
        if not isinstance(protected, bool):
            raise RetentionIndexError("protected must be a boolean")
        entries = self.load_entries()
        matches = [entry for entry in entries if entry.entry_id == entry_id]
        if not matches:
            raise RetentionIndexError(f"unknown retention entry_id: {entry_id}")
        current = matches[0]
        updated = RetentionEntry(
            entry_id=current.entry_id,
            artifact_sha256=current.artifact_sha256,
            size_bytes=current.size_bytes,
            artifact_type=current.artifact_type,
            created_at_ms=current.created_at_ms,
            expires_at_ms=current.expires_at_ms,
            protected=protected,
        )
        others = [entry for entry in entries if entry.entry_id != entry_id]
        self.file_system.write_index(
            self.index_path, retention_index_document((*others, updated))
        )
        return updated

    # {
    #   責務: [collect: 現在の索引へRetention計画を適用し、管理root内の不要Artifactだけを削除する]
    #   処理: [1: 索引・全metadata・外部参照を検証し、不正なら何も変えずに停止する
    #     2: 純粋なplan_retentionで計画を作り、削除先が全てroot内の正規object配置か先に検査する
    #     3: 索引を先に原子的に置換し、その後でobjectを削除する（途中停止でも参照切れを作らずorphanだけが残る）]
    #   引数: [live_references: Experience/Event Journalなどが参照中のArtifact]
    #   戻り値: [RetentionReport: 適用した計画と削除したdigest]
    #   エラー: [RetentionIndexError: 索引・metadata不正, RetentionSafetyError: 削除先がroot外・symlink,
    #     OSError: 索引保存または削除失敗]
    # }
    @_serialized
    def collect(
        self, live_references: Iterable[ArtifactReference] = ()
    ) -> RetentionReport:
        plan = self.plan(live_references)
        targets = [self.safe_object_path(digest) for digest in plan.deleted_artifacts]
        if plan.removed_entries:
            self.file_system.write_index(
                self.index_path, retention_index_document(plan.kept_entries)
            )
        for target in targets:
            self.file_system.remove_object(target)
        return RetentionReport(plan, plan.deleted_artifacts)

    # {
    #   責務: [_measure_usage: 索引記録と未索引Artifactを合わせたStoreの使用byteを、metadataから検証付きで求める]
    #   処理: [索引digestのmetadataが存在しsizeが一致することを確認し、
    #     索引にないobjectもmetadata sizeで数える。どれか1つでも不正なら計測を拒否する]
    #   引数: [entries: 検証済みの保持記録]
    #   戻り値: [StoreUsage: 合計byte、未索引byte、未索引digest]
    #   エラー: [RetentionIndexError: metadataの欠落・破損・size不一致]
    # }
    def _measure_usage(self, entries: tuple[RetentionEntry, ...]) -> StoreUsage:
        indexed: dict[str, int] = {}
        references: list[ArtifactReference] = []
        try:
            for entry in entries:
                if entry.artifact_sha256 in indexed:
                    continue
                metadata = self.store.read_metadata(entry.artifact_sha256)
                if metadata.size_bytes != entry.size_bytes:
                    raise RetentionIndexError(
                        f"index size disagrees with stored artifact {entry.artifact_sha256}"
                    )
                indexed[entry.artifact_sha256] = entry.size_bytes
                references.append(metadata.to_reference())
            unindexed = {
                digest: self.store.read_metadata(digest).size_bytes
                for digest in self.store.find_orphans(references)
            }
        except (ArtifactIntegrityError, OSError, ValueError) as error:
            if isinstance(error, RetentionIndexError):
                raise
            raise RetentionIndexError(
                f"artifact metadata is not usable: {error}"
            ) from error
        unindexed_bytes = sum(unindexed.values())
        # trashに残る削除途中のbyteも使用量に含め、残存中にhard limitを超えないようにする（Issue #790）。
        trash_bytes = ArtifactTrash(Path(self.store.root)).residual_bytes()
        return StoreUsage(
            total_bytes=sum(indexed.values()) + unindexed_bytes + trash_bytes,
            unindexed_bytes=unindexed_bytes,
            unindexed_digests=frozenset(unindexed),
            trash_bytes=trash_bytes,
        )

    # {
    #   責務: [safe_object_path: 削除してよい正規のobject directoryだけを返す（Orphan Reconcilerも使うため公開）]
    #   処理: [Store配置からpathを得て、root・shard・objectのどれもsymlinkでなく、
    #     実体pathがroot直下の「先頭2文字/digest」であることを確認する]
    #   引数: [digest: 削除予定のdigest]
    #   戻り値: [Path: root内の検証済みobject directory]
    #   エラー: [RetentionSafetyError: root外・symlink・配置不一致・digest不正]
    # }
    def safe_object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RetentionSafetyError("deletion target is not a SHA-256 digest")
        root = Path(self.store.root)
        candidate = Path(self.store.object_path(digest))
        if root.is_symlink() or _is_junction(root):
            raise RetentionSafetyError("managed root must not be a symlink")
        resolved_root = root.resolve(strict=True)
        expected = resolved_root / digest[:2] / digest
        for path in (candidate.parent, candidate):
            if path.is_symlink() or _is_junction(path):
                raise RetentionSafetyError(
                    f"deletion target passes through a symlink: {path}"
                )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RetentionSafetyError(
                f"deletion target is missing: {digest}"
            ) from error
        if resolved != expected or not resolved.is_relative_to(resolved_root):
            raise RetentionSafetyError(
                f"deletion target escapes the managed root: {digest}"
            )
        if not resolved.is_dir():
            raise RetentionSafetyError(f"deletion target is not a directory: {digest}")
        return resolved

    # {
    #   責務: [_now_ms: 差替可能な時計から判定時刻を取得し、契約外の値を拒否する]
    #   処理: [clockの戻り値がboolでない非負整数か確認する]
    #   引数: [なし]
    #   戻り値: [int: UNIXエポックms]
    #   エラー: [ValueError: clockの値が契約外]
    # }
    def _now_ms(self) -> int:
        value = self.clock()
        if type(value) is not int or value < 0:
            raise ValueError("retention clock must return non-negative integer ms")
        return value


# {
#   責務: [StoreUsage: Store使用量の内訳を計測結果として保持する（admissionも読むため公開）]
#   フィールド: [total_bytes: 索引分・未索引分・trash残存分の合計byte, unindexed_bytes: 索引にないobjectのbyte,
#     unindexed_digests: 索引にないobjectのdigest, trash_bytes: 削除途中でtrashに残るbyte]
# }
@dataclass(frozen=True)
class StoreUsage:
    total_bytes: int
    unindexed_bytes: int
    unindexed_digests: frozenset[str]
    trash_bytes: int = 0


# {
#   責務: [RetentionSnapshot: 同じ時点で観測した索引記録とStore使用量の組を返す]
#   フィールド: [entries: entry_id順の検証済み保持記録, usage: そのときのStore使用量]
# }
@dataclass(frozen=True)
class RetentionSnapshot:
    entries: tuple[RetentionEntry, ...]
    usage: StoreUsage


# {
#   責務: [_live_digests: 外部参照をdigest集合へ変換し、型の誤りを削除前に拒否する]
#   処理: [各要素がArtifactReferenceであることを確認してsha256を集める]
#   引数: [references: 参照中のArtifact]
#   戻り値: [frozenset: 参照中のdigest]
#   エラー: [TypeError: ArtifactReference以外を含む]
# }
def _live_digests(references: Iterable[ArtifactReference]) -> frozenset[str]:
    digests = set()
    for reference in references:
        if not isinstance(reference, ArtifactReference):
            raise TypeError("live references must be ArtifactReference values")
        digests.add(reference.sha256)
    return frozenset(digests)
