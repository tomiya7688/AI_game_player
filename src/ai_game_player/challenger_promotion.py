"""#171 Promotion / Rollback Gate: 評価結果からChallengerを採否し、Champion pointerと監査を原子的に更新する。

#170の`ChallengerArtifact`と、#173系が測った`CandidateEvaluation`を入力にする。採否は渡された数値から
このGateが計算し、外部の`passed`は受け付けない。実runtimeへの接続と#173の評価runnerは本Issueの範囲外。
"""

from hashlib import sha256
import json
from pathlib import Path
import threading
from typing import Any

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.bounded_file import read_regular_file
from ai_game_player.challenger_promotion_models import (
    PROMOTION_SCHEMA_VERSION,
    ROLLBACK_REASONS,
    CandidateEvaluation,
    PromotionCriteria,
    PromotionDecision,
    PromotionError,
    to_json_bytes,
)
from ai_game_player.challenger_update_models import (
    ChallengerArtifact,
    ChallengerUpdateError,
    ComponentParameters,
    validate_created_at,
)
from ai_game_player.cross_process_lock import cross_process_file_lock
from ai_game_player.knowledge_scope import KnowledgeScope

MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_ROLLBACK_VERSIONS = 32
MAX_AUDIT_EVENTS = 128
GENESIS_DIGEST = "0" * 64
_SCHEMA = "challenger-champion-registry"


