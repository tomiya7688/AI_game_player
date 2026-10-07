import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


@dataclass(frozen=True)
class EvaluatorEvidence:
    evaluator: str
    score: float
    confidence: float
    reliability: float
    evidence: str

    def __post_init__(self) -> None:
        if not self.evaluator.strip():
            raise ValueError("evaluator name must not be empty")
        if not isfinite(self.score) or not -1.0 <= self.score <= 1.0:
            raise ValueError("evaluation score must be between -1 and 1")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("evaluation confidence must be between 0 and 1")
        if not isfinite(self.reliability) or not 0.0 <= self.reliability <= 1.0:
            raise ValueError("evaluator reliability must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class KnowledgeEvidence:
    evidence_id: str
    source: str
    statement: str
    confidence: float
    provenance: str
    category: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.source.strip() or not self.provenance.strip():
            raise ValueError("knowledge evidence metadata must not be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("knowledge confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class CandidateDecisionContext:
    action_id: str
    kind: str
    label: str
    recognition_confidence: float
    allowed: bool
    evaluations: tuple[EvaluatorEvidence, ...]
    utility_score: float
    utility_confidence: float
    evaluator_conflict: bool
    knowledge: tuple[KnowledgeEvidence, ...]
    uncertainty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "label": self.label,
            "recognition_confidence": self.recognition_confidence,
            "allowed": self.allowed,
            "evaluation": {
                "score": self.utility_score,
                "confidence": self.utility_confidence,
                "conflict": self.evaluator_conflict,
                "evidence": [value.to_dict() for value in self.evaluations],
            },
            "knowledge": [value.to_dict() for value in self.knowledge],
            "uncertainty": list(self.uncertainty),
        }


@dataclass(frozen=True)
class DecisionContext:
    snapshot_id: str
    state: dict[str, Any]
    candidates: tuple[CandidateDecisionContext, ...]
    allowed_action_ids: tuple[str, ...]
    previous_outcome: dict[str, Any]
    recent_history: tuple[dict[str, Any], ...]
    goal: dict[str, str]
    uncertainty: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "decision-context/v1",
            "snapshot_id": self.snapshot_id,
            "state": self.state,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "allowed_action_ids": list(self.allowed_action_ids),
            "previous_outcome": self.previous_outcome,
            "recent_history": list(self.recent_history),
            "goal": self.goal,
            "uncertainty": list(self.uncertainty),
        }


class CandidateContextEvaluator(Protocol):
    name: str
    reliability: float

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        ...


class SafetyContextEvaluator:
    name = "safety"
    reliability = 1.0

    def __init__(self, evaluator: ActionEvaluator | None = None) -> None:
        self.evaluator = evaluator or ActionEvaluator()

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        report = self.evaluator.explain(observation, [candidate])[0]
        accepted = bool(report["accepted"])
        return EvaluatorEvidence(
            self.name,
            0.0 if accepted else -1.0,
            1.0,
            self.reliability,
            str(report["reason"]),
        )


class RepetitionContextEvaluator:
    name = "repetition"
    reliability = 0.9

    def __init__(self, window: int = 5) -> None:
        if window < 1:
            raise ValueError("repetition window must be positive")
        self.window = window

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        history = recent_history[-self.window :]
        repeats = sum(1 for entry in history if str(entry.get("action_id", "")) == candidate.action_id)
        if repeats == 0:
            return EvaluatorEvidence(self.name, 0.0, 0.8, self.reliability, "action not present in recent history")
        state_changed = _state_changed_from_history(observation, history[-1] if history else None)
        stalled = previous_outcome.status in {"failure", "ongoing", "unknown"} and not state_changed
        penalty = min(1.0, 0.25 * repeats + (0.5 if stalled else 0.0))
        reason = f"recent repeats={repeats}; state_changed={state_changed}; previous_outcome={previous_outcome.status}"
        return EvaluatorEvidence(self.name, -penalty, max(0.5, previous_outcome.confidence), self.reliability, reason)


class EvaluationFusion:
    def __init__(self, conflict_threshold: float = 0.35) -> None:
        if not 0 <= conflict_threshold <= 1:
            raise ValueError("conflict threshold must be between 0 and 1")
        self.conflict_threshold = conflict_threshold

    def fuse(self, evaluations: list[EvaluatorEvidence]) -> tuple[float, float, bool]:
        weighted = [(entry, entry.confidence * entry.reliability) for entry in evaluations]
        denominator = sum(weight for _, weight in weighted)
        if denominator == 0:
            score = 0.0
            confidence = 0.0
        else:
            score = sum(entry.score * weight for entry, weight in weighted) / denominator
            confidence = denominator / max(1.0, sum(entry.reliability for entry in evaluations))
        positive = any(entry.score >= self.conflict_threshold and entry.confidence > 0 for entry in evaluations)
        negative = any(entry.score <= -self.conflict_threshold and entry.confidence > 0 for entry in evaluations)
        return round(score, 6), round(min(1.0, confidence), 6), positive and negative


