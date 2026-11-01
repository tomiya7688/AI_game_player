"""Knowledgeの不変版を既存Artifact Storeへ保存し、安全な読取結果を返す。"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from ai_game_player.artifact_store import (
    ArtifactIntegrityError,
    FileSystemArtifactStore,
)
from ai_game_player.atomic_json import stage_json_write
from ai_game_player.cross_process_lock import cross_process_file_lock
from ai_game_player.experience import ArtifactReference
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.knowledge_snapshot import (
    KnowledgeSnapshot,
    MAX_KNOWLEDGE_BYTES,
    _unique_object,
    _timestamp,
)


KNOWLEDGE_MEDIA_TYPE = "application/vnd.kadoka.knowledge+json"
MAX_INDEX_BYTES = 4096


# {
#   責務: [KnowledgeReadResult: 有効Snapshotかstatic defaultへ戻す理由を明示する]
#   フィールド: [snapshot: 読取成功時だけ存在する不変Snapshot,
#     reference: 整合性を検証したArtifact参照。失敗時はNone,
#     reason: ok/missing/invalid/expired/scope_mismatchの固定理由]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeReadResult:
    snapshot: KnowledgeSnapshot | None
    reference: ArtifactReference | None
    reason: str


# {
#   責務: [KnowledgeSnapshotStore: scopeとversionで隔離したKnowledgeを不変Artifactとして管理する]
#   フィールド: [root: 参照indexの保存先, artifacts: 既存の内容hash付きArtifact Store]
# }
class KnowledgeSnapshotStore:
    # {
    #   責務: [__init__: Knowledge用indexとArtifact格納先を選ぶ]
    #   処理: [既存FileSystemArtifactStoreを再利用し、初期化だけでは保存や昇格を行わない]
    #   引数: [root: このKnowledge Storeの専用ローカル保存先]
    #   戻り値: [なし]
    # }
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.artifacts = FileSystemArtifactStore(self.root / "objects")

    # {
    #   責務: [save: Snapshotを内容hash付きArtifactとscope/version indexとして保存する]
    #   処理: [上限付き検証済みbytesを保存し、版ごとのlock内でindexを原子的公開する。同じ版は内容一致時だけ再利用する]
    #   引数: [snapshot: 検証済みの不変Knowledge。active版の自動昇格は行わない]
    #   戻り値: [ArtifactReference: 保存したSnapshotの内容SHA付き参照]
    #   エラー: [ValueError: Snapshot型が不正または既存版との衝突, OSError/TimeoutError: 保存やlock失敗]
    # }
    def save(self, snapshot: KnowledgeSnapshot) -> ArtifactReference:
        if not isinstance(snapshot, KnowledgeSnapshot):
            raise ValueError("knowledge save requires a validated snapshot")
        reference = ArtifactReference(
            f"sha256.{snapshot.sha256}", snapshot.sha256, KNOWLEDGE_MEDIA_TYPE, False
        )
        index_path = self._index_path(snapshot.scope, snapshot.version)
        with cross_process_file_lock(index_path.with_suffix(".lock")):
            if index_path.exists():
                existing = self.read(
                    snapshot.scope,
                    snapshot.version,
                    now=_timestamp(snapshot.created_at),
                )
                if existing.reference == reference and existing.snapshot is not None:
                    return reference
                raise ValueError(
                    "knowledge version is immutable or its existing index is invalid"
                )
            reference = self.artifacts.put(
                snapshot.to_bytes(),
                media_type=KNOWLEDGE_MEDIA_TYPE,
                artifact_type="knowledge.snapshot",
            )
            index = {
                "schema_version": 1,
                "scope": snapshot.scope.to_dict(),
                "version": snapshot.version,
                "reference": {
                    "artifact_id": reference.artifact_id,
                    "sha256": reference.sha256,
                    "media_type": reference.media_type,
                    "sensitive": reference.sensitive,
                },
            }
            pending = stage_json_write(index_path, index)
            try:
                pending.publish()
            finally:
                pending.discard()
        return reference

    # {
    #   責務: [read: 正しいscope・version・内容・期限を満たすKnowledgeだけを呼出側へ渡す]
    #   処理: [有界index読込、既存Artifactの有界整合性検査、Snapshot契約と全scope照合を行う。失敗時は静的初期値へ戻す結果を返す]
    #   引数: [scope: 呼出側が期待する全5要素の隔離範囲, version: 読取対象の版,
    #     now: 任意のUTC比較時刻。期限判定の再現試験に利用する]
    #   戻り値: [KnowledgeReadResult: 成功はreason=ok、失敗はsnapshot/reference=Noneと固定理由]
    #   エラー: [ValueError: 呼出側が渡したscope/version自体が契約外]
    # }
    def read(
        self, scope: KnowledgeScope, version: str, *, now: datetime | None = None
    ) -> KnowledgeReadResult:
        path = self._index_path(scope, version)
        try:
            if path.is_symlink():
                return KnowledgeReadResult(None, None, "invalid")
            with path.open("rb") as stream:
                payload = stream.read(MAX_INDEX_BYTES + 1)
            if len(payload) > MAX_INDEX_BYTES:
                return KnowledgeReadResult(None, None, "invalid")
            index = json.loads(payload, object_pairs_hook=_unique_object)
            if not isinstance(index, dict) or set(index) != {
                "schema_version",
                "scope",
                "version",
                "reference",
            }:
                return KnowledgeReadResult(None, None, "invalid")
            if type(index["schema_version"]) is not int or index["schema_version"] != 1:
                return KnowledgeReadResult(None, None, "invalid")
            if KnowledgeScope.from_dict(index["scope"]) != scope:
                return KnowledgeReadResult(None, None, "scope_mismatch")
            if index["version"] != version:
                return KnowledgeReadResult(None, None, "invalid")
            raw_reference = index["reference"]
            if not isinstance(raw_reference, dict) or set(raw_reference) != {
                "artifact_id",
                "sha256",
                "media_type",
                "sensitive",
            }:
                return KnowledgeReadResult(None, None, "invalid")
            reference = ArtifactReference(**raw_reference)
            if (
                reference.media_type != KNOWLEDGE_MEDIA_TYPE
                or reference.sensitive
                or reference.artifact_id != f"sha256.{reference.sha256}"
            ):
                return KnowledgeReadResult(None, None, "invalid")
            snapshot = KnowledgeSnapshot(
                self.artifacts.read_bounded(reference, max_bytes=MAX_KNOWLEDGE_BYTES)
            )
            if snapshot.scope != scope:
                return KnowledgeReadResult(None, None, "scope_mismatch")
            if snapshot.version != version or snapshot.sha256 != reference.sha256:
                return KnowledgeReadResult(None, None, "invalid")
            if snapshot.is_expired(now):
                return KnowledgeReadResult(None, None, "expired")
            return KnowledgeReadResult(snapshot, reference, "ok")
        except FileNotFoundError:
            return KnowledgeReadResult(None, None, "missing")
        except (
            ArtifactIntegrityError,
            OSError,
            ValueError,
            TypeError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            # Keep potentially sensitive payload text and filesystem details out of diagnostics.
            return KnowledgeReadResult(None, None, "invalid")

    # {
    #   責務: [_index_path: 全scopeと版を固定長hashにし、path traversalや他scopeとの混在を防ぐ]
    #   処理: [scope型と版の識別子を検証してindexの保存先を作る]
    #   引数: [scope: 全5識別子の隔離範囲, version: bounded opaque版識別子]
    #   戻り値: [Path: root以下のscope/version固有JSON path]
    #   エラー: [ValueError: scope型またはversionが不正]
    # }
    def _index_path(self, scope: KnowledgeScope, version: str) -> Path:
        if not isinstance(scope, KnowledgeScope):
            raise ValueError("knowledge read requires a validated scope")
        validate_identifier(version, "version")
        version_key = sha256(version.encode()).hexdigest()
        return self.root / "snapshots" / scope.key / f"{version_key}.json"
