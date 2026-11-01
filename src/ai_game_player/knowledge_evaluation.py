"""同一の記録済み判断ケースでKnowledgeの変更前後を評価する。実入力は発行しない。"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
import statistics
import threading
import time
import tracemalloc
from typing import Callable

from ai_game_player.baseline_provider import BaselineWithheld
from ai_game_player.decision_context import DecisionContext
from ai_game_player.decision_reliability import ReliabilityStatus
from ai_game_player.decision_verifier import DecisionVerifier
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.models import ScreenObservation


EVALUATION_GROUPS = frozenset({"replay", "regression", "safety", "playability"})
MAX_EVALUATION_CASES = 512
_EVALUATION_TRACE_LOCK = threading.Lock()


# {
#   責務: [KnowledgeEvaluationCase: 根拠付きの同一観測・許可候補・操作成果を対照評価へ渡す]
#   フィールド: [case_id: suite内の一意な不透明ID, scope: 評価対象の隔離範囲,
#     group: replay/regression/safety/playability, context: 実行しない判断材料,
#     reward_by_action: 記録・検証済みの操作成果-1〜1。unknownは数値を捏造せずケースにしない,
#     evidence_sha256: 成果ラベルの出所を参照するdigest]
# }
@dataclass(frozen=True)
class KnowledgeEvaluationCase:
    case_id: str
    scope: KnowledgeScope
    group: str
    context: DecisionContext
    reward_by_action: tuple[tuple[str, float], ...]
    evidence_sha256: str

    # {
    #   責務: [__post_init__: 未知成果・重複候補・不正な根拠を評価入力にしない]
    #   処理: [群と数値とdigestを検証し、外部の可変contextと報酬辞書をコピーする]
    #   引数: [self: 構築中の評価ケース]
    #   戻り値: [なし]
    #   エラー: [ValueError: ケース契約が不正、または許可候補の成果が不足]
    # }
    def __post_init__(self) -> None:
        validate_identifier(self.case_id, "case_id")
        if (
            not isinstance(self.scope, KnowledgeScope)
            or self.group not in EVALUATION_GROUPS
        ):
            raise ValueError("knowledge evaluation scope/group is invalid")
        if not isinstance(self.context, DecisionContext):
            raise ValueError("knowledge evaluation requires a DecisionContext")
        rewards = tuple(tuple(item) for item in self.reward_by_action)
        reward_map = dict(rewards)
        if len(reward_map) != len(rewards):
            raise ValueError("knowledge evaluation repeats an action reward")
        for action_id, reward in rewards:
            validate_identifier(action_id, "action_id")
            if (
                type(reward) not in (int, float)
                or not isfinite(reward)
                or not -1 <= reward <= 1
            ):
                raise ValueError("knowledge evaluation rewards must be finite in -1..1")
        if not set(self.context.allowed_action_ids) <= set(reward_map):
            raise ValueError(
                "knowledge evaluation lacks allowed-action outcome evidence"
            )
        if len(reward_map) > 256 or len(_canonical(self.context.to_dict())) > 65536:
            raise ValueError("knowledge evaluation case exceeds its input budget")
        if not _digest(self.evidence_sha256):
            raise ValueError("knowledge evaluation requires an evidence digest")
        object.__setattr__(self, "context", deepcopy(self.context))
        object.__setattr__(self, "reward_by_action", tuple(sorted(rewards)))


# {
#   責務: [KnowledgeEvaluationReport: 実際の対照判断から集計した測定値を対象版へ固定する]
#   フィールド: [scope: 隔離範囲, champion_sha256/challenger_sha256: 比較した不変版,
#     suite_sha256: ケース・成果ラベルのdigest, case_count: 一意ケース数,
#     group_quality: 群ごとの件数・変更前後の平均成果,
#     invalid_decisions: 許可境界/Verifier違反件数の変更前後,
#     execution_errors: 判断器例外件数の変更前後,
#     latency_p95_ms: 判断器初期化と判断のp95ミリ秒の変更前後,
#     peak_python_bytes: tracemallocで測った最大Python割当の変更前後]
# }
@dataclass(frozen=True)
class KnowledgeEvaluationReport:
    scope: KnowledgeScope
    champion_sha256: str
    challenger_sha256: str
    suite_sha256: str
    case_count: int
    group_quality: tuple[tuple[str, int, float, float], ...]
    invalid_decisions: tuple[int, int]
    execution_errors: tuple[int, int]
    latency_p95_ms: tuple[float, float]
    peak_python_bytes: tuple[int, int]

    # {
    #   責務: [to_dict: 自己申告の合格フラグを含まない評価記録を作る]
    #   処理: [対象digest、出所、群ごとの成果と実測資源を出力する]
    #   引数: [self: 実行済みの対照評価]
    #   戻り値: [dict: JSONに保存できるoffline評価記録]
    # }
    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "verification": "offline_paired_decision_replay",
            "scope": self.scope.to_dict(),
            "champion_sha256": self.champion_sha256,
            "challenger_sha256": self.challenger_sha256,
            "suite_sha256": self.suite_sha256,
            "case_count": self.case_count,
            "group_quality": {
                name: {"count": count, "champion": before, "challenger": after}
                for name, count, before, after in self.group_quality
            },
            "invalid_decisions": list(self.invalid_decisions),
            "execution_errors": list(self.execution_errors),
            "latency_p95_ms": list(self.latency_p95_ms),
            "peak_python_bytes": list(self.peak_python_bytes),
        }

    # {
    #   責務: [sha256: 対象版と全測定値を含む評価記録を識別する]
    #   処理: [正規化JSONのdigestを返す]
    #   引数: [self: 評価記録]
    #   戻り値: [str: 64文字のhex digest]
    # }
    @property
    def sha256(self) -> str:
        return sha256(_canonical(self.to_dict())).hexdigest()


# {
#   責務: [KnowledgeEvaluationRunner: 同じケースで2版の判断器を実行し境界違反と成果を測る]
#   フィールド: [なし: provider、ケース、Knowledgeは呼出しごとに明示される]
# }
class KnowledgeEvaluationRunner:
    # {
    #   責務: [run: 保存版を渡した実Providerを対照実行しoffline検証結果を返す]
    #   処理: [全ケースを事前検証し、順番の偏りを抑えて変更前後を交互実行する。
    #     判断を既存Verifierへ通し、記録済み成果とPython時間/heapを集計する]
    #   引数: [champion/challenger: 同scopeの不変Snapshot, cases: 重複しない最大512ケース,
    #     provider_factory: Snapshotからchoose_context対応Providerを作る信頼済みアプリ側関数]
    #   戻り値: [KnowledgeEvaluationReport: 合格を自己申告させない測定記録]
    #   エラー: [ValueError: scope・ケース・既存tracemalloc sessionが不適合]
    # }
    def run(
        self,
        champion: KnowledgeSnapshot,
        challenger: KnowledgeSnapshot,
        cases: tuple[KnowledgeEvaluationCase, ...],
        provider_factory: Callable,
    ) -> KnowledgeEvaluationReport:
        if not _EVALUATION_TRACE_LOCK.acquire(blocking=False):
            raise ValueError("another knowledge evaluation is measuring Python memory")
        try:
            return self._run_locked(champion, challenger, cases, provider_factory)
        finally:
            _EVALUATION_TRACE_LOCK.release()

    # {
    #   責務: [_run_locked: process共通heap計測を占有した状態で対照ケースを実行する]
    #   処理: [入力検証後、変更前後の判断を実行して群ごとに成果と実測資源を集約する]
    #   引数: [champion/challenger: 同scopeの不変版, cases: 根拠付きの有界ケース,
    #     provider_factory: 信頼済みアプリ側の判断器生成関数]
    #   戻り値: [KnowledgeEvaluationReport: offline測定記録]
    #   エラー: [ValueError: 入力不整合または外部tracemalloc sessionが存在]
    # }
    def _run_locked(
        self,
        champion: KnowledgeSnapshot,
        challenger: KnowledgeSnapshot,
        cases: tuple[KnowledgeEvaluationCase, ...],
        provider_factory: Callable,
    ) -> KnowledgeEvaluationReport:
        if champion.scope != challenger.scope:
            raise ValueError("knowledge comparison must use the same scope")
        cases = tuple(cases)
        if not 1 <= len(cases) <= MAX_EVALUATION_CASES:
            raise ValueError("knowledge comparison case count is outside its budget")
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("knowledge comparison repeats a case")
        if len({case.evidence_sha256 for case in cases}) != len(cases):
            raise ValueError("knowledge comparison repeats outcome evidence")
        if any(case.scope != champion.scope for case in cases):
            raise ValueError("knowledge comparison contains another scope")
        if tracemalloc.is_tracing():
            raise ValueError(
                "run knowledge evaluation outside another tracemalloc session"
            )
        suite_payload = [
            {
                "id": case.case_id,
                "group": case.group,
                "context": case.context.to_dict(),
                "rewards": case.reward_by_action,
                "evidence_sha256": case.evidence_sha256,
            }
            for case in cases
        ]
        suite_digest = sha256(_canonical(suite_payload)).hexdigest()
        qualities: dict[str, list[list[float]]] = {
            group: [[], []] for group in EVALUATION_GROUPS
        }
        invalid = [0, 0]
        errors = [0, 0]
        latencies: list[list[float]] = [[], []]
        peaks = [0, 0]
        for index, case in enumerate(cases):
            order = (0, 1) if index % 2 == 0 else (1, 0)
            for variant in order:
                snapshot = champion if variant == 0 else challenger
                reward, bad, failed, latency, peak = _run_trial(
                    snapshot, case, provider_factory
                )
                qualities[case.group][variant].append(reward)
                invalid[variant] += bad
                errors[variant] += failed
                latencies[variant].append(latency)
                peaks[variant] = max(peaks[variant], peak)
        groups = tuple(
            (
                group,
                len(values[0]),
                statistics.mean(values[0]),
                statistics.mean(values[1]),
            )
            for group, values in sorted(qualities.items())
            if values[0]
        )
        return KnowledgeEvaluationReport(
            champion.scope,
            champion.sha256,
            challenger.sha256,
            suite_digest,
            len(cases),
            groups,
            tuple(invalid),
            tuple(errors),
            tuple(_p95(values) for values in latencies),
            tuple(peaks),
        )


# {
#   責務: [_run_trial: 1版の判断器を入力せず実行し、Verifierを通った成果だけを採点する]
#   処理: [contextをコピーしてSnapshotからProviderを作る。判断器が壊れても失敗として記録し、heap計測を必ず終了する]
#   引数: [snapshot: 評価対象版, case: 操作別の既知成果を持つケース, factory: 信頼済み判断器生成関数]
#   戻り値: [tuple: 成果、境界違反件数、例外件数、経過ms、最大Python割当byte]
# }
def _run_trial(
    snapshot: KnowledgeSnapshot, case: KnowledgeEvaluationCase, factory: Callable
) -> tuple:
    context = deepcopy(case.context)
    observation = ScreenObservation(
        str(context.state.get("screen_id", "")),
        context.state["size"][0],
        context.state["size"][1],
        features={"signature": context.state.get("signature", "")},
    )
    reward, invalid, failed = -1.0, 0, 0
    tracemalloc.start()
    started = time.perf_counter_ns()
    try:
        try:
            provider = factory(snapshot)
            decision = provider.choose_context(context)
            reliability = DecisionVerifier().verify(decision, case.context, observation)
            if reliability.status in {
                ReliabilityStatus.TRUST,
                ReliabilityStatus.CAUTION,
            }:
                reward = dict(case.reward_by_action)[decision.action_id]
            else:
                invalid = 1
        except BaselineWithheld:
            reward = 0.0
        except Exception:
            failed = 1
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return reward, invalid, failed, elapsed, peak


# {
#   責務: [_canonical: 評価digestの入力を一意のJSON bytesへ固定する]
#   処理: [キー順を固定し、非有限数を拒否してUTF-8化する]
#   引数: [value: 評価ケースまたは集計値]
#   戻り値: [bytes: digest用正規化JSON]
#   エラー: [ValueError/TypeError: JSON契約外の値]
# }
def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


# {
#   責務: [_digest: 外部の根拠参照がSHA-256表記か判定する]
#   処理: [長さとhex文字だけを照合する]
#   引数: [value: 出所digest候補]
#   戻り値: [bool: 64文字の小文字hexならTrue]
# }
def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


# {
#   責務: [_p95: 実測時間列のnearest-rank p95を返す]
#   処理: [昇順にした95%位置を採用する]
#   引数: [values: 1件以上の経過ミリ秒]
#   戻り値: [float: 観測されたp95値]
# }
def _p95(values: list[float]) -> float:
    from math import ceil

    return sorted(values)[ceil(len(values) * 0.95) - 1]