class CandidateKnowledgeRetriever:
    def __init__(self, store: KnowledgeStore | None = None, limit: int = 3) -> None:
        if limit < 0:
            raise ValueError("knowledge limit must not be negative")
        self.store = store
        self.limit = limit

    def retrieve(self, candidate: ActionCandidate) -> list[KnowledgeEvidence]:
        if self.store is None or self.limit == 0:
            return []
        queries = [candidate.label, candidate.action_id]
        matches: list[dict[str, object]] = []
        seen: set[str] = set()
        for query in queries:
            if not query.strip():
                continue
            for entry in self.store.search(query):
                evidence_id = str(entry.get("id", ""))
                if not evidence_id or evidence_id in seen:
                    continue
                seen.add(evidence_id)
                matches.append(entry)
                if len(matches) >= self.limit:
                    break
            if len(matches) >= self.limit:
                break
        provenance = str(self.store.path)
        return [
            KnowledgeEvidence(
                str(entry["id"]),
                "knowledge_store",
                str(entry.get("statement", "")),
                _confidence(entry.get("confidence", 0.0)),
                provenance,
                str(entry.get("category", "")),
            )
            for entry in matches
        ]


class DecisionContextBuilder:
    """Builds the compact state/evaluation/knowledge package consumed by a decision provider."""

    def __init__(
        self,
        knowledge_store: KnowledgeStore | None = None,
        evaluators: list[CandidateContextEvaluator] | None = None,
        fusion: EvaluationFusion | None = None,
        history_limit: int = 5,
        knowledge_limit: int = 3,
    ) -> None:
        if history_limit < 0:
            raise ValueError("history limit must not be negative")
        self.evaluators = list(evaluators) if evaluators is not None else [SafetyContextEvaluator(), RepetitionContextEvaluator()]
        self.fusion = fusion or EvaluationFusion()
        self.history_limit = history_limit
        self.knowledge = CandidateKnowledgeRetriever(knowledge_store, knowledge_limit)

    def build(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        allowed_candidates: list[ActionCandidate],
        *,
        recent_history: list[dict[str, Any]] | None = None,
        previous_outcome: OutcomeAssessment | None = None,
        current_goal: str = "",
        short_term_goal: str = "",
    ) -> DecisionContext:
        history = [entry for entry in recent_history or [] if _is_action_history_entry(entry)]
        history = history[-self.history_limit :] if self.history_limit else []
        outcome = previous_outcome or OutcomeAssessment("unknown", 0.0, "no previous action")
        allowed_ids = {candidate.action_id for candidate in allowed_candidates}
        candidate_contexts: list[CandidateDecisionContext] = []
        global_uncertainty: list[str] = []
        for candidate in candidates:
            evaluations = [evaluator.evaluate(observation, candidate, history, outcome) for evaluator in self.evaluators]
            score, confidence, conflict = self.fusion.fuse(evaluations)
            knowledge = self.knowledge.retrieve(candidate)
            uncertainty: list[str] = []
            if conflict:
                uncertainty.append("evaluator_conflict")
            if confidence < 0.5:
                uncertainty.append("low_evaluation_confidence")
            if candidate.confidence < 0.6:
                uncertainty.append("low_recognition_confidence")
            if not evaluations:
                uncertainty.append("missing_evaluation")
            if not knowledge:
                uncertainty.append("no_candidate_knowledge")
            candidate_contexts.append(
                CandidateDecisionContext(
                    candidate.action_id,
                    candidate.kind,
                    candidate.label,
                    candidate.confidence,
                    candidate.action_id in allowed_ids,
                    tuple(evaluations),
                    score,
                    confidence,
                    conflict,
                    tuple(knowledge),
                    tuple(uncertainty),
                )
            )
        if not allowed_ids:
            global_uncertainty.append("no_allowed_actions")
        if any(candidate.evaluator_conflict for candidate in candidate_contexts):
            global_uncertainty.append("candidate_evaluator_conflict")
        if outcome.confidence < 0.5:
            global_uncertainty.append("previous_outcome_uncertain")
        return DecisionContext(
            uuid4().hex,
            _state_summary(observation),
            tuple(candidate_contexts),
            tuple(candidate.action_id for candidate in allowed_candidates),
            {"status": outcome.status, "confidence": outcome.confidence, "reason": outcome.reason, "state_changed": _state_changed_from_history(observation, history[-1] if history else None)},
            tuple(_history_summary(entry) for entry in history),
            {"current_goal": current_goal, "short_term_goal": short_term_goal},
            tuple(global_uncertainty),
        )