# {
#   責務: [ChampionRegistry: component×scopeごとのChampion pointer・rollback履歴・監査を1ファイルで所有し、採否とrollbackを原子的に確定する]
#   フィールド: [_path: 状態JSONの保存先(隣に.lockを置く), _thread_lock: 同一process内の直列化]
#   処理: [操作ごとにlock内で状態を読み直し、検証し、一時ファイル経由で置換する。失敗時は旧状態のまま]
# }
class ChampionRegistry:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._thread_lock = threading.Lock()

    # {
    #   責務: [initialize: 初回のChampionを登録する]
    #   処理: [未登録であることを確認し、世代1以降として登録と監査を確定する]
    #   引数: [champion: 初回Champion, at: UTC Z時刻]
    #   戻り値: [PromotionDecision: status=promoted, reasons=空]
    #   エラー: [PromotionError: already_initializedまたはinvalid_input]
    # }
    def initialize(self, champion: ComponentParameters, *, at: str) -> PromotionDecision:
        _check_time(at)
        if type(champion) is not ComponentParameters:
            raise PromotionError("invalid_input", "champion must be ComponentParameters")
        with self._locked() as state:
            key = _key(champion)
            if key in state["targets"]:
                raise PromotionError("already_initialized", "champion already registered")
            state["targets"][key] = {"champion": champion.to_dict(), "history": [], "last_change_at": at}
            _append_audit(state, "initialize", champion, None, champion.sha256, None, None, (), at)
            self._commit(state)
            return PromotionDecision("promoted", (), state["generation"], champion.version, "")

    # {
    #   責務: [decide: 評価結果から昇格または却下を決め、昇格ならpointerを原子的に切り替える]
    #   処理: [1: 前提Championの一致を確認, 2: 評価の対象一致を確認, 3: 件数・改善・回帰・資源を基準で計算, 4: 昇格か却下かを監査付きで確定]
    #   引数: [artifact: #170のChallenger, evaluation: 比較評価の結果, criteria: 昇格基準, at: UTC Z時刻]
    #   戻り値: [PromotionDecision]
    #   エラー: [PromotionError: not_initializedまたはinvalid_input]
    # }
    def decide(
        self,
        artifact: ChallengerArtifact,
        evaluation: CandidateEvaluation,
        criteria: PromotionCriteria,
        *,
        at: str,
    ) -> PromotionDecision:
        _check_time(at)
        if (
            type(artifact) is not ChallengerArtifact
            or type(evaluation) is not CandidateEvaluation
            or type(criteria) is not PromotionCriteria
        ):
            raise PromotionError("invalid_input", "artifact, evaluation and criteria must be typed values")
        with self._locked() as state:
            key = _key(artifact.parent)
            target = state["targets"].get(key)
            if target is None:
                raise PromotionError("not_initialized", "no champion registered for this target")
            current = ComponentParameters.from_dict(target["champion"])
            reasons = _reject_reasons(
                artifact, evaluation, criteria, current, target["last_change_at"], at
            )
            if reasons:
                _append_audit(
                    state, "reject", current, artifact, current.sha256, evaluation, criteria, reasons, at
                )
                self._commit(state)
                return PromotionDecision("rejected", reasons, state["generation"], current.version, criteria.fingerprint)
            target["history"] = (target["history"] + [target["champion"]])[-MAX_ROLLBACK_VERSIONS:]
            target["champion"] = artifact.challenger.to_dict()
            target["last_change_at"] = at
            _append_audit(
                state, "promote", artifact.challenger, artifact, artifact.challenger.sha256,
                evaluation, criteria, (), at, from_sha256=current.sha256,
            )
            self._commit(state)
            return PromotionDecision(
                "promoted", (), state["generation"], artifact.challenger.version, criteria.fingerprint
            )

    # {
    #   責務: [rollback: 直前のChampionへpointerを戻す]
    #   処理: [履歴の最後を現在のChampionにし、戻した事実と固定理由を監査へ残す]
    #   引数: [component: 部品名, scope: 対象scope, reason: ROLLBACK_REASONSの一つ, at: UTC Z時刻]
    #   戻り値: [PromotionDecision: status=promoted(=戻した版を本番にした), reasons=空]
    #   エラー: [PromotionError: not_initialized / no_rollback_target / invalid_input]
    # }
    def rollback(self, component: str, scope: KnowledgeScope, *, reason: str, at: str) -> PromotionDecision:
        _check_time(at)
        if reason not in ROLLBACK_REASONS or type(scope) is not KnowledgeScope:
            raise PromotionError("invalid_input", "rollback reason or scope is invalid")
        with self._locked() as state:
            target = state["targets"].get(f"{component}:{scope.key}")
            if target is None:
                raise PromotionError("not_initialized", "no champion registered for this target")
            if not target["history"]:
                raise PromotionError("no_rollback_target", "no previous champion to restore")
            current = ComponentParameters.from_dict(target["champion"])
            previous = ComponentParameters.from_dict(target["history"].pop())
            target["champion"] = previous.to_dict()
            target["last_change_at"] = at
            _append_audit(
                state, "rollback", previous, None, previous.sha256, None, None, (reason,), at,
                from_sha256=current.sha256,
            )
            self._commit(state)
            return PromotionDecision("promoted", (), state["generation"], previous.version, "")

    # {
    #   責務: [champion: 現在のChampion parameterを返す]
    #   処理: [lock内で状態を読み、検証済みのpointerを返す]
    #   引数: [component: 部品名, scope: 対象scope] 戻り値: [ComponentParameters | None: 未登録ならNone]
    # }
    def champion(self, component: str, scope: KnowledgeScope) -> ComponentParameters | None:
        with self._locked() as state:
            target = state["targets"].get(f"{component}:{scope.key}")
            return None if target is None else ComponentParameters.from_dict(target["champion"])

    # {
    #   責務: [audit_events: 保持している監査イベントを古い順に返す]
    #   処理: [lock内で読み、digest連鎖が検証済みの複製を返す] 引数: [なし] 戻り値: [list[dict]]
    # }
    def audit_events(self) -> list[dict[str, Any]]:
        with self._locked() as state:
            return json.loads(json.dumps(state["audit"]))

    def _locked(self) -> "_StateSession":
        return _StateSession(self)

    def _commit(self, state: dict[str, Any]) -> None:
        state["generation"] += 1
        if len(state["audit"]) > MAX_AUDIT_EVENTS:
            dropped = state["audit"][: len(state["audit"]) - MAX_AUDIT_EVENTS]
            state["audit_base_digest"] = dropped[-1]["digest"]
            state["audit_first_sequence"] += len(dropped)
            state["audit"] = state["audit"][len(dropped):]
        staged = stage_json_write(self._path, state)
        try:
            # 実際に書いたファイル（インデント込み）が読込上限を超える状態は公開しない（公開すると以後読めなくなる）。
            if staged.temporary_path.stat().st_size > MAX_STATE_BYTES:
                raise PromotionError("invalid_state", "registry state would exceed the size limit")
            staged.publish()
        finally:
            staged.discard()


