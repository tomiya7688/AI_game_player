"""保持記録とpolicyから、削除対象を決定的に選ぶ副作用なしのRetention計画。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ai_game_player.retention_index import RetentionEntry, validate_entries
from ai_game_player.retention_policy import RetentionPolicy

REASON_DUPLICATE = "duplicate"
REASON_EXPIRED = "expired"
REASON_BUDGET = "budget"
BLOCKED_EXPIRED_REFERENCED = "expired_but_referenced"


# {
#   責務: [RemovedEntry: 計画で索引から外す1記録と、その理由を呼出側へ示す]
#   フィールド: [entry_id: 外す記録ID, artifact_sha256: その記録が参照していたArtifact,
#     reason: duplicate/expired/budget のいずれか,
#     alias_of: duplicateの場合に同じ内容を保持し続ける記録ID。それ以外はNone]
# }
@dataclass(frozen=True)
class RemovedEntry:
    entry_id: str
    artifact_sha256: str
    reason: str
    alias_of: str | None = None


# {
#   責務: [BlockedEntry: 削除条件を満たしたが参照整合性のため残した記録を示す]
#   フィールド: [entry_id: 残した記録ID, reason: 残した理由（expired_but_referenced）]
# }
@dataclass(frozen=True)
class BlockedEntry:
    entry_id: str
    reason: str


# {
#   責務: [RetentionPlan: 1回のGC判断の結果を、実行前に検査できる不変値として表す]
#   フィールド: [kept_entries: GC後も索引に残す記録（entry_id順）, removed_entries: 外す記録（決定順）,
#     deleted_artifacts: 物理削除してよいArtifact digest（昇順）, blocked_entries: 参照中で残した記録,
#     bytes_before/bytes_after: GC前後の使用byte（未索引Artifactを含む）,
#     over_budget: GC後もsoft budgetを超える, over_hard_limit: GC後もhard limitを超える]
# }
@dataclass(frozen=True)
class RetentionPlan:
    kept_entries: tuple[RetentionEntry, ...]
    removed_entries: tuple[RemovedEntry, ...]
    deleted_artifacts: tuple[str, ...]
    blocked_entries: tuple[BlockedEntry, ...]
    bytes_before: int
    bytes_after: int
    over_budget: bool
    over_hard_limit: bool


# {
#   責務: [plan_retention: 保持記録をpolicyと時刻だけから評価し、削除してよい記録とArtifactを決める]
#   処理: [1: exact duplicate（同じdigest）を最も長く保持される代表1件へ集約しProtectedは全て残す
#     2: TTL切れの非Protected記録を外す。外部参照中のdigestは残してblockedへ記録する
#     3: soft budget超過時だけ、非Protectedかつ外部非参照の記録を作成が古い順（同時刻はentry_id順）に外す
#     4: どの残存記録・外部参照からも参照されないdigestだけを物理削除対象にする]
#   引数: [entries: 現在の保持記録, policy: 容量とTTLの上限, now_ms: 判定時刻のUNIXエポックms,
#     live_artifact_sha256: Experience/Event Journalなど外部から参照中のdigest,
#     unindexed_bytes: 索引にないが同じroot内で容量を使うArtifactのbyte数（削除対象にしない）]
#   戻り値: [RetentionPlan: 入力が同じなら常に同じ削除順・結果になる計画]
#   エラー: [RetentionIndexError: 保持記録の全体不変条件違反, ValueError: 時刻・byte数が負または整数でない]
#   補足: [ファイルやStoreには触れない。実行はRetentionCollectorが担当する]
# }
def plan_retention(
    entries: Iterable[RetentionEntry],
    policy: RetentionPolicy,
    *,
    now_ms: int,
    live_artifact_sha256: Iterable[str] = (),
    unindexed_bytes: int = 0,
) -> RetentionPlan:
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if type(unindexed_bytes) is not int or unindexed_bytes < 0:
        raise ValueError("unindexed_bytes must be a non-negative integer")
    validated = validate_entries(entries, max_entries=policy.max_entries)
    live = frozenset(live_artifact_sha256)
    bytes_before = unindexed_bytes + _unique_bytes(validated)

    kept, removed = _remove_exact_duplicates(validated)
    kept, expired, blocked = _remove_expired(kept, now_ms, live)
    removed.extend(expired)
    kept, evicted = _evict_for_budget(
        kept, live, max(0, policy.soft_budget_bytes - unindexed_bytes)
    )
    removed.extend(evicted)

    bytes_after = unindexed_bytes + _unique_bytes(kept)
    still_referenced = {entry.artifact_sha256 for entry in kept} | live
    deleted = sorted({item.artifact_sha256 for item in removed} - still_referenced)
    return RetentionPlan(
        kept_entries=tuple(sorted(kept, key=lambda entry: entry.entry_id)),
        removed_entries=tuple(removed),
        deleted_artifacts=tuple(deleted),
        blocked_entries=tuple(blocked),
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        over_budget=bytes_after > policy.soft_budget_bytes,
        over_hard_limit=bytes_after > policy.hard_limit_bytes,
    )


# {
#   責務: [_remove_exact_duplicates: 同じArtifact digestを指す記録を、内容を失わずに集約する]
#   処理: [digestごとにProtectedがあればProtectedを全て残し非Protectedを外す。
#     Protectedがなければ最も長く保持される1件（無期限→期限が遅い→作成が古い→entry_id順）を残し、
#     期限内の重複記録があるのに内容がTTLで失われないようにする。外した記録には残した代表IDを付ける]
#   引数: [entries: entry_id順の検証済み保持記録]
#   戻り値: [tuple: 残す記録の一覧と、duplicate理由で外す記録の一覧（digest順・代表外の古い順）]
# }
def _remove_exact_duplicates(
    entries: tuple[RetentionEntry, ...],
) -> tuple[list[RetentionEntry], list[RemovedEntry]]:
    groups: dict[str, list[RetentionEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.artifact_sha256, []).append(entry)
    kept: list[RetentionEntry] = []
    removed: list[RemovedEntry] = []
    for digest in sorted(groups):
        group = sorted(groups[digest], key=_age_order)
        protected = [entry for entry in group if entry.protected]
        survivors = protected or [min(group, key=_longest_retention_order)]
        survivor_ids = {entry.entry_id for entry in survivors}
        kept.extend(survivors)
        removed.extend(
            RemovedEntry(
                entry.entry_id, digest, REASON_DUPLICATE, survivors[0].entry_id
            )
            for entry in group
            if entry.entry_id not in survivor_ids
        )
    return kept, removed


# {
#   責務: [_remove_expired: TTL期限を過ぎた非Protected記録を外し、参照中のものは残す]
#   処理: [期限が判定時刻以下の非Protected記録を期限順・entry_id順に調べ、
#     外部参照中のdigestならblocked、それ以外はexpired理由で外す]
#   引数: [entries: 重複除去後の記録, now_ms: 判定時刻, live: 外部参照中のdigest]
#   戻り値: [tuple: 残す記録、expired理由で外す記録、参照中で残したblocked記録]
# }
def _remove_expired(
    entries: list[RetentionEntry], now_ms: int, live: frozenset[str]
) -> tuple[list[RetentionEntry], list[RemovedEntry], list[BlockedEntry]]:
    expired = sorted(
        (
            entry
            for entry in entries
            if not entry.protected
            and entry.expires_at_ms is not None
            and entry.expires_at_ms <= now_ms
        ),
        key=lambda entry: (entry.expires_at_ms, entry.entry_id),
    )
    removed: list[RemovedEntry] = []
    blocked: list[BlockedEntry] = []
    removed_ids: set[str] = set()
    for entry in expired:
        if entry.artifact_sha256 in live:
            blocked.append(BlockedEntry(entry.entry_id, BLOCKED_EXPIRED_REFERENCED))
            continue
        removed.append(
            RemovedEntry(entry.entry_id, entry.artifact_sha256, REASON_EXPIRED)
        )
        removed_ids.add(entry.entry_id)
    kept = [entry for entry in entries if entry.entry_id not in removed_ids]
    return kept, removed, blocked


# {
#   責務: [_evict_for_budget: 索引上の使用byteがsoft budget以下になるまで、古い非保護記録を外す]
#   処理: [非Protectedかつ外部非参照の記録を作成が古い順・entry_id順に外し、
#     そのdigestを他の残存記録が参照しない場合だけ解放byteとして数える]
#   引数: [entries: TTL処理後の記録, live: 外部参照中のdigest,
#     indexed_budget_bytes: 未索引分を差し引いた索引記録の許容byte。未索引分だけで予算を超える場合は0]
#   戻り値: [tuple: 残す記録と、budget理由で外す記録（外した順）]
# }
def _evict_for_budget(
    entries: list[RetentionEntry], live: frozenset[str], indexed_budget_bytes: int
) -> tuple[list[RetentionEntry], list[RemovedEntry]]:
    usage = _unique_bytes(entries)
    if usage <= indexed_budget_bytes:
        return entries, []
    references: dict[str, int] = {}
    for entry in entries:
        references[entry.artifact_sha256] = references.get(entry.artifact_sha256, 0) + 1
    candidates = sorted(
        (
            entry
            for entry in entries
            if not entry.protected and entry.artifact_sha256 not in live
        ),
        key=_age_order,
    )
    removed: list[RemovedEntry] = []
    removed_ids: set[str] = set()
    for entry in candidates:
        if usage <= indexed_budget_bytes:
            break
        removed.append(
            RemovedEntry(entry.entry_id, entry.artifact_sha256, REASON_BUDGET)
        )
        removed_ids.add(entry.entry_id)
        references[entry.artifact_sha256] -= 1
        if references[entry.artifact_sha256] == 0:
            usage -= entry.size_bytes
    kept = [entry for entry in entries if entry.entry_id not in removed_ids]
    return kept, removed


# {
#   責務: [_unique_bytes: content-addressed保存の実使用量として、digestごとに1回だけbyte数を数える]
#   処理: [記録をdigestで一意化し、そのsizeを合計する]
#   引数: [entries: 対象の保持記録]
#   戻り値: [int: 一意なArtifactの合計byte]
# }
def _unique_bytes(entries: Iterable[RetentionEntry]) -> int:
    return sum({entry.artifact_sha256: entry.size_bytes for entry in entries}.values())


# {
#   責務: [_age_order: 古い記録を先に並べる決定的な順序キーを返す]
#   処理: [作成時刻、同時刻ならentry_idで比較できる組を作る]
#   引数: [entry: 保持記録]
#   戻り値: [tuple: (created_at_ms, entry_id)]
# }
def _age_order(entry: RetentionEntry) -> tuple[int, str]:
    return entry.created_at_ms, entry.entry_id


# {
#   責務: [_longest_retention_order: 重複集約で、最も長く保持される記録を先に並べる決定的な順序キーを返す]
#   処理: [無期限を最優先し、次に期限が遅い順、同条件なら作成が古い順・entry_id順にする]
#   引数: [entry: 保持記録]
#   戻り値: [tuple: (期限有無, 期限の符号反転, created_at_ms, entry_id)]
# }
def _longest_retention_order(entry: RetentionEntry) -> tuple[int, int, int, str]:
    if entry.expires_at_ms is None:
        return 0, 0, entry.created_at_ms, entry.entry_id
    return 1, -entry.expires_at_ms, entry.created_at_ms, entry.entry_id
