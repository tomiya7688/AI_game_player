import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.deferred_trace_append import DeferredTraceAppend
from ai_game_player.journal_adapters import LegacyEventAdapter, StagedJournalAppend
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.knowledge_projection import KnowledgeProjection
from ai_game_player.execution_receipt import ExecutionReceipt
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


@dataclass(frozen=True)
# {
#   責務: [EvaluatorEvidence: 1つの評価器が1候補について出した点数・根拠・確信度をまとめる]
#   フィールド: [evaluator: 評価器名, score: 候補の評価値（-1は否定、0は中立、1は肯定）, confidence: 評価の確かさ, reliability: 評価器自体の信頼度, evidence: 点数の理由]
# }
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
# {
#   責務: [KnowledgeEvidence: 候補を選ぶ理由として参照した知識の本文と出所を保持する]
#   フィールド: [evidence_id: 知識記録ID, source: 収集元, statement: 判断に使う知識本文, confidence: 知識の確かさ, provenance: 保存先や参照元, category: 知識の分類名]
# }
class KnowledgeEvidence:
    evidence_id: str
    source: str
    statement: str
    confidence: float
    provenance: str
    category: str = ""

    def __post_init__(self) -> None:
        if (
            not self.evidence_id.strip()
            or not self.source.strip()
            or not self.provenance.strip()
        ):
            raise ValueError("knowledge evidence metadata must not be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("knowledge confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
# {
#   責務: [CandidateDecisionContext: 画面で見つかった1候補について、許可状態・評価根拠・参考知識をまとめる]
#   フィールド: [action_id: 候補ID, kind: クリックやキー入力など操作の種類, label: 画面上の表示名, recognition_confidence: 画面解析が候補を正しく見つけた確信度, allowed: 独立した候補評価器が実行可能としたか, evaluations: 候補ごとの点数と理由, utility_score: 目標への見込み, utility_confidence: 見込みの確かさ, evaluator_conflict: 評価器同士の結論が割れたか, knowledge: 判断に使う知識, uncertainty: 残る不確実性]
# }
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
# {
#   責務: [DecisionContext: 1回の選択に使った画面・許可候補・過去の結果を同じ観測IDに結び付ける]
#   フィールド: [snapshot_id: 判断対象の画面観測ID, state: 画面状態の要約, candidates: 検出した候補と評価根拠, allowed_action_ids: 実行可能と評価された候補ID, previous_outcome: 直前に実行した候補の結果, recent_history: 過去の判断履歴, goal: 最終目標と短期目標, uncertainty: 判断材料に残る不確実性, knowledge_projection: このstepだけの任意soft evidence。実観測と区別する]
# }
class DecisionContext:
    snapshot_id: str
    state: dict[str, Any]
    candidates: tuple[CandidateDecisionContext, ...]
    allowed_action_ids: tuple[str, ...]
    previous_outcome: dict[str, Any]
    recent_history: tuple[dict[str, Any], ...]
    goal: dict[str, str]
    uncertainty: tuple[str, ...]
    knowledge_projection: KnowledgeProjection | None = None

    # {
    #   責務: [to_dict: 判断材料をモデル要求またはローカルTrace向けの独立辞書に変換する]
    #   処理: [Knowledgeは公開opt-in時、またはローカルTraceへの明示要求時だけ含める]
    #   引数: [include_knowledge: Trueはローカル監査用。モデル送信では既定Falseを使う]
    #   戻り値: [dict: 観測とKnowledgeの出所を分離したJSON化可能な判断材料]
    # }
    def to_dict(self, *, include_knowledge: bool = False) -> dict[str, Any]:
        value = {
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
        if self.knowledge_projection is not None and (
            include_knowledge or self.knowledge_projection.expose_to_models
        ):
            value["knowledge_projection"] = (
                self.knowledge_projection.to_dict()
                if include_knowledge
                else self.knowledge_projection.to_model_dict()
            )
        return value


class CandidateContextEvaluator(Protocol):
    name: str
    reliability: float

    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence: ...


class SafetyContextEvaluator:
    name = "safety"
    reliability = 1.0

    def __init__(self, evaluator: ActionEvaluator | None = None) -> None:
        self.evaluator = evaluator or ActionEvaluator()

    # {
    #   責務: [evaluate: 候補1件の実行可否を既存ActionEvaluatorへ問い合わせ、判断材料用の証拠へ変える]
    #   引数: [observation: 候補座標と画面範囲を比べる観測, candidate: 実行可否を調べる候補, recent_history: 直近履歴（この評価器では未使用）, previous_outcome: 直前操作の結果（この評価器では未使用）]
    #   戻り値: [EvaluatorEvidence: 許可なら中立、拒否なら否定点を持つ評価と既存の理由]
    # }
    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        report = self.evaluator.explain(observation, [candidate])[0]
        return self._evidence_from_report(report)

    # {
    #   責務: [evaluate_batch: 同じ候補集合に対するSafety結果を候補ごとに返す]
    #   処理: [全候補を一括評価し、重複action IDの拒否理由を候補単位の証拠へ変換する]
    #   引数: [observation: 候補座標を検証する画面観測, candidates: 同一batchとして評価する候補集合]
    #   戻り値: [list[EvaluatorEvidence]: 入力候補と同じ順序のSafety評価証拠]
    # }
    def evaluate_batch(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
    ) -> list[EvaluatorEvidence]:
        reports = self.evaluator.explain(observation, candidates)
        return [self._evidence_from_report(report) for report in reports]

    # {
    #   責務: [_evidence_from_report: ActionEvaluatorの1件分の報告をDecision Context用の証拠へ変換する]
    #   処理: [許可状態をscoreへ、拒否理由を監査文字列へ対応付ける]
    #   引数: [report: acceptedとreasonを含むActionEvaluatorの候補別報告]
    #   戻り値: [EvaluatorEvidence: Decision Contextへ保存するSafety証拠]
    # }
    def _evidence_from_report(self, report: dict[str, object]) -> EvaluatorEvidence:
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

    # {
    #   責務: [evaluate: 同じ候補が続き、直前の操作後に画面が進んでいない場合に繰返し点を下げる]
    #   引数: [observation: 現在画面, candidate: 点数を付ける操作候補, recent_history: 実行前に止めていない過去の選択, previous_outcome: 直前操作の成功・失敗・進行状況]
    #   戻り値: [EvaluatorEvidence: 繰返し回数と画面変化から求めた点数・確信度・理由]
    # }
    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        recent_history: list[dict[str, Any]],
        previous_outcome: OutcomeAssessment,
    ) -> EvaluatorEvidence:
        history = recent_history[-self.window :]
        repeats = sum(
            1
            for entry in history
            if str(entry.get("action_id", "")) == candidate.action_id
        )
        if repeats == 0:
            return EvaluatorEvidence(
                self.name,
                0.0,
                0.8,
                self.reliability,
                "action not present in recent history",
            )
        state_changed = _state_changed_from_history(
            observation, history[-1] if history else None
        )
        stalled = (
            previous_outcome.status in {"failure", "ongoing", "unknown"}
            and not state_changed
        )
        penalty = min(1.0, 0.25 * repeats + (0.5 if stalled else 0.0))
        reason = f"recent repeats={repeats}; state_changed={state_changed}; previous_outcome={previous_outcome.status}"
        return EvaluatorEvidence(
            self.name,
            -penalty,
            max(0.5, previous_outcome.confidence),
            self.reliability,
            reason,
        )


class EvaluationFusion:
    def __init__(self, conflict_threshold: float = 0.35) -> None:
        if not 0 <= conflict_threshold <= 1:
            raise ValueError("conflict threshold must be between 0 and 1")
        self.conflict_threshold = conflict_threshold

    def fuse(self, evaluations: list[EvaluatorEvidence]) -> tuple[float, float, bool]:
        weighted = [
            (entry, entry.confidence * entry.reliability) for entry in evaluations
        ]
        denominator = sum(weight for _, weight in weighted)
        if denominator == 0:
            score = 0.0
            confidence = 0.0
        else:
            score = (
                sum(entry.score * weight for entry, weight in weighted) / denominator
            )
            confidence = denominator / max(
                1.0, sum(entry.reliability for entry in evaluations)
            )
        positive = any(
            entry.score >= self.conflict_threshold and entry.confidence > 0
            for entry in evaluations
        )
        negative = any(
            entry.score <= -self.conflict_threshold and entry.confidence > 0
            for entry in evaluations
        )
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


# {
#   責務: [DecisionContextBuilder: 現在画面・候補評価・参考知識・実行履歴を、判断元へ渡す1件の判断材料へまとめる]
#   保持状態: [evaluators: 候補ごとの評価器, fusion: 評価点と確信度をまとめる処理, history_limit: 判断元へ渡す実行前停止後でない履歴の最大件数, knowledge: 候補に関連する参考知識の検索器]
#   処理: [候補ID・許可状態・評価根拠を同じ画面観測IDに結び、結果と不確実性をJSON形式へ変換できる状態で返す]
# }
class DecisionContextBuilder:
    # {
    #   責務: [__init__: 使用する評価器、評価統合規則、履歴件数、参考知識件数を設定する]
    #   引数: [knowledge_store: 候補に関連する説明を検索する保存先, evaluators: 点数と理由を出す評価器一覧, fusion: 評価点・確信度・意見不一致を統合する処理, history_limit: 参照する履歴の最大件数, knowledge_limit: 候補ごとに返す知識の最大件数]
    #   戻り値: []
    #   エラー: [ValueError: 履歴件数が負数の場合]
    # }

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
        self.evaluators = (
            list(evaluators)
            if evaluators is not None
            else [SafetyContextEvaluator(), RepetitionContextEvaluator()]
        )
        self.fusion = fusion or EvaluationFusion()
        self.history_limit = history_limit
        self.knowledge = CandidateKnowledgeRetriever(knowledge_store, knowledge_limit)

    # {
    #   責務: [build: 観測・候補・評価・知識をDecision Provider向けsnapshotへまとめる]
    #   処理: [Safety評価は同じ候補batchで一度行い、他の評価器は候補ごとに実行して証拠を統合する]
    #   引数: [observation: 判断対象画面, candidates: SafetyとDecision Contextへ渡す全候補, allowed_candidates: 判断可能と確定した候補, recent_history: 直近の判断履歴, previous_outcome: 直前操作の結果, current_goal: 利用者の現在目標, short_term_goal: 次に達成する短期目標]
    #   戻り値: [DecisionContext: Providerへ渡す圧縮済みの判断snapshot]
    # }
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
        history = [
            entry for entry in recent_history or [] if _is_action_history_entry(entry)
        ]
        history = history[-self.history_limit :] if self.history_limit else []
        outcome = previous_outcome or OutcomeAssessment(
            "unknown", 0.0, "no previous action"
        )
        allowed_ids = {candidate.action_id for candidate in allowed_candidates}
        batch_safety_evidence = {
            id(evaluator): evaluator.evaluate_batch(observation, candidates)
            for evaluator in self.evaluators
            if isinstance(evaluator, SafetyContextEvaluator)
            and type(evaluator).evaluate is SafetyContextEvaluator.evaluate
        }
        candidate_contexts: list[CandidateDecisionContext] = []
        global_uncertainty: list[str] = []
        for candidate_index, candidate in enumerate(candidates):
            evaluations = [
                batch_safety_evidence[id(evaluator)][candidate_index]
                if id(evaluator) in batch_safety_evidence
                else evaluator.evaluate(observation, candidate, history, outcome)
                for evaluator in self.evaluators
            ]
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
            {
                "status": outcome.status,
                "confidence": outcome.confidence,
                "reason": outcome.reason,
                "state_changed": _state_changed_from_history(
                    observation, history[-1] if history else None
                ),
            },
            tuple(_history_summary(entry) for entry in history),
            {"current_goal": current_goal, "short_term_goal": short_term_goal},
            tuple(global_uncertainty),
        )