class _StateSession:
    """lockの取得・状態の読込検証・例外時の破棄を担う内部のcontext。"""

    def __init__(self, registry: ChampionRegistry) -> None:
        self._registry = registry
        self._stack: Any = None

    def __enter__(self) -> dict[str, Any]:
        registry = self._registry
        registry._thread_lock.acquire()
        try:
            self._stack = cross_process_file_lock(registry._path.with_suffix(registry._path.suffix + ".lock"))
            self._stack.__enter__()
            return _read_state(registry._path)
        except BaseException:
            self._release(None)
            raise

    def __exit__(self, *exc_info: Any) -> None:
        self._release(exc_info)

    def _release(self, exc_info: Any) -> None:
        try:
            if self._stack is not None:
                self._stack.__exit__(*(exc_info or (None, None, None)))
        finally:
            self._stack = None
            self._registry._thread_lock.release()


def _reject_reasons(
    artifact: ChallengerArtifact,
    evaluation: CandidateEvaluation,
    criteria: PromotionCriteria,
    current: ComponentParameters,
    last_change_at: str,
    decided_at: str,
) -> tuple[str, ...]:
    if artifact.parent.sha256 != current.sha256:
        return ("stale_champion",)
    if (
        evaluation.artifact_id != artifact.artifact_id
        or evaluation.champion_sha256 != artifact.parent.sha256
        or evaluation.challenger_sha256 != artifact.challenger.sha256
    ):
        return ("evaluation_mismatch",)
    # 評価はtargetの最後の変更(初期化・昇格・rollback)より後に測ったものだけを受け付ける。
    # 時刻は対象ごとに1つだけ持つので、監査ログの保持上限(128件)に依存せず永続的に効く。
    if evaluation.evaluated_at <= last_change_at:
        return ("stale_evaluation",)
    if evaluation.evaluated_at > decided_at:
        return ("evaluation_in_future",)
    reasons = []
    if evaluation.target_cases < criteria.min_target_cases:
        reasons.append("insufficient_target_cases")
    if evaluation.regression_cases < criteria.min_regression_cases:
        reasons.append("insufficient_regression_cases")
    if _gap(evaluation.target_candidate, evaluation.target_baseline) < criteria.min_target_gain:
        reasons.append("no_target_improvement")
    if _gap(evaluation.regression_baseline, evaluation.regression_candidate) > criteria.max_regression_drop:
        reasons.append("regression")
    cost = artifact.resource_cost
    if cost.parameter_bytes > criteria.max_parameter_bytes or cost.delta_bytes > criteria.max_delta_bytes:
        reasons.append("resource_exceeded")
    return tuple(sorted(reasons))


def _gap(larger: float, smaller: float) -> float:
    # 0.55-0.53のような境界値が浮動小数の誤差で基準を下回らないよう、9桁へ丸めて比べる。
    return round(larger - smaller, 9)


def _append_audit(
    state: dict[str, Any],
    action: str,
    subject: ComponentParameters,
    artifact: ChallengerArtifact | None,
    to_sha256: str,
    evaluation: CandidateEvaluation | None,
    criteria: PromotionCriteria | None,
    reasons: tuple[str, ...],
    at: str,
    *,
    from_sha256: str | None = None,
) -> None:
    previous = state["audit"][-1]["digest"] if state["audit"] else state["audit_base_digest"]
    event = {
        "sequence": state["audit_first_sequence"] + len(state["audit"]),
        "generation": state["generation"] + 1,
        "action": action,
        "target": _key(subject),
        "from_sha256": from_sha256,
        "to_sha256": to_sha256,
        "artifact_id": None if artifact is None else artifact.artifact_id,
        "evaluation_ref": None if evaluation is None else evaluation.evaluation_ref,
        "criteria_fingerprint": None if criteria is None else criteria.fingerprint,
        "reasons": list(reasons),
        "at": at,
        "prev_digest": previous,
    }
    event["digest"] = _event_digest(event)
    state["audit"].append(event)


