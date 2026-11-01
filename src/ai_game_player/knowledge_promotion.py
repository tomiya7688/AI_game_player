"""KnowledgeのChallenger評価と、次stepで読むChampion版の原子的な公開。"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import threading
from hashlib import sha256
from typing import Callable

from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.cross_process_lock import cross_process_file_lock
from ai_game_player.knowledge_evaluation import (
    KnowledgeEvaluationCase,
    KnowledgeEvaluationRunner,
    _canonical,
)
from ai_game_player.knowledge_promotion_policy import KnowledgePromotionPolicy
from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_light_validation import (
    KnowledgeLightValidationPolicy,
    PreparedKnowledgePublication,
    light_canonical_events,
    MAX_LIGHT_REPORT_BYTES,
)
from ai_game_player.experience import ExperienceEvent
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.knowledge_snapshot_store import (
    KnowledgeReadResult,
    KnowledgeSnapshotStore,
)


MAX_STATE_BYTES = 128 * 1024
MAX_ROLLBACK_VERSIONS = 32
MAX_AUDIT_EVENTS = 128


# {
#   責務: [KnowledgePromotionResult: 昇格・却下・復元の結論と確定した世代を返す]
#   フィールド: [status: initialized/promoted/rejected/rolled_back, reason: 固定の理由コード,
#     generation: pointerと監査の原子的な更新番号, champion_version: 更新後の本番版,
#     report_sha256: 保存したoffline対照評価。評価未実施ならNone]
# }
@dataclass(frozen=True)
class KnowledgePromotionResult:
    status: str
    reason: str
    generation: int
    champion_version: str | None
    report_sha256: str | None = None


# {
#   責務: [KnowledgePromotionGate: 知識本体を変更せず、評価済み保存版の公開先を管理する]
#   フィールド: [store: 不変Snapshot保存先, root: pointer/監査保存先,
#     reports: 評価記録の内容hash保存先, runner: 実判断を対照評価する純粋な実行器]
# }
class KnowledgePromotionGate:
    # {
    #   責務: [__init__: Snapshot本体と昇格制御の保存先を分離する]
    #   処理: [保存先と評価器を保持する。暗黙にChampionを選ばない]
    #   引数: [store: 既存Snapshot Store, root: pointer/履歴/評価記録の保存先]
    #   戻り値: [なし]
    # }
    def __init__(self, store: KnowledgeSnapshotStore, root: Path) -> None:
        self.store = store
        self.root = Path(root)
        self.reports = FileSystemArtifactStore(self.root / "reports")
        self.runner = KnowledgeEvaluationRunner()
        self._prepared: dict[
            str, tuple[PreparedKnowledgePublication, str, str, int, str, str]
        ] = {}
        self._prepared_lock = threading.Lock()
        self._preparing: set[str] = set()
        self.last_prepare_reason = "idle"

    # {
    #   責務: [prepare_light_update: 小さい数値deltaを既存Builderで再構成し、未公開候補として検証する]
    #   処理: [Gate自身が証拠を集計・構造/差分/時間検査する。保存してもactive pointerは変更しない]
    #   引数: [scope: 隔離範囲, events: 最大64件のcanonical証拠, version/created_at: 新版の参照,
    #     policy: 明示的なsoft-prior専用軽量方針]
    #   戻り値: [PreparedKnowledgePublication|None: 合格した未公開候補。自己申告passedは受け取らない]
    #   エラー: [OSError/ValueError: 保存/契約失敗。旧Championを維持する]
    # }
    def prepare_light_update(
        self,
        scope: KnowledgeScope,
        events: tuple[ExperienceEvent, ...],
        *,
        version: str,
        created_at: str,
        policy: KnowledgeLightValidationPolicy | None = None,
    ) -> PreparedKnowledgePublication | None:
        selected = policy or KnowledgeLightValidationPolicy()
        if (
            type(selected) is not KnowledgeLightValidationPolicy
            or not 1 <= len(events) <= 64
        ):
            raise ValueError("invalid bounded light publication request")
        with self._prepared_lock:
            if scope.key in self._preparing or any(
                record[1] == scope.key for record in self._prepared.values()
            ):
                self.last_prepare_reason = "publication_ack_pending"
                return None
            if len(self._prepared) + len(self._preparing) >= 64:
                self.last_prepare_reason = "publication_capacity"
                return None
            self._preparing.add(scope.key)
        try:
            return self._prepare_light_update_reserved(
                scope, events, version, created_at, selected
            )
        finally:
            with self._prepared_lock:
                self._preparing.discard(scope.key)

    # {
    #   責務: [_prepare_light_update_reserved: 一scopeの準備所有権を保持してIOと有限検証を実行する]
    #   処理: [registry lock外で計算・保存し、合格permitの短い登録だけlock内に戻す]
    #   引数: [scope/events/version/created_at/selected: 検証済みの有限要求]
    #   戻り値: [PreparedKnowledgePublication|None: Gate実検証済みのinactive候補]
    # }
    def _prepare_light_update_reserved(
        self,
        scope: KnowledgeScope,
        events: tuple[ExperienceEvent, ...],
        version: str,
        created_at: str,
        selected: KnowledgeLightValidationPolicy,
    ) -> PreparedKnowledgePublication | None:
        started = time.monotonic()
        canonical_events = light_canonical_events(events)
        with cross_process_file_lock(self._lock_path(scope)):
            state = self._read_state(scope)
        current = self.capture(scope)
        if (
            current.snapshot is None
            or state["champion"] is None
            or current.snapshot.sha256 != state["champion"]["sha256"]
        ):
            self.last_prepare_reason = "missing_champion"
            return None
        candidate = (
            KnowledgeBuilder(KnowledgeBuilderPolicy(max_batch_events=64))
            .build(
                current.snapshot,
                events,
                scope=scope,
                version=version,
                created_at=created_at,
            )
            .snapshot
        )
        if candidate is None:
            self.last_prepare_reason = "missing_candidate"
            return None
        candidate = selected.bound_candidate(current.snapshot, candidate)
        work_ms = (time.monotonic() - started) * 1000
        reason = selected.assess(current.snapshot, candidate, work_ms)
        self.last_prepare_reason = reason
        if reason != "accepted":
            return None
        self.store.save(candidate)
        report = self.reports.put(
            _canonical(
                {
                    "schema": "knowledge.light-validation/v1",
                    "policy": asdict(selected),
                    "champion_sha256": current.snapshot.sha256,
                    "challenger_sha256": candidate.sha256,
                    "work_ms": work_ms,
                    "event_count": len(events),
                    "canonical_events": canonical_events,
                    "canonical_batch_sha256": sha256(
                        _canonical(canonical_events)
                    ).hexdigest(),
                    "source_experience_range": candidate.to_dict()[
                        "source_experience_range"
                    ][-8:],
                    "source_ranges_omitted": max(
                        0, len(candidate.to_dict()["source_experience_range"]) - 8
                    ),
                    "reason": reason,
                    "paired_replay_performed": False,
                }
            ),
            media_type="application/json",
            artifact_type="knowledge.light_validation",
            sensitive=False,
        )
        prepared = PreparedKnowledgePublication(
            candidate, state["generation"], current.snapshot.sha256, report
        )
        with self._prepared_lock:
            self._prepared[prepared.nonce] = (
                prepared,
                scope.key,
                candidate.sha256,
                state["generation"],
                current.snapshot.sha256,
                report.sha256,
            )
        return prepared

    # {
    #   責務: [acknowledge_publication: 成功したruntime Mode CASの公開だけを永続checkpointへ反映する]
    #   処理: [Gate所有permit・双方SHA・世代・report・authorizationを再照合しatomic commitする]
    #   引数: [prepared: このGateが発行しcached pointer交換後にauthorizeした候補]
    #   戻り値: [KnowledgePromotionResult: 永続公開の結論。古いpermitでは現版を巻き戻さない]
    #   エラー: [ValueError: 未認証/改変permit, OSError: IO失敗。再試行まで次promotionは禁止]
    # }
    def acknowledge_publication(
        self, prepared: PreparedKnowledgePublication
    ) -> KnowledgePromotionResult:
        with self._prepared_lock:
            record = self._prepared.get(prepared.nonce)
        snapshot = prepared.snapshot
        if (
            record is None
            or record[0] is not prepared
            or type(prepared.authorized_epoch) is not int
            or prepared.authorized_epoch < 0
            or record[1:]
            != (
                snapshot.scope.key,
                snapshot.sha256,
                prepared.generation,
                prepared.champion_sha256,
                prepared.report.sha256,
            )
        ):
            raise ValueError("publication requires an owned authorized permit")
        with cross_process_file_lock(self._lock_path(snapshot.scope)):
            state = self._read_state(snapshot.scope)
            if (
                state["generation"] != prepared.generation
                or state["champion"] is None
                or state["champion"]["sha256"] != prepared.champion_sha256
            ):
                return self._result(state, "rejected", "stale_publication")
            loaded = self.store.read(snapshot.scope, snapshot.version)
            if loaded.snapshot is None or loaded.snapshot.sha256 != snapshot.sha256:
                return self._result(state, "rejected", "invalid_publication_snapshot")
            self.reports.read_bounded(prepared.report, max_bytes=MAX_LIGHT_REPORT_BYTES)
            state["history"] = (state["history"] + [state["champion"]])[
                -MAX_ROLLBACK_VERSIONS:
            ]
            state["champion"] = _reference(loaded)
            result = self._commit(
                snapshot.scope,
                state,
                "promoted",
                "light_validation_v1",
                prepared.report,
            )
        with self._prepared_lock:
            del self._prepared[prepared.nonce]
        return result

    # {
    #   責務: [discard_prepared: Mode切替やStopで不採用になった未公開候補だけを失効する]
    #   処理: [非認証のGate所有permitをmemory registryから除く。保存Artifactはactiveにならない]
    #   引数: [prepared: 未公開候補] 戻り値: [なし] エラー: [ValueError: 公開済みpermit]
    # }
    def discard_prepared(self, prepared: PreparedKnowledgePublication) -> None:
        if prepared.authorized_epoch is not None:
            raise ValueError("authorized publication requires checkpoint recovery")
        with self._prepared_lock:
            record = self._prepared.get(prepared.nonce)
            if record is not None and record[0] is prepared:
                del self._prepared[prepared.nonce]

    # {
    #   責務: [initialize_champion: 利用者・構成側が選んだ最初の保存版だけを登録する]
    #   処理: [保存版を検証し、未初期化のscopeだけを世代1として公開する]
    #   引数: [scope: 完全一致の隔離範囲, version: 初期の静的または検証済み保存版]
    #   戻り値: [KnowledgePromotionResult: initializedまたは既存Championを維持したrejected]
    #   エラー: [OSError/ValueError: 永続化失敗や不正なpointer。既存状態は上書きしない]
    # }
    def initialize_champion(
        self, scope: KnowledgeScope, version: str
    ) -> KnowledgePromotionResult:
        loaded = self.store.read(scope, version)
        if loaded.snapshot is None:
            return KnowledgePromotionResult(
                "rejected", "invalid_initial_snapshot", 0, None
            )
        with cross_process_file_lock(self._lock_path(scope)):
            state = self._read_state(scope)
            if state["champion"] is not None:
                return self._result(state, "rejected", "already_initialized")
            state["champion"] = _reference(loaded)
            return self._commit(scope, state, "initialized", "initial_baseline", None)

    # {
    #   責務: [capture: 次の1stepで一貫して使う不変Championを取得する]
    #   処理: [原子的なpointerを読み保存版とdigestを照合する。公開後も既に返した値は変化しない]
    #   引数: [scope: 完全一致のゲーム・場面・Player範囲]
    #   戻り値: [KnowledgeReadResult: 欠落/破損/期限切れではSnapshot無しのstatic fallback]
    # }
    def capture(self, scope: KnowledgeScope) -> KnowledgeReadResult:
        try:
            state = self._read_state(scope)
            pointer = state["champion"]
            if pointer is None:
                return KnowledgeReadResult(None, None, "missing_champion")
            loaded = self.store.read(scope, pointer["version"])
            if loaded.snapshot is None or loaded.snapshot.sha256 != pointer["sha256"]:
                return KnowledgeReadResult(None, None, "invalid_champion")
            return loaded
        except (OSError, ValueError, TypeError, KeyError):
            return KnowledgeReadResult(None, None, "invalid_promotion_state")

    # {
    #   責務: [evaluate_and_promote: Gate自身が比較を実行し、条件を満たす新しい保存版だけを公開する]
    #   処理: [Champion世代を固定してlock外で対照評価し、測定を保存する。
    #     lock内で世代を再照合し、合格時だけpointerを変更する。外部passedフラグは受け取らない]
    #   引数: [scope: 隔離範囲, challenger_version: 保存済み更新候補, cases: 根拠付き対照ケース,
    #     provider_factory: Snapshotから実Providerを作る信頼済みアプリ関数, policy: 成果と資源の条件]
    #   戻り値: [KnowledgePromotionResult: promotedまたは理由付きrejected。却下時はChampionを維持]
    #   エラー: [OSError/ValueError: 状態ファイルの破損・永続化失敗。既存pointerを維持する]
    # }
    def evaluate_and_promote(
        self,
        scope: KnowledgeScope,
        challenger_version: str,
        cases: tuple[KnowledgeEvaluationCase, ...],
        provider_factory: Callable,
        policy: KnowledgePromotionPolicy | None = None,
    ) -> KnowledgePromotionResult:
        if policy is not None and type(policy) is not KnowledgePromotionPolicy:
            raise ValueError("knowledge promotion requires a validated policy value")
        with cross_process_file_lock(self._lock_path(scope)):
            initial = self._read_state(scope)
        champion_pointer = initial["champion"]
        if champion_pointer is None:
            return self._record_rejection(scope, "missing_champion")
        champion = self.store.read(scope, champion_pointer["version"])
        challenger = self.store.read(scope, challenger_version)
        if (
            champion.snapshot is None
            or champion.snapshot.sha256 != champion_pointer["sha256"]
        ):
            return self._record_rejection(scope, "invalid_champion")
        if challenger.snapshot is None:
            return self._record_rejection(scope, "invalid_challenger")
        report_reference = None
        selected_policy = policy or KnowledgePromotionPolicy()
        try:
            report = self.runner.run(
                champion.snapshot, challenger.snapshot, cases, provider_factory
            )
            report_payload = report.to_dict()
            report_payload["measurement_sha256"] = report.sha256
            report_payload["policy"] = {"schema_version": 1, **asdict(selected_policy)}
            report_reference = self.reports.put(
                _canonical(report_payload),
                media_type="application/json",
                artifact_type="knowledge.evaluation",
                sensitive=False,
            )
            reason = selected_policy.assess(
                champion.snapshot, challenger.snapshot, report
            )
        except (ValueError, TypeError, KeyError):
            reason = "invalid_evaluation"
        with cross_process_file_lock(self._lock_path(scope)):
            current = self._read_state(scope)
            if (
                current["generation"] != initial["generation"]
                or current["champion"] != champion_pointer
            ):
                return self._commit(
                    scope, current, "rejected", "stale_evaluation", report_reference
                )
            if reason != "accepted":
                return self._commit(
                    scope, current, "rejected", reason, report_reference
                )
            # Recheck expiry/integrity after potentially long evaluation.
            final_champion = self.store.read(scope, champion_pointer["version"])
            if (
                final_champion.snapshot is None
                or final_champion.snapshot.sha256 != champion.snapshot.sha256
            ):
                return self._commit(
                    scope, current, "rejected", "champion_changed", report_reference
                )
            final = self.store.read(scope, challenger_version)
            if (
                final.snapshot is None
                or final.snapshot.sha256 != challenger.snapshot.sha256
            ):
                return self._commit(
                    scope, current, "rejected", "challenger_changed", report_reference
                )
            current["history"] = (current["history"] + [current["champion"]])[
                -MAX_ROLLBACK_VERSIONS:
            ]
            current["champion"] = _reference(final)
            return self._commit(scope, current, "promoted", reason, report_reference)

    # {
    #   責務: [rollback: 履歴にある同scopeのvalid保存版へ公開先を戻す]
    #   処理: [復元対象のdigest・期限を検証し、途中失敗では現在のChampionを変更しない]
    #   引数: [scope: 隔離範囲, version: 明示した過去版。Noneなら直前の保存版]
    #   戻り値: [KnowledgePromotionResult: rolled_backまたは理由付きrejected]
    #   エラー: [OSError/ValueError: 永続化失敗や状態ファイル破損]
    # }
    def rollback(
        self, scope: KnowledgeScope, version: str | None = None
    ) -> KnowledgePromotionResult:
        with cross_process_file_lock(self._lock_path(scope)):
            state = self._read_state(scope)
            history = state["history"]
            target = next(
                (
                    item
                    for item in reversed(history)
                    if version is None or item["version"] == version
                ),
                None,
            )
            if target is None:
                return self._commit(
                    scope, state, "rejected", "rollback_version_unavailable", None
                )
            loaded = self.store.read(scope, target["version"])
            if loaded.snapshot is None or loaded.snapshot.sha256 != target["sha256"]:
                return self._commit(
                    scope, state, "rejected", "invalid_rollback_snapshot", None
                )
            if state["champion"] is not None:
                state["history"] = (history + [state["champion"]])[
                    -MAX_ROLLBACK_VERSIONS:
                ]
            state["champion"] = target
            return self._commit(
                scope, state, "rolled_back", "validated_previous_snapshot", None
            )

    # {
    #   責務: [audit: pointerと同じtransactionで記録された最近128件の監査を返す]
    #   処理: [保存された監査辞書の独立コピーを返す]
    #   引数: [scope: 対象の隔離範囲]
    #   戻り値: [tuple: 操作、理由、世代、対象版、評価digestの記録]
    #   エラー: [ValueError/OSError: 状態ファイル不正または読取失敗]
    # }
    def audit(self, scope: KnowledgeScope) -> tuple[dict, ...]:
        return tuple(self._read_state(scope)["audit"])

    # {
    #   責務: [_record_rejection: 評価開始前の却下もChampionを変えず監査する]
    #   処理: [lockを取得し現行世代へ固定理由を追記する]
    #   引数: [scope: 対象scope, reason: 却下理由]
    #   戻り値: [KnowledgePromotionResult: rejected]
    # }
    def _record_rejection(
        self, scope: KnowledgeScope, reason: str
    ) -> KnowledgePromotionResult:
        with cross_process_file_lock(self._lock_path(scope)):
            return self._commit(
                scope, self._read_state(scope), "rejected", reason, None
            )

    # {
    #   責務: [_read_state: 有界の原子的pointer記録を検証して読む]
    #   処理: [通常ファイルだけを容量上限付きで読み、未作成は初期状態、破損・別scopeは拒否する]
    #   引数: [scope: 期待するscope]
    #   戻り値: [dict: 独立したmutable transaction作業コピー]
    #   エラー: [ValueError/OSError: 不正または読取失敗]
    # }
    def _read_state(self, scope: KnowledgeScope) -> dict:
        path = self.root / scope.key / "state.json"
        try:
            payload = read_regular_file(path, max_bytes=MAX_STATE_BYTES)
        except FileNotFoundError:
            return {
                "schema_version": 1,
                "scope": scope.to_dict(),
                "generation": 0,
                "champion": None,
                "history": [],
                "audit": [],
            }
        state = json.loads(payload, object_pairs_hook=_unique_object)
        _validate_state(state, scope)
        return state

    # {
    #   責務: [_commit: pointer、履歴、監査、世代を一つの原子的置換で確定する]
    #   処理: [監査を有界化し、同期済み一時JSONからpublishする。失敗時は旧ファイルを保つ]
    #   引数: [scope/state: lock内の作業状態, status/reason: 結論, report: 任意の保存済み評価参照]
    #   戻り値: [KnowledgePromotionResult: 永続化した結論]
    #   エラー: [OSError/ValueError: 保存失敗。公開前の状態に留まる]
    # }
    def _commit(
        self, scope: KnowledgeScope, state: dict, status: str, reason: str, report
    ) -> KnowledgePromotionResult:
        state["generation"] += 1
        record = {
            "generation": state["generation"],
            "status": status,
            "reason": reason,
            "champion": state["champion"],
            "report_sha256": report.sha256 if report else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["audit"] = (state["audit"] + [record])[-MAX_AUDIT_EVENTS:]
        _validate_state(state, scope)
        staged = stage_json_write(self.root / scope.key / "state.json", state)
        try:
            staged.publish()
        finally:
            try:
                staged.discard()
            except OSError:
                pass
        return self._result(state, status, reason, report.sha256 if report else None)

    # {
    #   責務: [_lock_path: 同scopeのprocess間更新を直列化するlock場所を得る]
    #   処理: [不透明scope digest配下の固定名を返す]
    #   引数: [scope: 更新対象]
    #   戻り値: [Path: lockファイル]
    # }
    def _lock_path(self, scope: KnowledgeScope) -> Path:
        return self.root / scope.key / "state.lock"

    # {
    #   責務: [_result: 確定状態から呼出側向けの結論を作る]
    #   処理: [現行版と世代を抜き出す]
    #   引数: [state: 保存状態, status/reason: 操作結論, report_digest: 任意の評価digest]
    #   戻り値: [KnowledgePromotionResult: 不変の操作結果]
    # }
    @staticmethod
    def _result(
        state: dict, status: str, reason: str, report_digest: str | None = None
    ) -> KnowledgePromotionResult:
        pointer = state["champion"]
        return KnowledgePromotionResult(
            status,
            reason,
            state["generation"],
            pointer["version"] if pointer else None,
            report_digest,
        )


# {
#   責務: [_reference: 検証済み保存版だけからpointer用の小さい参照を作る]
#   処理: [版と内容digestをコピーする]
#   引数: [loaded: Store.read成功結果]
#   戻り値: [dict: version/sha256の二要素]
# }
def _reference(loaded: KnowledgeReadResult) -> dict[str, str]:
    return {"version": loaded.snapshot.version, "sha256": loaded.snapshot.sha256}


# {
#   責務: [_validate_state: 外部ファイルをpointer更新へ使う前に契約を照合する]
#   処理: [scope、型、件数、digestと世代を検証し余分なデータを拒否する]
#   引数: [state: JSONから読んだ値, scope: 期待する隔離範囲]
#   戻り値: [なし]
#   エラー: [ValueError: 契約外の値]
# }
def _validate_state(state: object, scope: KnowledgeScope) -> None:
    fields = {"schema_version", "scope", "generation", "champion", "history", "audit"}
    if (
        not isinstance(state, dict)
        or set(state) != fields
        or type(state["schema_version"]) is not int
        or state["schema_version"] != 1
    ):
        raise ValueError("invalid knowledge promotion state schema")
    if (
        state["scope"] != scope.to_dict()
        or type(state["generation"]) is not int
        or not 0 <= state["generation"] <= 1_000_000_000_000
    ):
        raise ValueError("invalid knowledge promotion scope/generation")
    if (
        not isinstance(state["history"], list)
        or len(state["history"]) > MAX_ROLLBACK_VERSIONS
    ):
        raise ValueError("invalid knowledge rollback history")
    if not isinstance(state["audit"], list) or len(state["audit"]) > MAX_AUDIT_EVENTS:
        raise ValueError("invalid knowledge audit history")
    pointers = state["history"] + (
        [state["champion"]] if state["champion"] is not None else []
    )
    previous_generation = 0
    for record in state["audit"]:
        if not isinstance(record, dict) or set(record) != {
            "generation",
            "status",
            "reason",
            "champion",
            "report_sha256",
            "created_at",
        }:
            raise ValueError("invalid knowledge audit record")
        if (
            type(record["generation"]) is not int
            or not previous_generation < record["generation"] <= state["generation"]
        ):
            raise ValueError("invalid knowledge audit generation")
        previous_generation = record["generation"]
        if record["status"] not in {
            "initialized",
            "promoted",
            "rejected",
            "rolled_back",
        }:
            raise ValueError("invalid knowledge audit status")
        validate_identifier(record["reason"], "reason")
        if record["champion"] is not None:
            pointers.append(record["champion"])
        if record["report_sha256"] is not None:
            _validate_digest(record["report_sha256"])
        try:
            timestamp = datetime.fromisoformat(
                record["created_at"].replace("Z", "+00:00")
            )
            if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
                raise ValueError("audit timestamp is not UTC")
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("invalid knowledge audit timestamp") from error
    if previous_generation != state["generation"]:
        raise ValueError("knowledge audit does not match pointer generation")
    if state["audit"] and state["audit"][-1]["champion"] != state["champion"]:
        raise ValueError("knowledge audit does not match champion pointer")
    for pointer in pointers:
        if not isinstance(pointer, dict) or set(pointer) != {"version", "sha256"}:
            raise ValueError("invalid knowledge champion reference")
        validate_identifier(pointer["version"], "version")
        _validate_digest(pointer["sha256"])
    if len(_canonical(state)) > MAX_STATE_BYTES:
        raise ValueError("knowledge promotion state exceeds byte budget")


# {
#   責務: [_validate_digest: pointerと評価参照のdigest表記を制限する]
#   処理: [64文字の小文字hex以外を拒否する]
#   引数: [value: 外部ファイルから読んだ参照候補]
#   戻り値: [なし]
#   エラー: [ValueError: digest形式が不正]
# }
def _validate_digest(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("invalid knowledge reference digest")


# {
#   責務: [_unique_object: pointer JSONの重複キーを曖昧に解釈しない]
#   処理: [同じキーを二度読んだ場合は記録全体を拒否する]
#   引数: [pairs: JSON decoderが渡す順序付きキーと値]
#   戻り値: [dict: 重複がないobject]
#   エラー: [ValueError: 同一キーが重複]
# }
def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("knowledge promotion JSON contains duplicate keys")
        result[key] = value
    return result