# {
#   責務: [DecisionTraceStore: 画面観測・候補選択・信頼性検査・実行前停止・操作結果を、後から順に追える判断履歴へ保存する]
#   フィールド: [path: 旧JSON形式の履歴保存先, event_adapter: 指定時に実行セッションの追記型記録へ保存し、旧形式の読み取りも提供する変換器]
#   処理: [拒否や実行前停止も監査履歴には残し、次の選択へ渡す履歴からは拒否・実行前停止を除く]
# }
class DecisionTraceStore:
    # {
    #   責務: [__init__: 旧trace JSONの保存先と任意のSession Journal adapterを保持する]
    #   引数: [path: 旧trace JSONの場所, event_adapter: 指定時に新しいtrace eventを追記するJournal adapter]
    #   戻り値: [なし。保存先をインスタンスへ保持する]
    # }
    def __init__(
        self, path: Path, event_adapter: LegacyEventAdapter | None = None
    ) -> None:
        self.path = path
        self.event_adapter = event_adapter

    # {
    #   責務: [append: 判断と任意の信頼性検査結果をtraceへ確定する]
    #   処理: [未確定traceを準備し、Journalまたは旧JSONへ一度だけ公開する]
    #   引数: [context: 判断元へ渡した画面・候補・目標・履歴, decision: 選ばれた許可候補, reliability: 形式と画面整合性を調べた結果。未実施なら省略]
    #   戻り値: [なし。trace保存に失敗した場合は保存例外を呼び出し元へ伝える]
    # }
    def append(
        self,
        context: DecisionContext,
        decision: ActionDecision,
        *,
        reliability: dict[str, Any] | None = None,
    ) -> None:
        staged_write = self.prepare_append(context, decision, reliability=reliability)
        try:
            staged_write.publish()
        finally:
            staged_write.discard()

    # {
    #   責務: [prepare_append: 判断と検査根拠を含むtraceを実行世代の確定前に準備する]
    #   処理: [snapshot・判断・根拠を一件に固定し、Journal/旧JSON用の遅延appendを作る]
    #   引数: [context: providerへ渡した判断context, decision: 選ばれた候補と理由, reliability: 任意の信頼性検査結果, action_generation: 任意の判断開始世代]
    #   戻り値: [DeferredTraceAppend | StagedJournalAppend: guard内で最新履歴へ追記する操作]
    #   エラー: [ValueError/TypeError: traceをJSON化できない場合]
    # }
    def prepare_append(
        self,
        context: DecisionContext,
        decision: ActionDecision,
        *,
        reliability: dict[str, Any] | None = None,
        action_generation: int | None = None,
    ) -> DeferredTraceAppend | StagedJournalAppend:
        entry: dict[str, Any] = {
            "snapshot_id": context.snapshot_id,
            "screen_id": context.state.get("screen_id", ""),
            "state_signature": context.state.get("signature", ""),
            "action_id": decision.action_id,
            "decision": decision.to_dict(),
            "execution_status": "planned",
            "action_generation": action_generation,
            "previous_outcome": context.previous_outcome,
            "used_evidence_ids": [
                evidence.evidence_id
                for candidate in context.candidates
                if candidate.action_id == decision.action_id
                for evidence in candidate.knowledge
            ],
            "context": context.to_dict(include_knowledge=True),
        }
        if reliability is not None:
            entry["reliability"] = reliability
        if self.event_adapter is not None:
            return self.event_adapter.prepare_append(entry)
        return DeferredTraceAppend(entry, self._append_entry)

    # {
    #   責務: [mark_action_not_executed: 実行前に停止した判断へ監査状態を追記する]
    #   処理: [snapshot IDとaction IDでtraceを特定し、Journalではpatch event、旧JSONではatomic置換で停止理由を保存する]
    #   引数: [snapshot_id: 判断時点の画面snapshot ID, action_id: 停止した候補ID, reason: 実行へ進まなかった理由]
    #   戻り値: [bool: 対象traceを更新した場合True。対象判断がなければFalse]
    # }
    def mark_action_not_executed(
        self, snapshot_id: str, action_id: str, reason: str
    ) -> bool:
        entries = self._read()
        for entry in reversed(entries):
            if (
                entry.get("snapshot_id") == snapshot_id
                and entry.get("action_id") == action_id
                and isinstance(entry.get("decision"), dict)
            ):
                receipt = entry.get("execution_receipt")
                if isinstance(receipt, dict) and receipt.get("execution_status") in {
                    "executed",
                    "dispatch_unknown",
                }:
                    return False
                self._update_entry(
                    entries,
                    entry,
                    {"execution_status": "blocked", "execution_block_reason": reason},
                )
                return True
        return False

    # {
    #   責務: [append_rejection: 拒否したprovider判断を実行可能な選択と区別して監査履歴へ保存する]
    #   処理: [decision=nullと信頼性検査結果を含むtrace recordをJournalまたは旧JSONへ追加する]
    #   引数: [context: 拒否判定に使った画面・候補・目標, reliability: 拒否状態と検査根拠]
    #   戻り値: [なし。保存に失敗した場合は保存例外を呼び出し元へ伝える]
    # }
    def append_rejection(
        self, context: DecisionContext, reliability: dict[str, Any]
    ) -> None:
        rejection = {
            "snapshot_id": context.snapshot_id,
            "screen_id": context.state.get("screen_id", ""),
            "state_signature": context.state.get("signature", ""),
            "action_id": reliability.get("action_id", ""),
            "decision": None,
            "reliability": reliability,
            "context": context.to_dict(include_knowledge=True),
        }
        self._append_entry(rejection)

    # {
    #   責務: [_append_entry: 新しいtrace recordをJournalまたは旧JSONへ確定する]
    #   処理: [Journal adapterの遅延appendをpublishし、adapter未指定時は原子的に旧JSONを置換する]
    #   引数: [entry: 追加するdecision trace record]
    #   戻り値: [なし。recordを保存した後に戻る]
    # }
    def _append_entry(self, entry: dict[str, Any]) -> None:
        if self.event_adapter is not None:
            staged_write = self.event_adapter.prepare_append(entry)
            try:
                staged_write.publish()
            finally:
                staged_write.discard()
            return
        entries = self._read()
        entries.append(entry)
        self._write(entries)

    # {
    #   責務: [_update_entry: 操作結果または実行前停止を既存traceへ関連付ける]
    #   処理: [Journalでは対象IDを指すpatchを追記し、旧JSONではrecordを更新して原子的に置換する]
    #   引数: [entries: patch適用済みtrace一覧, target: 更新対象trace, fields: 追加または上書きする監査項目]
    #   戻り値: [なし。更新recordを保存した後に戻る]
    # }
    def _update_entry(
        self,
        entries: list[dict[str, Any]],
        target: dict[str, Any],
        fields: dict[str, Any],
    ) -> None:
        if self.event_adapter is not None:
            self._append_entry(
                {
                    "snapshot_id": target.get("snapshot_id", ""),
                    "action_id": target.get("action_id", ""),
                    "trace_update": fields,
                }
            )
            return
        target.update(fields)
        self._write(entries)

    # {
    #   責務: [record_action_outcome: receiptで実行事実を確認した元の判断へだけ観測結果を関連付ける]
    #   処理: [snapshot・操作・世代を照合し、dispatch_unknownはunknown以外へ変更しない]
    #   引数: [snapshot_id/action_id: 元判断と実操作, outcome: 実操作後の結果,
    #     action_generation: 期待する実行世代。Noneは明示receiptに保存済みの世代を使う]
    #   戻り値: [bool: 条件を満たす実行traceを更新した場合True]
    # }
    def record_action_outcome(
        self,
        snapshot_id: str,
        action_id: str,
        outcome: dict[str, Any],
        *,
        action_generation: int | None = None,
    ) -> bool:
        entries = self._read()
        for entry in reversed(entries):
            receipt = _execution_receipt_for_entry(entry)
            if receipt is None:
                continue
            status = receipt.execution_status
            if (
                entry.get("snapshot_id") == snapshot_id
                and entry.get("action_id") == action_id
                and status in {"executed", "dispatch_unknown"}
                and (
                    action_generation is None
                    or receipt.action_generation == action_generation
                )
                and (status == "dispatch_unknown" or receipt.eligible_for_learning)
                and (status != "dispatch_unknown" or outcome.get("status") == "unknown")
            ):
                self._update_entry(entries, entry, {"action_outcome": outcome})
                return True
        return False

    # {
    #   責務: [record_execution_receipt: planned traceへ同じsnapshot・操作・世代の実行事実を関連付ける]
    #   処理: [画面参照と世代を照合し、別操作や矛盾するreceiptで既存事実を上書きしない]
    #   引数: [receipt: Executorの送信証拠に由来する不変の受領記録]
    #   戻り値: [True: 新規記録, None: 同内容で記録済み, False: 対象なし/不一致]
    # }
    def record_execution_receipt(self, receipt: ExecutionReceipt) -> bool | None:
        entries = self._read()
        for entry in reversed(entries):
            if (
                not isinstance(entry.get("decision"), dict)
                or entry.get("snapshot_id") != receipt.snapshot_id
                or entry.get("action_id") != receipt.action_id
            ):
                continue
            generation = entry.get("action_generation")
            if generation is not None and (
                type(generation) is not int or generation != receipt.action_generation
            ):
                return False
            if (
                entry.get("screen_id") != receipt.screen_id
                or str(entry.get("state_signature", "")) != receipt.state_signature
            ):
                return False
            existing = entry.get("execution_receipt")
            if isinstance(existing, dict):
                return None if existing == receipt.to_dict() else False
            self._update_entry(
                entries,
                entry,
                {
                    "execution_receipt": receipt.to_dict(),
                    "execution_status": receipt.execution_status,
                    "action_generation": receipt.action_generation,
                },
            )
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
    #   責務: [recent_actions: 完全な実行receiptで送信を確認し、学習対象となる直近実操作だけを返す]
    #   処理: [planned/legacy/blocked/dry-run/不明送信を除外し、指定時は世代も照合してから件数を制限する]
    #   引数: [limit: 最大件数。0は空, action_generation: 任意の対象実行世代]
    #   戻り値: [list: 確認済み実操作traceを古い順に並べた一覧]
    #   エラー: [ValueError: limitが負]
    # }
    def recent_actions(
        self, limit: int = 5, *, action_generation: int | None = None
    ) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("history limit must not be negative")
        if limit == 0:
            return []
        action_entries = [
            entry
            for entry in self._read()
            if _is_action_history_entry(entry)
            and (
                action_generation is None
                or entry.get("action_generation") == action_generation
            )
        ]
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
        if self.event_adapter is not None:
            records = self.event_adapter.records
        elif not self.path.exists():
            return []
        else:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise ValueError("decision trace must contain an array")
            records = [entry for entry in value if isinstance(entry, dict)]
        projected: list[dict[str, Any]] = []
        indexed_entries: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            patch = record.get("trace_update")
            if isinstance(patch, dict):
                key = (
                    str(record.get("snapshot_id", "")),
                    str(record.get("action_id", "")),
                )
                target = indexed_entries.get(key)
                if target is not None:
                    target.update(patch)
                continue
            projected.append(record)
            key = (str(record.get("snapshot_id", "")), str(record.get("action_id", "")))
            indexed_entries[key] = record
        return projected

    # {
    #   責務: [_write: trace全体を一時JSONへ書いてから保存先へ原子的に置き換える]
    #   処理: [同じ保存先を対象にstage_json_writeを使い、公開後に一時ファイルを後始末する]
    #   引数: [entries: 保存順に並んだtrace record全件]
    #   戻り値: [なし。JSON置換が成功した後に戻る]
    #   エラー: [OSError: 一時JSONの作成または保存先への置換に失敗した場合]
    # }
    def _write(self, entries: list[dict[str, Any]]) -> None:
        staged_write = stage_json_write(self.path, entries)
        try:
            staged_write.publish()
        finally:
            staged_write.discard()


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