# {
#   責務: [
#     DecisionTraceStore: 画面状態、判断、判断の信頼性検査、操作結果を一件の履歴に結び付けて保存する
#   ]
#   フィールド: [
#     path: 判断履歴のJSON配列を読み書きするファイルの場所
#   ]
#   処理: [
#     1: 許可候補から選ばれた判断に、画面状態と選択理由を添えて追加する
#     2: 拒否された判断や実行前に停止した理由も残し、行動履歴と区別する
#     3: 操作後に届いた結果を、画面IDと操作IDが一致する判断へ結び付ける
#     4: 一時ファイルへ全件を書いてから置き換え、途中終了で履歴が半端になるのを防ぐ
#   ]
# }
class DecisionTraceStore:

    # {
    #   責務: [
    #     __init__: 判断履歴を保存するファイルの場所を記録し、このStoreの書き込み先を決める
    #   ]
    #   引数: [
    #     path: 画面・判断・操作結果をJSON配列として保存するファイルの場所
    #   ]
    #   戻り値: [なし。保存先をインスタンスに保持する]
    # }
    def __init__(self, path: Path) -> None:
        self.path = path

    # {
    #   責務: [
    #     append: REJECTされず記録対象となった判断に、参照した画面・候補・検査記録を添えて履歴へ追加する
    #   ]
    #   処理: [
    #     1: 既存履歴を読み込み、選択操作・画面状態・判断材料を一件にまとめる
    #     2: 任意の信頼性検査記録がある場合は同じ履歴に関連付ける
    #     3: 一時ファイルへ全件を書き、完成後に履歴ファイルと置き換える
    #   ]
    #   引数: [
    #     context: モデルへ渡した画面状態、操作候補、目標、過去の操作結果
    #     decision: 拒否されず候補一覧から選ばれた操作とその理由。追加確認が必要な状態も記録できる
    #     reliability: 選択が画面や許可候補と矛盾しないかを調べた記録。未実施なら省略する
    #   ]
    #   戻り値: [なし。履歴ファイルを更新する。保存に失敗した場合は書き込み例外を呼び出し元へ返す]
    # }
    def append(
        self,
        context: DecisionContext,
        decision: ActionDecision,
        *,
        reliability: dict[str, Any] | None = None,
    ) -> None:
        entries = self._read()
        trace_entry: dict[str, Any] = {
            "snapshot_id": context.snapshot_id,
            "screen_id": context.state.get("screen_id", ""),
            "state_signature": context.state.get("signature", ""),
            "action_id": decision.action_id,
            "decision": decision.to_dict(),
            "previous_outcome": context.previous_outcome,
            "used_evidence_ids": [
                evidence.evidence_id
                for candidate in context.candidates
                if candidate.action_id == decision.action_id
                for evidence in candidate.knowledge
            ],
            "context": context.to_dict(),
        }
        if reliability is not None:
            trace_entry["reliability"] = reliability
        entries.append(trace_entry)
        self._write(entries)

    # {
    #   責務: [
    #     mark_action_not_executed: 実行前の安全確認などで中止した判断を、実行済み操作と誤認されない状態に更新する
    #   ]
    #   処理: [
    #     1: 新しい履歴から順に、指定画面と操作の判断記録を探す
    #     2: 該当記録に停止状態と理由を保存し、見つからなければ何も変更しない
    #     3: 停止した操作が次回の成功済み行動履歴に混ざらないようにする
    #   ]
    #   引数: [
    #     snapshot_id: 中止した判断を特定する、判断時点の画面ID
    #     action_id: 中止した操作候補のID
    #     reason: 実行しなかった理由。安全判定や実行条件など、後から監査できる説明を渡す
    #   ]
    #   戻り値: [
    #     bool: 一致する判断を見つけて停止状態を保存した場合True。該当履歴がなければFalse
    #   ]
    # }
    def mark_action_not_executed(self, snapshot_id: str, action_id: str, reason: str) -> bool:
        entries = self._read()
        for entry in reversed(entries):
            if (
                entry.get("snapshot_id") == snapshot_id
                and entry.get("action_id") == action_id
                and isinstance(entry.get("decision"), dict)
            ):
                entry["execution_status"] = "blocked"
                entry["execution_block_reason"] = reason
                self._write(entries)
                return True
        return False

    # {
    #   責務: [
    #     append_rejection: 信頼性検査で拒否した判断を、実行可能な選択と混同しない履歴として保存する
    #   ]
    #   処理: [
    #     1: 拒否時に使った画面状態と検査結果を一件にまとめる
    #     2: 実行する判断が存在しないことをdecision=nullで記録する
    #     3: 一時ファイルへ全件を書き、完成後に履歴ファイルと置き換える
    #   ]
    #   引数: [
    #     context: モデルに提示した候補や画面状態など、拒否判定の基になった情報
    #     reliability: 不許可の理由と検査根拠を含む、信頼性判定の辞書
    #   ]
    #   戻り値: [なし。履歴ファイルを更新する。保存に失敗した場合は書き込み例外を呼び出し元へ返す]
    # }
    def append_rejection(self, context: DecisionContext, reliability: dict[str, Any]) -> None:
        entries = self._read()
        entries.append(
            {
                "snapshot_id": context.snapshot_id,
                "screen_id": context.state.get("screen_id", ""),
                "state_signature": context.state.get("signature", ""),
                "action_id": reliability.get("action_id", ""),
                "decision": None,
                "reliability": reliability,
                "context": context.to_dict(),
            }
        )
        self._write(entries)

    # {
    #   責務: [
    #     record_action_outcome: 実行後の観測結果を、元の画面で選んだ操作の判断記録へ結び付ける
    #   ]
    #   処理: [
    #     1: 新しい履歴から順に、画面ID・操作IDが一致し実行対象となる判断を探す
    #     2: 該当する判断に、進行・失敗・状態変化などの観測結果を記録する
    #     3: 履歴を更新して保存し、該当記録がなければFalseを返す
    #   ]
    #   引数: [
    #     snapshot_id: 操作を選択した画面のID。後続の同じ操作と取り違えないために使う
    #     action_id: 結果を記録する操作候補のID
    #     outcome: 操作後の画面比較から得た結果の辞書
    #   ]
    #   戻り値: [
    #     bool: 一致する実行判断を更新できた場合True。該当履歴がない場合False
    #   ]
    # }
    def record_action_outcome(self, snapshot_id: str, action_id: str, outcome: dict[str, Any]) -> bool:
        entries = self._read()
        for entry in reversed(entries):
            if (
                entry.get("snapshot_id") == snapshot_id
                and entry.get("action_id") == action_id
                and _is_action_history_entry(entry)
            ):
                entry["action_outcome"] = outcome
                self._write(entries)
                return True
        return False

    # {
    #   責務: [次の判断に渡すため、保存順を保った直近の判断記録を返す]
    #   処理: [
    #     1: 保存済み履歴を読み込む
    #     2: 指定件数を超える古い項目を除き、残りを古い順で返す
    #   ]
    #   引数: [limit: 返す記録の最大件数。0なら空の一覧を返す]
    #   戻り値: [list[dict[str, Any]]: 指定件数以内の最新履歴。並び順は古い項目から新しい項目]
    #   エラー: [ValueError: limitが負数の場合]
    # }
    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("history limit must not be negative")
        return self._read()[-limit:] if limit else []

    # {
    #   責務: [
    #     recent_actions: 次の判断で繰り返しを避けるため、実行対象になった直近の操作履歴だけを返す
    #   ]
    #   処理: [
    #     1: 拒否記録と実行前に止めた操作を履歴全件から除く
    #     2: 残った記録の末尾から指定件数を返す
    #   ]
    #   引数: [
    #     limit: 返す操作履歴の最大件数。0なら空の一覧を返す
    #   ]
    #   戻り値: [
    #     list[dict[str, Any]]: 古いものから新しいものの順に並ぶ、実行対象の判断記録
    #   ]
    #   エラー: [
    #     ValueError: limitが負数の場合。履歴件数として負数を指定できない
    #   ]
    # }
    def recent_actions(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("history limit must not be negative")
        if limit == 0:
            return []
        action_entries = [entry for entry in self._read() if _is_action_history_entry(entry)]
        return action_entries[-limit:]

    # {
    #   責務: [保存ファイルを読み、履歴として扱える辞書項目だけを呼び出し元へ渡す]
    #   処理: [
    #     1: ファイルがなければ、まだ履歴がないものとして空の一覧を返す
    #     2: JSON配列を読み込み、配列以外なら履歴形式の誤りとして拒否する
    #     3: 配列内から辞書ではない値を除き、履歴項目だけを返す
    #   ]
    #   引数: []
    #   戻り値: [list[dict[str, Any]]: 保存順に並んだ、辞書形式の履歴項目]
    #   エラー: [ValueError: ファイルの最上位がJSON配列でない場合。JSON解析や読み込みの例外も呼び出し元へ伝える]
    # }
    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("decision trace must contain an array")
        return [entry for entry in value if isinstance(entry, dict)]

    # {
    #   責務: [履歴全件を一時ファイルへ書き切ってから置き換え、途中失敗で既存履歴を壊さないよう保存する]
    #   処理: [
    #     1: 保存先の親ディレクトリがなければ作成する
    #     2: Unicodeをそのまま保持した整形JSONを一時ファイルへ書く
    #     3: 書き込み完了後に一時ファイルを保存先へ置き換える
    #   ]
    #   引数: [entries: 保存する判断履歴全件。各項目は辞書形式]
    #   戻り値: [なし。ディスクへの置き換えが成功した後に戻る]
    #   エラー: [ディレクトリ作成・書き込み・置き換えに失敗した場合は、入出力例外を呼び出し元へ伝える]
    # }
    def _write(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def _state_summary(observation: ScreenObservation) -> dict[str, Any]:
    features = observation.features
    visible_text: list[str] = []
    remaining = 160
    for raw in observation.ocr_text[:8]:
        text = " ".join(str(raw).split())
        if not text or remaining <= 0:
            continue
        text = text[:remaining]
        visible_text.append(text)
        remaining -= len(text)
    detected = features.get("detected_elements", [])
    return {
        "screen_id": observation.screen_id,
        "size": [observation.width, observation.height],
        "signature": str(features.get("signature", "")),
        "perceptual_hash": str(features.get("perceptual_hash", "")),
        "mean_brightness": features.get("mean_brightness"),
        "visible_text": visible_text,
        "detected_element_count": len(detected) if isinstance(detected, list) else 0,
    }


def _history_summary(entry: dict[str, Any]) -> dict[str, Any]:
    previous = entry.get("previous_outcome", {})
    action_outcome = entry.get("action_outcome")
    if isinstance(action_outcome, dict):
        outcome_status = str(action_outcome.get("status", "unknown"))
        state_changed = bool(action_outcome.get("state_changed", outcome_status == "changed"))
    else:
        outcome_status = str(previous.get("status", "unknown")) if isinstance(previous, dict) else "unknown"
        state_changed = bool(previous.get("state_changed", False)) if isinstance(previous, dict) else False
    return {
        "snapshot_id": str(entry.get("snapshot_id", "")),
        "screen_id": str(entry.get("screen_id", "")),
        "state_signature": str(entry.get("state_signature", "")),
        "action_id": str(entry.get("action_id", "")),
        "outcome": outcome_status,
        "state_changed": state_changed,
    }


# {
#   責務: [
#     _is_action_history_entry: 実際に実行対象となった判断だけを選び、拒否・中止記録を操作履歴から外す
#   ]
#   処理: [
#     1: 選択された操作の記録がない項目を除外する
#     2: 信頼性検査でREJECTになった項目を除外する
#     3: 実行前に停止状態へ更新された項目を除外する
#   ]
#   引数: [
#     entry: 画面状態・判断・実行結果などを含む履歴の1件
#   ]
#   戻り値: [
#     bool: 次の判断で過去の操作として参照する記録ならTrue
#   ]
# }
def _is_action_history_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry.get("decision"), dict):
        return False
    if entry.get("execution_status") == "blocked":
        return False
    reliability = entry.get("reliability")
    if not isinstance(reliability, dict):
        return True
    return str(reliability.get("status", "")).upper() != "REJECT"


def _state_changed_from_history(observation: ScreenObservation, previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    current_signature = str(observation.features.get("signature", ""))
    previous_signature = str(previous.get("state_signature", ""))
    if current_signature and previous_signature:
        return current_signature != previous_signature
    current_hash = str(observation.features.get("perceptual_hash", ""))
    previous_context = previous.get("context", {})
    previous_state = previous_context.get("state", {}) if isinstance(previous_context, dict) else {}
    previous_hash = str(previous_state.get("perceptual_hash", "")) if isinstance(previous_state, dict) else ""
    if current_hash and previous_hash:
        return current_hash != previous_hash
    return str(previous.get("screen_id", "")) != observation.screen_id


def _confidence(value: object) -> float:
    result = float(value)
    if not isfinite(result):
        return 0.0
    return max(0.0, min(1.0, result))