def _event_digest(event: dict[str, Any]) -> str:
    body = {name: value for name, value in event.items() if name != "digest"}
    return sha256(to_json_bytes(body)).hexdigest()


def _key(parameters: ComponentParameters) -> str:
    return f"{parameters.component}:{parameters.scope.key}"


def _check_time(value: object) -> None:
    try:
        validate_created_at(value)
    except ChallengerUpdateError as error:
        raise PromotionError("invalid_input", "at must be a UTC Z timestamp") from error


def _new_state() -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "generation": 0,
        "audit_base_digest": GENESIS_DIGEST,
        "audit_first_sequence": 1,
        "targets": {},
        "audit": [],
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_state()
    try:
        raw = read_regular_file(path, max_bytes=MAX_STATE_BYTES)
        state = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        _validate_state(state)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PromotionError("invalid_state", f"registry state is invalid: {error}") from error
    return state


def _validate_state(state: Any) -> None:
    expected = set(_new_state())
    if (
        not isinstance(state, dict)
        or set(state) != expected
        or state["schema"] != _SCHEMA
        or state["schema_version"] != PROMOTION_SCHEMA_VERSION
        or type(state["generation"]) is not int
        or not isinstance(state["targets"], dict)
        or not isinstance(state["audit"], list)
    ):
        raise ValueError("registry fields do not match the contract")
    for key, target in state["targets"].items():
        if not isinstance(target, dict) or not isinstance(target.get("history"), list):
            raise ValueError("target history must be a list")
        if type(target.get("last_change_at")) is not str:
            raise ValueError("target last_change_at must be a timestamp")
        _check_time(target["last_change_at"])
        champion = ComponentParameters.from_dict(target["champion"])
        history = [ComponentParameters.from_dict(item) for item in target["history"]]
        if set(target) != {"champion", "history", "last_change_at"} or len(history) > MAX_ROLLBACK_VERSIONS:
            raise ValueError("target record does not match the contract")
        if key != _key(champion) or any(_key(item) != key for item in history):
            raise ValueError("target key does not match its parameters")
    if (state["targets"] or state["generation"] != 0) and not state["audit"]:
        raise ValueError("registered targets and generations require an audit trail")
    _validate_pointer_binding(state)
    previous = state["audit_base_digest"]
    sequence = state["audit_first_sequence"]
    for event in state["audit"]:
        if (
            event["prev_digest"] != previous
            or event["digest"] != _event_digest(event)
            or event["sequence"] != sequence
        ):
            raise ValueError("audit chain is broken")
        previous = event["digest"]
        sequence += 1


def _validate_pointer_binding(state: dict[str, Any]) -> None:
    """Champion pointerと世代を、保持している監査eventの最新状態へ束縛する。"""
    latest: dict[str, dict[str, Any]] = {}
    last_change: dict[str, dict[str, Any]] = {}
    for event in state["audit"]:
        latest[event["target"]] = event
        if event["action"] != "reject":
            last_change[event["target"]] = event
    # 評価の鮮度基準(last_change_at)は、保持している最新の変更eventの時刻と一致しなければならない。
    for key, event in last_change.items():
        target = state["targets"].get(key)
        if target is None or target["last_change_at"] != event["at"]:
            raise ValueError("last change time does not match the audit trail")
    for key, event in latest.items():
        target = state["targets"].get(key)
        if target is None or ComponentParameters.from_dict(target["champion"]).sha256 != event["to_sha256"]:
            raise ValueError("champion pointer does not match the audit trail")
        # 直前のeventが昇格なら、rollback先（履歴の末尾）はその昇格前のChampionと一致しなければならない。
        if event["action"] == "promote" and (
            not target["history"]
            or ComponentParameters.from_dict(target["history"][-1]).sha256 != event["from_sha256"]
        ):
            raise ValueError("rollback history does not match the audit trail")
    if state["audit"] and state["audit"][-1]["generation"] != state["generation"]:
        raise ValueError("generation does not match the audit trail")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in registry state")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite number in registry state: {value}")