# {
#   責務: [_history_summary: 確認済み実操作traceを判断用の短い履歴へ投影する]
#   処理: [当該操作自身のOutcomeだけを使用し、未観測時はunknownとする]
#   引数: [entry: receipt検証済みのtrace record]
#   戻り値: [dict: 実行参照・世代・結果・状態変化の要約]
# }
def _history_summary(entry: dict[str, Any]) -> dict[str, Any]:
    action_outcome = entry.get("action_outcome")
    if isinstance(action_outcome, dict):
        outcome_status = str(action_outcome.get("status", "unknown"))
        state_changed = bool(
            action_outcome.get("state_changed", outcome_status == "changed")
        )
    else:
        outcome_status = "unknown"
        state_changed = False
    return {
        "snapshot_id": str(entry.get("snapshot_id", "")),
        "screen_id": str(entry.get("screen_id", "")),
        "state_signature": str(entry.get("state_signature", "")),
        "action_id": str(entry.get("action_id", "")),
        "execution_status": entry.get("execution_status", "legacy_unknown"),
        "action_generation": entry.get("action_generation"),
        "outcome": outcome_status,
        "state_changed": state_changed,
    }


# {
#   責務: [_is_action_history_entry: 保存済み判断が同一参照の実送信事実を持つか確認する]
#   処理: [完全なreceiptと学習可否を照合し、拒否・計画だけ・旧形式不明を実操作扱いにしない]
#   引数: [entry: 判断trace record]
#   戻り値: [bool: 確認済み実操作ならTrue]
# }
def _is_action_history_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry.get("decision"), dict):
        return False
    receipt = _execution_receipt_for_entry(entry)
    if (
        receipt is None
        or receipt.execution_status != "executed"
        or not receipt.eligible_for_learning
    ):
        return False
    reliability = entry.get("reliability")
    if not isinstance(reliability, dict):
        return True
    return str(reliability.get("status", "")).upper() != "REJECT"


