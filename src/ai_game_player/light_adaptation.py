"""操作経路から分離した、有界queueと増分統計だけの軽量更新worker。"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from uuid import uuid4

from ai_game_player.experience import ExperienceEvent, ExperienceProvenance
from ai_game_player.knowledge_builder import KnowledgeBuilder
from ai_game_player.knowledge_builder_policy import KnowledgeBuilderPolicy
from ai_game_player.knowledge_feature_adapter import KnowledgeFeatureAdapter
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.knowledge_snapshot_store import KnowledgeSnapshotStore
from ai_game_player.knowledge_promotion import KnowledgePromotionGate
from ai_game_player.knowledge_light_validation import (
    KnowledgeLightValidationPolicy,
    PreparedKnowledgePublication,
    light_canonical_events,
)
from ai_game_player.learning_identity import LearningIdentityPolicy
from ai_game_player.light_adaptation_budget import (
    LightAdaptationBudget,
    LightResourceSample,
)
from ai_game_player.light_utility import (
    UtilityEstimate,
    UtilityMeasurement,
    update_utility,
)
from ai_game_player.runtime_mode_config import RuntimeOperation, RuntimePhase
from ai_game_player.runtime_mode_controller import (
    RuntimeModeController,
    RuntimeModeTransitionError,
)
from ai_game_player.run_control import ExecutionCancelled
from ai_game_player.knowledge_update_sample import KnowledgeUpdateSample
from ai_game_player.light_pending_store import read_pending_deltas, write_pending_deltas


# {
#   責務: [LightAdaptationMetrics: 有限workerの仕事量と遅延・却下理由を読み取る]
#   フィールド: [enqueued/dropped/updates/published/failures: 累積数,
#     queued: 現在queue数, reason: 最後の固定理由, update_ms/cpu_ms: 直近仕事の実測]
# }
@dataclass(frozen=True, slots=True)
class LightAdaptationMetrics:
    enqueued: int
    dropped: int
    updates: int
    published: int
    failures: int
    queued: int
    reason: str
    update_ms: float
    cpu_ms: float


# {
#   責務: [LightAdaptationRunner: 推論を待たせず有限batchから次のimmutable候補を構築する]
#   フィールド: [mode: #421共通gate, scope: 完全隔離範囲, budget: 有限予算,
#     adapter: cached版のsoft投影, _queue: 有限delta列, _active/_candidate: 不変版,
#     _worker/_stop/_wake: worker寿命, _resource: versioned数値余力, _utility: #409接続口]
# }
class LightAdaptationRunner:
    # {
    #   責務: [__init__: 静的版を読むだけで推論経路と独立したworkerを準備する]
    #   処理: [bounded queue・Builder・cached snapshotを初期化。外部callbackやhistory scanはしない]
    #   引数: [scope: 信頼済みSession scope, root: ローカル保存先, mode: 共通Mode gate,
    #     identity: profileの識別保証, budget: 有限予算, initial_snapshot: 選択済み版, gate: #532公開gate]
    #   戻り値: [なし] エラー: [ValueError: 不整合なscope/profile/予算]
    # }
    def __init__(
        self,
        scope: KnowledgeScope,
        root: Path,
        mode: RuntimeModeController,
        *,
        identity: LearningIdentityPolicy | None = None,
        budget: LightAdaptationBudget | None = None,
        initial_snapshot: KnowledgeSnapshot | None = None,
        gate: KnowledgePromotionGate | None = None,
    ) -> None:
        self.scope = scope
        self.mode = mode
        self.budget = budget or LightAdaptationBudget()
        self.identity = identity or LearningIdentityPolicy(profile_id=scope.profile_id)
        if self.identity.profile_id != scope.profile_id or (
            initial_snapshot is not None and initial_snapshot.scope != scope
        ):
            raise ValueError("light adaptation scope/profile mismatch")
        self.store = KnowledgeSnapshotStore(Path(root) / "snapshots")
        self.gate = gate or KnowledgePromotionGate(
            self.store, Path(root) / "publication"
        )
        if type(self.gate) is not KnowledgePromotionGate:
            raise ValueError("light runner requires the built-in publication gate")
        self.store = self.gate.store
        captured_result = self.gate.capture(scope)
        captured = captured_result.snapshot
        initialization_failed = (
            captured is None and captured_result.reason != "missing_champion"
        )
        self._active = captured or initial_snapshot
        if (
            captured is None
            and not initialization_failed
            and self.mode.permits(RuntimeOperation.KNOWLEDGE_BUILD)
        ):
            try:
                baseline = initial_snapshot or KnowledgeBuilder.empty_snapshot(
                    scope, "static.1", datetime.now(timezone.utc).isoformat()
                )
                self.gate.store.save(baseline)
                self.gate.initialize_champion(scope, baseline.version)
                self._active = self.gate.capture(scope).snapshot
                initialization_failed = self._active is None
            except Exception:
                initialization_failed = True
        self._candidate = self._active
        self._pending: tuple[PreparedKnowledgePublication, int] | None = None
        self._authorized: PreparedKnowledgePublication | None = None
        self._publication_lock = threading.Lock()
        self._queue: Queue[bytes | UtilityMeasurement] = Queue(
            self.budget.queue_capacity
        )
        self._builder = KnowledgeBuilder(KnowledgeBuilderPolicy(max_batch_events=64))
        self._resource = LightResourceSample()
        self._utility: tuple[UtilityEstimate, ...] = ()
        self._waiting_events: tuple[ExperienceEvent, ...] = ()
        self._inflight_events: tuple[ExperienceEvent, ...] = ()
        self._pending_path = Path(root) / "pending" / f"{scope.key}.json"
        self._pending_checkpoint_error = False
        self._pending_recovered = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._session_id = uuid4().hex
        self._sequence = 0
        self._last_publish = -float("inf")
        self._writes: deque[float] = deque(maxlen=self.budget.max_writes_per_minute)
        self._enqueued = self._dropped = self._updates = self._published = (
            self._failures
        ) = 0
        self._reason = (
            "knowledge_initialization_failed" if initialization_failed else "idle"
        )
        self._update_ms = self._cpu_ms = 0.0
        self._cpu_seconds = 0.0
        self._cpu_window = time.monotonic()
        self._oldest_pending = time.monotonic()
        self._checkpoint_failed = initialization_failed
        self._checkpoint_retry = False
        self._last_mode_flush = time.monotonic()
        self.adapter = KnowledgeFeatureAdapter(
            self.capture, lambda _: self.scope, identity_policy=self.identity
        )
        if self._admitted():
            try:
                recovered = read_pending_deltas(self._pending_path, scope)
                self._pending_recovered = self._pending_path.exists()
                for event in recovered:
                    self.enqueue(event)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
                OverflowError,
            ):
                self._reason = "invalid_pending_checkpoint"

    # {
    #   責務: [capture: 次step用の選択版をIOなしで読む]
    #   処理: [完全一致scopeだけに不変参照を返す] 引数: [scope: 現在scope]
    #   戻り値: [KnowledgeSnapshot|None: 現在の最後のvalid版]
    # }
    def capture(self, scope: KnowledgeScope) -> KnowledgeSnapshot | None:
        return self._active if scope == self.scope else None

    # {
    #   責務: [start: 一つだけdaemon workerを起動する]
    #   処理: [二重起動・停止後起動を拒否する] 引数: [なし] 戻り値: [なし]
    #   エラー: [RuntimeError: 停止済みrunner]
    # }
    def start(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("light runner is closed")
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._run, name="light-adaptation", daemon=True
            )
            self._worker.start()

    # {
    #   責務: [set_resources: hostの資源圧をIOなしで次の更新に反映する]
    #   処理: [検証済みの不変数値投影を参照置換する] 引数: [sample: #425接続用投影]
    #   戻り値: [なし] エラー: [ValueError: 未検証型]
    # }
    def set_resources(self, sample: LightResourceSample) -> None:
        if not isinstance(sample, LightResourceSample):
            raise ValueError("light resources require a validated numeric projection")
        self._resource = sample
        self._wake.set()

    # {
    #   責務: [enqueue: canonicalな有限deltaだけを非blockingで受け取る]
    #   処理: [Modeと停止を照合し、payloadの深さ・件数・文字数を先に制限してimmutable bytesへ複写する]
    #   引数: [event: receipt検証済みのExperience] 戻り値: [bool: queue採用]
    # }
    def enqueue(self, event: ExperienceEvent) -> bool:
        if not self._admitted():
            return self._drop("mode_denied")
        try:
            if (
                type(event) is not ExperienceEvent
                or type(event.artifacts) is not tuple
                or event.artifacts
                or type(event.payload) is not dict
                or (
                    event.provenance is not None
                    and type(event.provenance) is not ExperienceProvenance
                )
                or len(event.payload) > 24
            ):
                return self._drop("invalid_delta")
            value = event.to_dict()
            if not _bounded_json(value):
                return self._drop("oversized_delta")
            value = light_canonical_events((event,))[0]
            payload = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            if len(payload) > self.budget.max_event_bytes:
                return self._drop("oversized_delta")
        except (ValueError, TypeError, AttributeError):
            return self._drop("invalid_delta")
        return self._put(payload)

    # {
    #   責務: [record_utility: 数値実測を学習queueへ渡す]
    #   処理: [共通Modeで更新が許可される時だけbounded queueへput_nowaitする]
    #   引数: [sample: versioned数値投影] 戻り値: [bool: 採用されたか]
    # }
    def record_utility(self, sample: UtilityMeasurement) -> bool:
        if not isinstance(sample, UtilityMeasurement) or not self._admitted():
            return self._drop("mode_denied")
        return self._put(sample)

    # {
    #   責務: [metrics: 推論から読み取れるworkerの固定量telemetryを返す]
    #   処理: [historyを走査せず累積値を複写する] 引数: [なし] 戻り値: [LightAdaptationMetrics]
    # }
    @property
    def metrics(self) -> LightAdaptationMetrics:
        return LightAdaptationMetrics(
            self._enqueued,
            self._dropped,
            self._updates,
            self._published,
            self._failures,
            self._queue.qsize(),
            self._reason,
            self._update_ms,
            self._cpu_ms,
        )

    # {
    #   責務: [utility_estimates: #409接続用の数値統計版を取得する]
    #   処理: [immutable tupleを返す。Routerや完全Utility推定を代替しない]
    #   引数: [なし] 戻り値: [tuple: 有界な手法別EWMA]
    # }
    @property
    def utility_estimates(self) -> tuple[UtilityEstimate, ...]:
        return self._utility

    # {
    #   責務: [close: 停止を先にlatchしworkerを期限内に終了する]
    #   処理: [IO/probeを待たずstop event、wake後に有限joinする]
    #   引数: [timeout: join上限秒] 戻り値: [なし] エラー: [RuntimeError: worker終了未確認]
    # }
    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout)
            if self._worker.is_alive():
                raise RuntimeError(
                    "light adaptation worker did not stop within deadline"
                )
        elif self._waiting_events or self._inflight_events or not self._queue.empty():
            self._checkpoint_pending()
        if self._pending_checkpoint_error:
            raise RuntimeError("accepted delta checkpoint failed during shutdown")

    # {
    #   責務: [_admitted: 共通Modeと停止世代を更新開始前に照合する]
    #   処理: [独自のlearning boolを作らず#421 operation gateを使う]
    #   引数: [なし] 戻り値: [bool: 更新開始を許すか]
    # }
    def _admitted(self) -> bool:
        return (
            not self._stop.is_set()
            and not self._checkpoint_failed
            and self.mode.permits(RuntimeOperation.LIGHT_UPDATE)
        )

    # {
    #   責務: [publish_at_boundary: 検証済み候補をMode世代CASで次stepのcached版にする]
    #   処理: [非blocking pointer lockと共通Phase gateを使い、memory交換後だけreceiptを発行する。
    #     IO・replay・checkpointはworkerへ戻す]
    #   引数: [なし] 戻り値: [bool: 今回新しい版をruntimeへ公開したか]
    # }
    def publish_at_boundary(self) -> bool:
        if not self._admitted() or self._resource.reason(self.budget):
            return False
        if not self._publication_lock.acquire(blocking=False):
            return False
        try:
            pending = self._pending
            if pending is None or self._authorized is not None:
                return False
            prepared, expected_epoch = pending
            if self.mode.snapshot.epoch != expected_epoch:
                self.gate.discard_prepared(prepared)
                self._pending = None
                self._reason = "stale_mode_candidate"
                return False
            try:
                state = self.mode.transition(RuntimePhase.EVALUATE)
                self.mode.record_evaluation(
                    prepared.snapshot.version, True, expected_epoch=state.epoch
                )
                state = self.mode.transition(RuntimePhase.PROMOTE)
                old = self._active

                # {
                #   責務: [commit: 短いMode CAS内でcached版と公開receiptだけを交換する]
                #   処理: [pointer成功後にauthorizeし、例外ならpointerを戻す。IOはしない]
                #   引数: [なし] 戻り値: [なし]
                # }
                def commit() -> None:
                    try:
                        self._active = prepared.snapshot
                        prepared.authorize(state.epoch)
                    except Exception:
                        self._active = old
                        raise
                    self._authorized = prepared
                    self._pending = None
                    self._published += 1
                    self._last_publish = time.monotonic()

                self.mode.run_if_allowed(
                    RuntimeOperation.PROMOTION, state.epoch, commit
                )
                self._wake.set()
                return True
            except (RuntimeModeTransitionError, ExecutionCancelled):
                self._reason = "publication_mode_denied"
                if prepared.authorized_epoch is None:
                    self.gate.discard_prepared(prepared)
                    self._pending = None
                return False
            finally:
                if self.mode.snapshot.phase in {
                    RuntimePhase.EVALUATE,
                    RuntimePhase.PROMOTE,
                }:
                    try:
                        self.mode.transition(RuntimePhase.INFER)
                    except (RuntimeModeTransitionError, ExecutionCancelled):
                        pass
        finally:
            self._publication_lock.release()

    # {
    #   責務: [retry_checkpoint: 明示recovery要求でだけ失敗したdurable acknowledgementを再試行する]
    #   処理: [admissionを再開せずworkerをwakeする。成功後にだけfailure latchを解除する]
    #   引数: [なし] 戻り値: [なし]
    # }
    def retry_checkpoint(self) -> None:
        self._checkpoint_retry = True
        self._wake.set()

    # {
    #   責務: [_put: queueが満杯なら操作を待たせずdropする]
    #   処理: [put_nowait後にworkerをwakeする] 引数: [value: 有界不変delta] 戻り値: [bool]
    # }
    def _put(self, value: bytes | UtilityMeasurement) -> bool:
        try:
            if self._queue.empty():
                self._oldest_pending = time.monotonic()
            self._queue.put_nowait(value)
        except Full:
            return self._drop("queue_full")
        self._enqueued += 1
        self._wake.set()
        return True

    # {
    #   責務: [_drop: 固定理由と件数だけを保持する]
    #   処理: [raw例外や画面をtelemetryへ残さない] 引数: [reason: 固定理由] 戻り値: [False]
    # }
    def _drop(self, reason: str) -> bool:
        self._dropped += 1
        self._reason = reason
        return False

    # {
    #   責務: [_run: gameplayと独立に、予算内の増分仕事だけを繰り返す]
    #   処理: [bounded wait、資源admission、batch処理。例外時は最後のvalid版を維持する]
    #   引数: [なし] 戻り値: [なし]
    # }
    def _run(self) -> None:
        try:
            self._run_loop()
        finally:
            self._checkpoint_pending()

    # {
    #   責務: [_checkpoint_pending: 新規学習停止後、受理済みの残りだけを有限退避する]
    #   処理: [最大128件をIDでdedupし一つの事実checkpointへ書く。overflowは明示dropする]
    #   引数: [なし] 戻り値: [なし。失敗latchはcloseへ伝播]
    # }
    def _checkpoint_pending(self) -> None:
        events = {
            event.event_id: event
            for event in (*self._waiting_events, *self._inflight_events)
        }
        for _ in range(self.budget.queue_capacity):
            try:
                value = self._queue.get_nowait()
            except Empty:
                break
            if isinstance(value, bytes):
                event = ExperienceEvent.from_dict(json.loads(value))
                if len(events) < 128:
                    events[event.event_id] = event
                else:
                    self._drop("shutdown_delta_capacity")
        if events or self._pending_recovered:
            try:
                bounded_events = []
                retained = 0
                for event in events.values():
                    size = len(
                        json.dumps(
                            event.to_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    )
                    if retained + size > 1024 * 1024 - 2048:
                        self._drop("shutdown_delta_bytes")
                        continue
                    bounded_events.append(event)
                    retained += size + 1
                champion = self.gate.capture(self.scope).snapshot
                watermarks = (
                    champion.to_dict()["builder_state"] if champion is not None else {}
                )
                dropped = write_pending_deltas(
                    self._pending_path,
                    self.scope,
                    tuple(bounded_events),
                    source_watermarks=watermarks.get("source_watermarks"),
                    correlation_watermarks=watermarks.get("correlation_watermarks"),
                )
                if dropped:
                    self._dropped += dropped
                    self._reason = "shutdown_retention_capacity"
            except (OSError, ValueError):
                self._pending_checkpoint_error = True
                self._failures += 1
                self._reason = "pending_checkpoint_failed"

    # {
    #   責務: [_run_loop: Stopを優先しMode・資源制約内の更新だけを実行する]
    #   処理: [queue待ち、admission、有限batch処理を行う]
    #   引数: [なし] 戻り値: [なし]
    # }
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(min(self.budget.idle_flush_seconds, 0.1))
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._authorized is not None and (
                not self._checkpoint_failed or self._checkpoint_retry
            ):
                checkpoint_cpu = time.thread_time()
                try:
                    result = self.gate.acknowledge_publication(self._authorized)
                    if result.status != "promoted":
                        raise ValueError("publication checkpoint rejected")
                    self._authorized = None
                    self._inflight_events = ()
                    self._checkpoint_failed = False
                except Exception:
                    self._checkpoint_failed = True
                    self._failures += 1
                    self._reason = "publication_checkpoint_failed"
                finally:
                    self._checkpoint_retry = False
                    self._cpu_seconds += time.thread_time() - checkpoint_cpu
                continue
            now = time.monotonic()
            if now - self._last_mode_flush >= 1.0:
                try:
                    self.mode.flush()
                except Exception:
                    self._failures += 1
                    self._reason = "mode_checkpoint_failed"
                    continue
                self._last_mode_flush = now
            reason = self._resource.reason(self.budget)
            if reason or not self._admitted():
                if self._checkpoint_failed:
                    self._reason = (
                        "publication_checkpoint_failed"
                        if self._authorized is not None
                        else "knowledge_initialization_failed"
                    )
                else:
                    self._reason = reason or "mode_denied"
                continue
            elapsed = now - self._cpu_window
            if elapsed >= 1.0:
                self._cpu_seconds = 0.0
                self._cpu_window = now
            if self._cpu_seconds >= self.budget.max_cpu_percent / 100:
                self._reason = "worker_cpu_budget"
                continue
            if self._pending is not None or self._authorized is not None:
                continue
            if now - self._last_publish < self.budget.min_publish_seconds:
                self._reason = "publish_interval"
                continue
            if (
                self._queue.qsize() < self.budget.min_samples
                and now - self._oldest_pending < self.budget.idle_flush_seconds
            ):
                continue
            retained_bytes = self._queue.qsize() * self.budget.max_event_bytes
            retained_bytes += (
                (len(self._waiting_events) + len(self._inflight_events))
                * self.budget.max_event_bytes
                * 4
            )
            if self._active is not None:
                retained_bytes += len(self._active.to_bytes()) * 12
            if retained_bytes > self.budget.max_memory_bytes:
                self._reason = "workspace_memory_budget"
                continue
            started, cpu_started = time.monotonic(), time.thread_time()
            try:
                self._update_batch()
            except Exception:
                self._failures += 1
                self._reason = "update_failed"
            finally:
                self._update_ms = (time.monotonic() - started) * 1000
                self._cpu_ms = (time.thread_time() - cpu_started) * 1000
                self._cpu_seconds += self._cpu_ms / 1000

    # {
    #   責務: [_update_batch: 少数の新eventだけで次の数値候補を作る]
    #   処理: [最大batch件を消費し、時間/Mode再照合後だけ候補を保持する。全履歴を読まない]
    #   引数: [なし] 戻り値: [なし]
    # }
    def _update_batch(self) -> None:
        events = list(self._waiting_events)
        utility = self._utility
        started = time.monotonic()
        epoch = self.mode.snapshot.epoch
        for _ in range(self.budget.batch_size):
            if (time.monotonic() - started) * 1000 >= self.budget.max_update_ms:
                break
            try:
                value = self._queue.get_nowait()
            except Empty:
                break
            if isinstance(value, bytes):
                if len(events) >= 64:
                    events.pop(0)
                    self._drop("evidence_window_full")
                events.append(ExperienceEvent.from_dict(json.loads(value)))
                if _verified_count(events, self._builder.policy) >= max(
                    4, self.budget.min_samples
                ):
                    break
            else:
                utility = update_utility(utility, value)
        if _verified_count(events, self._builder.policy) < max(
            4, self.budget.min_samples
        ):
            self._waiting_events = tuple(events)
            events = []
        else:
            self._waiting_events = tuple(events)
        if not events and utility == self._utility:
            return
        candidate = self._candidate
        if events:
            self._inflight_events = tuple(events)
            self._sequence += 1
            result = self._builder.build(
                self._active,
                events,
                scope=self.scope,
                version=f"light.{self._session_id}.{self._sequence}",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            candidate = result.snapshot
        if (time.monotonic() - started) * 1000 > self.budget.max_update_ms:
            self._reason = "update_time_budget"
            return

        # {
        #   責務: [commit: 数値計算の結果だけを一致するMode世代へ反映する]
        #   処理: [短いmemory代入だけを行い、active版は変更しない]
        #   引数: [なし] 戻り値: [なし]
        # }
        def commit() -> None:
            self._candidate = candidate
            self._utility = utility
            self._updates += 1
            self._reason = "candidate_updated"

        self.mode.run_if_allowed(RuntimeOperation.LIGHT_UPDATE, epoch, commit)
        now = time.monotonic()
        if events and candidate is not None and not self._stop.is_set():
            if now - self._last_publish < self.budget.min_publish_seconds:
                self._reason = "publish_interval"
                return
            while self._writes and now - self._writes[0] >= 60:
                self._writes.popleft()
            # Snapshot object/index, report object and active checkpoint each require
            # bounded filesystem writes. Reserve six writes before any staging IO.
            if len(self._writes) + 6 > self.budget.max_writes_per_minute:
                self._reason = "write_budget"
                return
            self._writes.extend([now] * 6)
            prepared = self.gate.prepare_light_update(
                self.scope,
                tuple(events),
                version=candidate.version,
                created_at=candidate.created_at,
                policy=KnowledgeLightValidationPolicy(
                    max_work_ms=self.budget.max_update_ms
                ),
            )
            if prepared is None:
                self._reason = self.gate.last_prepare_reason
                if self._reason == "insufficient_verified_evidence":
                    self._waiting_events = tuple(events)
                return
            if (time.monotonic() - started) * 1000 > self.budget.max_update_ms:
                self.gate.discard_prepared(prepared)
                self._reason = "update_time_budget"
                return
            if not self._admitted() or self.mode.snapshot.epoch != epoch:
                self.gate.discard_prepared(prepared)
                self._reason = "stale_mode_candidate"
                return
            with self._publication_lock:
                self._pending = (prepared, epoch)
                self._waiting_events = ()


# {
#   責務: [_bounded_json: enqueue前に入力の探索量そのものを有限化する]
#   処理: [深さ5・各object64項目・配列64件・文字512以内・合計512nodeだけ許す]
#   引数: [value: canonical eventの写し] 戻り値: [bool: 有限なJSON構造か]
# }
def _bounded_json(value: object) -> bool:
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > 512 or depth > 5:
            return False
        if type(item) is dict:
            if len(item) > 64 or any(
                not isinstance(k, str) or len(k) > 128 for k in item
            ):
                return False
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, (list, tuple)) and type(item) in (list, tuple):
            if len(item) > 64:
                return False
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            if len(item) > 512:
                return False
        elif item is not None and type(item) not in (bool, int, float):
            return False
    return True


# {
#   責務: [_verified_count: 未知結果を標本閾値へ水増しせずBuilderと同じ証拠条件を数える]
#   処理: [最大64件の型付きprojectionを照合する。統計やpriorはここで変更しない]
#   引数: [events: 有限buffer, policy: 既存Builder方針] 戻り値: [int: verified効果件数]
# }
def _verified_count(
    events: list[ExperienceEvent], policy: KnowledgeBuilderPolicy
) -> int:
    count = 0
    for event in events:
        sample = KnowledgeUpdateSample.from_event(event)
        count += int(
            sample.execution_status == "executed"
            and sample.verified
            and sample.outcome != "unknown"
            and sample.confidence >= policy.minimum_confidence
            and sample.evidence_kind in policy.trusted_evidence_sources
            and sample.provenance_source in policy.trusted_evidence_sources
        )
    return count
