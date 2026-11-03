"""Pipeline stage境界でread/modify/inject Hookを決定論的な順序・予算・失敗時方針で実行するin-process runtime。"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ai_game_player.hook_dispatch_result import (
    HOOK_STATUS_COMPLETED,
    HOOK_STATUS_FAILED,
    HOOK_STATUS_QUARANTINED,
    HOOK_STATUS_TIMEOUT,
    HookDispatchResult,
    HookExecutionRecord,
)
from ai_game_player.hook_registration import (
    HookFailurePolicy,
    HookMode,
    HookRegistration,
)
from ai_game_player.stage_envelope import (
    CORE_STAGE_IDS,
    StageEnvelope,
    StageEnvelopeError,
)
from ai_game_player.stage_envelope_codec import (
    decode_stage_envelope,
    encode_stage_envelope,
)


_LOGGER = logging.getLogger(__name__)
# Safety判定と実入力の境界。HookはこのstageのEnvelopeを観測だけでき、作成・置換はできない。
HOOK_READ_ONLY_STAGES = frozenset({"safety", "execution"})
_EXTENSION_STAGE_PATTERN = re.compile(r"ext\.[a-z][a-z0-9_.-]{0,123}")
# modify → read → inject の順。readは後段へ渡る最終Envelopeを、injectは加工後の値を基に派生する。
_MODE_ORDER = (HookMode.MODIFY, HookMode.READ, HookMode.INJECT)
HOOK_RUNTIME_MAX_INJECTED_TOTAL = 64

HookAuthorizer = Callable[[HookRegistration], None]


# {
#   責務: [_HookContractViolation: Hook出力がruntimeのprovenance・境界規則に違反したことを内部で伝える]
#   フィールド: [なし: 理由はValueErrorのmessageだけで運ぶ]
# }
class _HookContractViolation(ValueError):
    pass


# {
#   責務: [_HookState: 1つの登録Hookの連続失敗数とquarantine状態を保持する]
#   フィールド: [registration: 登録内容, consecutive_failures: 直近の連続失敗数, quarantined: 以後呼び出さないか]
# }
@dataclass
class _HookState:
    registration: HookRegistration
    consecutive_failures: int = 0
    quarantined: bool = False


# {
#   責務: [_InvocationOutcome: 1回のcallback呼び出しの戻り値・例外・timeout・所要時間を保持する]
#   フィールド: [value: 戻り値, error: 送出された例外, timed_out: 期限内に終わらなかったか, elapsed_ns: 所要時間, done: callbackの終了通知]
# }
@dataclass
class _InvocationOutcome:
    value: Any = None
    error: BaseException | None = None
    timed_out: bool = False
    elapsed_ns: int = 0
    done: threading.Event = field(default_factory=threading.Event)


# {
#   責務: [HookRuntime: 宣言済みstage境界へ登録されたHookを、決定論的な順序・Hookごとの時間/容量予算・失敗時方針に従って実行する]
#   フィールド: [
#     enabled: Falseならdispatchは何も呼ばず入力をそのまま返す（既定False）
#     _points: (stage, boundary)ごとに許可するHookMode
#     _hooks: hook_idごとの登録・失敗状態
#     _authorizer: 登録時に呼ぶ権限判定（#152 Capability Manifestの接続点。既定は無判定）
#     _clock_ns: latency計測用の単調時計
#     _max_consecutive_failures: 連続失敗でquarantineする回数
#     _max_injected_total: 1回のdispatchで追加できる派生Envelopeの総数
#   ]
#   補足: [dispatchは1つのPipeline threadから呼ぶ前提。同一process・同一権限で実行する。timeoutしたHookのthreadは停止できず、CPU・memory・file・network・入力APIへのアクセスも隔離しない。
#     隔離はsubprocess/IPC runtime（#235）、権限はCapability（#152）の範囲]
# }
class HookRuntime:
    # {
    #   責務: [__init__: 無効状態のruntimeを作り、時計と失敗上限を固定する]
    #   処理: [失敗上限と派生件数上限の範囲を確認し、空の接続点表・登録表を作る]
    #   引数: [enabled: Trueのときだけdispatchで登録Hookを呼ぶ, authorizer: 登録ごとに呼び、拒否時は例外を送出する関数,
    #     clock_ns: latency計測用の単調時計（ns）, max_consecutive_failures: この回数連続で失敗したHookをquarantineする,
    #     max_injected_total: 1回のdispatchで受け入れる派生Envelopeの総数上限]
    #   戻り値: [なし]
    #   エラー: [ValueError: 上限値が範囲外の場合]
    # }
    def __init__(
        self,
        *,
        enabled: bool = False,
        authorizer: HookAuthorizer | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        max_consecutive_failures: int = 3,
        max_injected_total: int = 16,
    ) -> None:
        if type(max_consecutive_failures) is not int or not (
            1 <= max_consecutive_failures <= 100
        ):
            raise ValueError("max_consecutive_failures must be in [1, 100]")
        if type(max_injected_total) is not int or not (
            0 <= max_injected_total <= HOOK_RUNTIME_MAX_INJECTED_TOTAL
        ):
            raise ValueError(
                f"max_injected_total must be in [0, {HOOK_RUNTIME_MAX_INJECTED_TOTAL}]"
            )
        self.enabled = bool(enabled)
        self._authorizer = authorizer
        self._clock_ns = clock_ns
        self._max_consecutive_failures = max_consecutive_failures
        self._max_injected_total = max_injected_total
        self._points: dict[tuple[str, str], frozenset[HookMode]] = {}
        self._hooks: dict[str, _HookState] = {}
        self._lock = threading.Lock()

    # {
    #   責務: [declare_point: Core側のstage境界がHookを受け付けることと、その境界で許すHookModeを宣言する]
    #   処理: [
    #     1: stageが公式stage IDか`ext.`付き拡張stageか、boundaryがinput/outputか確認する
    #     2: Safety・Execution stageではread以外を拒否する
    #     3: 同じ境界の再宣言は同じmodeなら受け入れ、異なれば拒否する
    #   ]
    #   引数: [stage: StageEnvelopeのstage ID, boundary: input/output, modes: 許可するHookMode]
    #   戻り値: [なし]
    #   エラー: [ValueError: stage・boundary・modeが不正、またはSafety/Execution境界でread以外を許可しようとした場合]
    # }
    def declare_point(
        self, stage: str, boundary: str, modes: Iterable[HookMode]
    ) -> None:
        if not isinstance(stage, str) or not (
            stage in CORE_STAGE_IDS or _EXTENSION_STAGE_PATTERN.fullmatch(stage)
        ):
            raise ValueError("stage must be a core stage id or an ext. stage id")
        if boundary not in ("input", "output"):
            raise ValueError("boundary must be input or output")
        allowed = frozenset(modes)
        if not allowed or any(not isinstance(mode, HookMode) for mode in allowed):
            raise ValueError("modes must be a non-empty set of HookMode")
        if stage in HOOK_READ_ONLY_STAGES and allowed != {HookMode.READ}:
            raise ValueError(f"{stage} boundaries accept read hooks only")
        key = (stage, boundary)
        with self._lock:
            existing = self._points.get(key)
            if existing is not None and existing != allowed:
                raise ValueError(f"hook point {stage}/{boundary} is already declared")
            self._points[key] = allowed

    # {
    #   責務: [register: Hookを宣言済みの境界へ登録する]
    #   処理: [
    #     1: 境界が宣言済みで、そのmodeを許可しているか確認する
    #     2: authorizerがあれば呼び、拒否（例外）なら登録しない
    #     3: hook_idの重複を拒否して登録表へ追加する
    #   ]
    #   引数: [registration: 検証済みの登録内容]
    #   戻り値: [なし]
    #   エラー: [ValueError: 未宣言の境界・未許可のmode・hook_id重複の場合, authorizerが送出した例外: 権限判定で拒否された場合]
    # }
    def register(self, registration: HookRegistration) -> None:
        if not isinstance(registration, HookRegistration):
            raise ValueError("registration must be a HookRegistration")
        key = (registration.stage, registration.boundary)
        with self._lock:
            allowed = self._points.get(key)
        if allowed is None:
            raise ValueError(
                f"hook point {registration.stage}/{registration.boundary} is not declared"
            )
        if registration.mode not in allowed:
            raise ValueError(
                f"{registration.mode.value} hooks are not allowed at "
                f"{registration.stage}/{registration.boundary}"
            )
        if self._authorizer is not None:
            self._authorizer(registration)
        with self._lock:
            if registration.hook_id in self._hooks:
                raise ValueError(
                    f"hook_id {registration.hook_id} is already registered"
                )
            self._hooks[registration.hook_id] = _HookState(registration)

    # {
    #   責務: [unregister: 登録済みHookを取り除く]
    #   処理: [hook_idの登録と失敗状態を削除する。未登録なら何もしない]
    #   引数: [hook_id: 取り除くHook]
    #   戻り値: [bool: 削除した場合True]
    # }
    def unregister(self, hook_id: str) -> bool:
        with self._lock:
            return self._hooks.pop(hook_id, None) is not None

    # {
    #   責務: [has_hooks: 指定境界のdispatchでHookが呼ばれ得るかを返す]
    #   処理: [enabledで、その境界の登録が1件以上あるか確認する]
    #   引数: [stage: stage ID, boundary: input/output]
    #   戻り値: [bool: Envelopeを作ってdispatchする必要がある場合True]
    # }
    def has_hooks(self, stage: str, boundary: str) -> bool:
        return self.enabled and bool(self._hooks_at(stage, boundary))

    # {
    #   責務: [has_halting_hooks: 指定境界にon_failure=HALTの登録があるかを返す]
    #   処理: [enabledで、その境界にHALT方針のHookが1件以上あるか確認する]
    #   引数: [stage: stage ID, boundary: input/output]
    #   戻り値: [bool: Envelopeを作れない場合も含め、失敗時に境界を停止すべきならTrue]
    # }
    def has_halting_hooks(self, stage: str, boundary: str) -> bool:
        return self.enabled and any(
            state.registration.on_failure is HookFailurePolicy.HALT
            for state in self._hooks_at(stage, boundary)
        )

    # {
    #   責務: [dispatch: 1件のEnvelopeを、その境界の登録Hookへ順に渡し、加工後・派生Envelopeと計測値を返す]
    #   処理: [
    #     1: 無効、または境界に登録がなければ、Hookを呼ばず入力をそのまま返す
    #     2: modify→read→injectの順、各modeの中はpriority・hook_id昇順でHookを1件ずつ呼ぶ
    #     3: 各Hookの出力をcanonical JSONの往復で再検証し、provenance・境界規則・予算を確認する。違反した出力は一切使わない
    #     4: 失敗したHookがHALT方針なら、残りのHookを呼ばずhalted=Trueで返す。SKIP方針なら失敗を記録して続ける
    #   ]
    #   引数: [envelope: Core stage境界の検証済みEnvelope]
    #   戻り値: [HookDispatchResult: 後段へ渡す最終Envelope、派生Envelope、Hookごとの記録、停止判定、総latency]
    #   エラー: [ValueError: envelopeがStageEnvelopeでない場合]
    # }
    def dispatch(self, envelope: StageEnvelope) -> HookDispatchResult:
        if not isinstance(envelope, StageEnvelope):
            raise ValueError("dispatch requires a StageEnvelope")
        if not self.enabled:
            return HookDispatchResult(envelope)
        states = self._hooks_at(envelope.stage, envelope.boundary)
        if not states:
            return HookDispatchResult(envelope)
        started = self._clock_ns()
        current = envelope
        injected: list[StageEnvelope] = []
        records: list[HookExecutionRecord] = []
        seen_ids = {envelope.envelope_id}
        for state in self._ordered(states):
            registration = state.registration
            record, outputs = self._run_one(
                state, envelope, current, seen_ids, len(injected)
            )
            records.append(record)
            if record.status != HOOK_STATUS_COMPLETED:
                if registration.on_failure is HookFailurePolicy.HALT:
                    return HookDispatchResult(
                        current,
                        tuple(injected),
                        tuple(records),
                        halted=True,
                        halt_reasons=(f"{registration.hook_id}:{record.failure_kind}",),
                        elapsed_ns=max(0, self._clock_ns() - started),
                    )
                continue
            if registration.mode is HookMode.MODIFY:
                current = outputs[0]
            elif registration.mode is HookMode.INJECT:
                injected.extend(outputs)
        return HookDispatchResult(
            current,
            tuple(injected),
            tuple(records),
            elapsed_ns=max(0, self._clock_ns() - started),
        )

    # {
    #   責務: [_hooks_at: 指定境界の登録状態を取り出す]
    #   処理: [lock内で登録表を走査し、stageとboundaryが一致するものを返す]
    #   引数: [stage: stage ID, boundary: input/output]
    #   戻り値: [list[_HookState]: 一致する登録状態（順不同）]
    # }
    def _hooks_at(self, stage: str, boundary: str) -> list[_HookState]:
        with self._lock:
            return [
                state
                for state in self._hooks.values()
                if state.registration.stage == stage
                and state.registration.boundary == boundary
            ]

    # {
    #   責務: [_ordered: 登録状態を決定論的な実行順へ並べる]
    #   処理: [modeを modify→read→inject の順に、各modeの中をpriority・hook_id昇順に並べる]
    #   引数: [states: 同じ境界の登録状態]
    #   戻り値: [list[_HookState]: 実行順]
    # }
    @staticmethod
    def _ordered(states: list[_HookState]) -> list[_HookState]:
        return sorted(
            states,
            key=lambda state: (
                _MODE_ORDER.index(state.registration.mode),
                state.registration.order_key(),
            ),
        )

    # {
    #   責務: [_run_one: 1つのHookを予算内で呼び、出力を検査して記録と採用する出力を返す]
    #   処理: [
    #     1: quarantine済みなら呼ばずにquarantinedを記録する
    #     2: callbackを期限付きで呼び、timeout・例外・出力違反を失敗として分類する
    #     3: 成功なら連続失敗数を0に戻し、失敗なら数を増やして上限またはtimeoutでquarantineし、logへ残す
    #   ]
    #   引数: [state: 登録状態, original: dispatchへの入力Envelope, current: modify適用後のEnvelope,
    #     seen_ids: このdispatchで既に使われたEnvelope ID（成功時に追加する）, injected_count: 既に採用した派生Envelope数]
    #   戻り値: [tuple[HookExecutionRecord, tuple[StageEnvelope, ...]]: 記録と、採用する出力（失敗時は空）]
    # }
    def _run_one(
        self,
        state: _HookState,
        original: StageEnvelope,
        current: StageEnvelope,
        seen_ids: set[str],
        injected_count: int,
    ) -> tuple[HookExecutionRecord, tuple[StageEnvelope, ...]]:
        registration = state.registration
        mode = registration.mode.value
        if state.quarantined:
            return (
                HookExecutionRecord(
                    registration.hook_id,
                    mode,
                    HOOK_STATUS_QUARANTINED,
                    0,
                    failure_kind="quarantined",
                ),
                (),
            )
        outcome = self._invoke(registration, current)
        failure_kind: str | None = None
        error_code: str | None = None
        outputs: tuple[StageEnvelope, ...] = ()
        if outcome.timed_out:
            failure_kind = "timeout"
        elif outcome.error is not None:
            failure_kind = "exception"
        else:
            try:
                outputs = self._accept_outputs(
                    registration, outcome.value, current, seen_ids, injected_count
                )
            except StageEnvelopeError as error:
                failure_kind, error_code = "invalid_output", error.code
            except _HookContractViolation:
                failure_kind = "contract_violation"
        if failure_kind is None:
            state.consecutive_failures = 0
            seen_ids.update(item.envelope_id for item in outputs)
            return (
                HookExecutionRecord(
                    registration.hook_id,
                    mode,
                    HOOK_STATUS_COMPLETED,
                    outcome.elapsed_ns,
                    output_envelope_ids=tuple(item.envelope_id for item in outputs),
                ),
                outputs,
            )
        state.consecutive_failures += 1
        if (
            outcome.timed_out
            or state.consecutive_failures >= self._max_consecutive_failures
        ):
            # timeoutしたthreadは止められないため、同じHookを再び呼んで実行中threadを増やさない。
            state.quarantined = True
        _LOGGER.warning(
            "hook %s (%s at %s/%s) failed: kind=%s code=%s error_type=%s quarantined=%s",
            registration.hook_id,
            mode,
            original.stage,
            original.boundary,
            failure_kind,
            error_code,
            type(outcome.error).__name__ if outcome.error is not None else None,
            state.quarantined,
        )
        return (
            HookExecutionRecord(
                registration.hook_id,
                mode,
                HOOK_STATUS_TIMEOUT if outcome.timed_out else HOOK_STATUS_FAILED,
                outcome.elapsed_ns,
                failure_kind=failure_kind,
                error_code=error_code,
            ),
            (),
        )

    # {
    #   責務: [_invoke: callbackを専用daemon threadで呼び、timeout_ns以内に終わったかと結果・所要時間を返す]
    #   処理: [threadを開始してtimeoutまでjoinし、終わっていなければtimed_outにする。例外はthread内で捕捉して呼び出し側へ返す]
    #   引数: [registration: 呼ぶHook, envelope: callbackへ渡す不変Envelope]
    #   戻り値: [_InvocationOutcome: 戻り値・例外・timeout・所要時間]
    # }
    def _invoke(
        self, registration: HookRegistration, envelope: StageEnvelope
    ) -> _InvocationOutcome:
        outcome = _InvocationOutcome()

        # {
        #   責務: [call: thread内でcallbackを1回呼び、戻り値か例外をoutcomeへ置く]
        #   処理: [Hook由来の全例外を捕捉し、Coreのthreadへ伝播させない]
        #   引数: [なし] 戻り値: [なし]
        # }
        def call() -> None:
            try:
                outcome.value = registration.callback(envelope)
            except BaseException as error:  # noqa: BLE001 - Hook失敗はCoreへ伝播させず分類する
                outcome.error = error
            finally:
                outcome.done.set()

        worker = threading.Thread(
            target=call, name=f"hook:{registration.hook_id}", daemon=True
        )
        started = self._clock_ns()
        worker.start()
        finished = outcome.done.wait(registration.timeout_ns / 1_000_000_000)
        outcome.elapsed_ns = max(0, self._clock_ns() - started)
        outcome.timed_out = not finished
        return outcome

    # {
    #   責務: [_accept_outputs: Hookの戻り値をmodeごとの規則で検査し、採用できる再検証済みEnvelopeへ変換する]
    #   処理: [
    #     1: readはNoneだけを受け入れる
    #     2: modifyは1件、injectはlist/tupleで上限件数以内を受け入れる
    #     3: 各EnvelopeをHookの予算内でcanonical JSONへencodeし、decodeし直して信頼できる新しいinstanceにする
    #     4: producer・parent・session/trace・ID重複・stage/boundaryの規則を確認する
    #   ]
    #   引数: [registration: 出力したHook, value: callbackの戻り値, current: Hookへ渡したEnvelope,
    #     seen_ids: 使用済みEnvelope ID, injected_count: 既に採用した派生Envelope数]
    #   戻り値: [tuple[StageEnvelope, ...]: 採用する出力（readは空）]
    #   エラー: [StageEnvelopeError: 出力がv1契約またはbyte予算に違反する場合, _HookContractViolation: mode・provenance・境界規則に違反する場合]
    # }
    def _accept_outputs(
        self,
        registration: HookRegistration,
        value: Any,
        current: StageEnvelope,
        seen_ids: set[str],
        injected_count: int,
    ) -> tuple[StageEnvelope, ...]:
        if registration.mode is HookMode.READ:
            if value is not None:
                raise _HookContractViolation("read hooks must return None")
            return ()
        if registration.mode is HookMode.MODIFY:
            candidates: tuple[Any, ...] = (value,)
        else:
            if not isinstance(value, (list, tuple)):
                raise _HookContractViolation("inject hooks must return a list or tuple")
            if (
                len(value) > registration.max_injected
                or injected_count + len(value) > self._max_injected_total
            ):
                raise _HookContractViolation("inject hook exceeded its envelope budget")
            candidates = tuple(value)
        accepted: list[StageEnvelope] = []
        batch_ids: set[str] = set()
        for candidate in candidates:
            envelope = self._revalidate(registration, candidate)
            self._check_provenance(registration, envelope, current)
            if envelope.envelope_id in seen_ids or envelope.envelope_id in batch_ids:
                raise _HookContractViolation("hook output reused an envelope_id")
            batch_ids.add(envelope.envelope_id)
            accepted.append(envelope)
        return tuple(accepted)

    # {
    #   責務: [_revalidate: Hookが返したobjectを、byte予算内のcanonical JSON往復で新しい検証済みEnvelopeへ作り直す]
    #   処理: [StageEnvelope以外を拒否し、encode結果のbyte数をHookの上限と比べ、decodeで厳密に再検証する]
    #   引数: [registration: 出力したHook, candidate: Hookの出力]
    #   戻り値: [StageEnvelope: Hookが保持する参照と共有しない再検証済みEnvelope]
    #   エラー: [StageEnvelopeError: 契約違反・byte上限超過, _HookContractViolation: StageEnvelopeでない場合]
    # }
    @staticmethod
    def _revalidate(registration: HookRegistration, candidate: Any) -> StageEnvelope:
        if not isinstance(candidate, StageEnvelope):
            raise _HookContractViolation("hook output must be a StageEnvelope")
        encoded = encode_stage_envelope(candidate)
        if len(encoded) > registration.max_output_bytes:
            raise StageEnvelopeError(
                StageEnvelopeError.LIMIT_EXCEEDED,
                "hook output exceeds the hook byte budget",
            )
        return decode_stage_envelope(encoded)

    # {
    #   責務: [_check_provenance: Hook出力が呼び出したHook自身の派生物として正しく記録され、許可された境界にあるか確認する]
    #   処理: [
    #     1: producer_kind=hook、producer_id/producer_versionが登録値、parentが入力ID、session/traceが入力と同じか確認する
    #     2: modifyはstage・boundary・payload契約を入力と同じに保つ
    #     3: injectは入力IDをderived_fromに含め、Safety/Execution stageのEnvelopeを作らない
    #   ]
    #   引数: [registration: 出力したHook, envelope: 再検証済み出力, current: Hookへ渡した入力]
    #   戻り値: [なし]
    #   エラー: [_HookContractViolation: いずれかの規則に違反する場合]
    # }
    @staticmethod
    def _check_provenance(
        registration: HookRegistration,
        envelope: StageEnvelope,
        current: StageEnvelope,
    ) -> None:
        if (
            envelope.producer_kind != "hook"
            or envelope.producer_id != registration.hook_id
            or envelope.producer_version != registration.hook_version
        ):
            raise _HookContractViolation("hook output must name the hook as producer")
        if envelope.parent_envelope_id != current.envelope_id:
            raise _HookContractViolation("hook output must reference its input")
        if (
            envelope.session_id != current.session_id
            or envelope.trace_id != current.trace_id
        ):
            raise _HookContractViolation("hook output must keep session and trace")
        if registration.mode is HookMode.MODIFY:
            if (
                envelope.stage != current.stage
                or envelope.boundary != current.boundary
                or envelope.payload_schema != current.payload_schema
                or envelope.payload_schema_version != current.payload_schema_version
            ):
                raise _HookContractViolation(
                    "modify hooks must keep the boundary and payload contract"
                )
            return
        if current.envelope_id not in envelope.derived_from:
            raise _HookContractViolation("injected envelopes must derive from input")
        if envelope.stage in HOOK_READ_ONLY_STAGES:
            raise _HookContractViolation(
                "hooks must not create safety or execution envelopes"
            )