# {
#   責務: [_execution_receipt_for_entry: 保存された実行受領記録の型と元traceへのbindを検証する]
#   処理: [完全なreceipt契約を復元し、snapshot・操作・世代・画面参照が一致する場合だけ採用する]
#   引数: [entry: 判断trace record]
#   戻り値: [ExecutionReceiptまたはNone: legacy/破損/不一致は送信事実として扱わない]
# }
def _execution_receipt_for_entry(entry: dict[str, Any]) -> ExecutionReceipt | None:
    raw = entry.get("execution_receipt")
    if not isinstance(raw, dict):
        return None
    try:
        receipt = ExecutionReceipt(**raw)
    except (TypeError, ValueError):
        return None
    if receipt.snapshot_id != entry.get(
        "snapshot_id"
    ) or receipt.action_id != entry.get("action_id"):
        return None
    if type(
        entry.get("action_generation")
    ) is not int or receipt.action_generation != entry.get("action_generation"):
        return None
    if receipt.screen_id != entry.get("screen_id") or receipt.state_signature != str(
        entry.get("state_signature", "")
    ):
        return None
    return receipt


def _state_changed_from_history(
    observation: ScreenObservation, previous: dict[str, Any] | None
) -> bool:
    if previous is None:
        return False
    current_signature = str(observation.features.get("signature", ""))
    previous_signature = str(previous.get("state_signature", ""))
    if current_signature and previous_signature:
        return current_signature != previous_signature
    current_hash = str(observation.features.get("perceptual_hash", ""))
    previous_context = previous.get("context", {})
    previous_state = (
        previous_context.get("state", {}) if isinstance(previous_context, dict) else {}
    )
    previous_hash = (
        str(previous_state.get("perceptual_hash", ""))
        if isinstance(previous_state, dict)
        else ""
    )
    if current_hash and previous_hash:
        return current_hash != previous_hash
    return str(previous.get("screen_id", "")) != observation.screen_id


def _confidence(value: object) -> float:
    result = float(value)
    if not isfinite(result):
        return 0.0
    return max(0.0, min(1.0, result))
