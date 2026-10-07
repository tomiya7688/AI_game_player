from dataclasses import replace
from pathlib import Path

from ai_game_player.decision_context import DecisionContext, DecisionContextBuilder, DecisionTraceStore
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityResult, ReliabilityStatus
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_fusion import OutcomeDetector
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.provider import RuleProvider


# {
#   責務: [
#     GamePlayerEngine: 観測・候補評価・Decision検証・履歴保存を順に実行する
#   ]
#   フィールド: [
#     decision_verifier: Provider出力をcontext evidenceと照合する検証器
#     last_reliability_result: 直近判断の信頼性判定
#   ]
#   処理: [
#     1: 観測状態と許可候補からDecision Contextを構築する
#     2: Providerの判断を決定論的に検証する
#     3: 判断・検証Evidence・履歴を保存する
#   ]
# }
class GamePlayerEngine:
    # {
    #   責務: [
    #     __init__: Decision処理と履歴を保持する依存関係を初期化する
    #   ]
    #   処理: [
    #     1: 候補評価・Provider・履歴Storeを初期化する
    #     2: DecisionContextBuilderとReliabilityVerifierを接続する
    #     3: 前回観測とOutcome状態を初期化する
    #   ]
    #   引数: [
    #     game_directory: ゲーム固有データを保存するdirectory
    #     provider: 判断を行う任意Provider
    #     context_builder: 任意のDecision Context構築器
    #     outcome_detector: 任意のOutcome検出器
    #   ]
    #   戻り値: []
    # }
    def __init__(
        self,
        game_directory: Path,
        provider=None,
        context_builder: DecisionContextBuilder | None = None,
        outcome_detector: OutcomeDetector | None = None,
    ) -> None:
        self.evaluator = ActionEvaluator()
        self.provider = provider or RuleProvider()
        self.history = HistoryStore(game_directory / "history.json")
        self.trace = DecisionTraceStore(game_directory / "decision_trace.json")
        self.context_builder = context_builder or DecisionContextBuilder(KnowledgeStore(game_directory / "knowledge.json"))
        self.outcome_detector = outcome_detector or OutcomeDetector(self._semantic_outcome_provider())
        self.decision_verifier = DecisionVerifier()
        self.last_reliability_result: ReliabilityResult | None = None
        self.last_outcome_event: OutcomeEvent | None = None
        self._previous_observation: ScreenObservation | None = None
        self._previous_action_id: str | None = None

    # {
    #   責務: [
    #     step: 観測snapshotから判断を取得し検証後に履歴へ保存する
    #   ]
    #   処理: [
    #     1: 安全に許可された候補とDecision Contextを構築する
    #     2: Provider出力をReliability Verifierへ渡す
    #     3: REJECT時は拒否Evidenceを記録して停止する
    #     4: 有効な判断とReliability Evidenceを履歴へ保存する
    #   ]
    #   引数: [
    #     observation: 判断対象の現在画面
    #     candidates: 画面から統合した操作候補
    #     purpose: 現在のゲーム目的
    #     personality: Provider向けの任意の振る舞い指定
    #   ]
    #   戻り値: [
    #     ActionDecision: 検証済み候補判断
    #   ]
    #   エラー: [
    #     ValueError: Provider判断がREJECTされた
    #   ]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        allowed = self.evaluator.evaluate(observation, candidates)
        previous_outcome = self._assess_previous_outcome(observation)
        context = self.context_builder.build(
            observation,
            candidates,
            allowed,
            recent_history=self.trace.recent_actions(5),
            previous_outcome=previous_outcome,
            current_goal=purpose,
        )
        if self._uses_context_api():
            raw_decision = self.provider.choose_context(context, personality)
        else:
            raw_decision = self.provider.choose(allowed, observation, purpose, personality)
            raw_decision = self._bind_legacy_context_references(raw_decision, context)
        reliability = self.decision_verifier.verify(
            raw_decision,
            context,
            observation,
            evaluator_allowed_action_ids=[candidate.action_id for candidate in allowed],
        )
        self.last_reliability_result = reliability
        if reliability.status == ReliabilityStatus.REJECT:
            self.trace.append_rejection(context, reliability.to_dict())
            failed_checks = ", ".join(
                entry.check for entry in reliability.evidence if entry.severity == "reject"
            )
            raise ValueError(f"Decision rejected by reliability verifier: {failed_checks}")
        decision = self.decision_verifier.normalize_decision(raw_decision)
        if decision is None:
            self.trace.append_rejection(context, reliability.to_dict())
            raise ValueError("Decision output could not be normalized after verification")
        self.history.append(observation, decision)
        self.trace.append(context, decision, reliability=reliability.to_dict())
        self._previous_observation = observation
        self._previous_action_id = decision.action_id
        return decision

    # {
    #   責務: [
    #     _bind_legacy_context_references: 旧choose APIの判断へ同期Context参照を補う
    #   ]
    #   処理: [
    #     1: ActionDecisionの欠落参照だけを特定する
    #     2: 現在のDecision Contextから不足参照を補完する
    #     3: 既存の明示参照・Provider・判断内容を保持する
    #   ]
    #   引数: [
    #     decision_output: 旧choose APIから返された未検証判断
    #     context: 同期呼び出しに使ったDecision Context
    #   ]
    #   戻り値: [
    #     object: 参照補完済みActionDecision、または変更しないProvider出力
    #   ]
    # }
    @staticmethod
    def _bind_legacy_context_references(decision_output: object, context: DecisionContext) -> object:
        if not isinstance(decision_output, ActionDecision):
            return decision_output
        missing_references = {
            "snapshot_id": context.snapshot_id,
            "screen_id": str(context.state.get("screen_id", "")),
            "state_signature": str(context.state.get("signature", "")),
        }
        values_to_bind = {
            field_name: reference
            for field_name, reference in missing_references.items()
            if getattr(decision_output, field_name) is None
        }
        return replace(decision_output, **values_to_bind) if values_to_bind else decision_output

    def _uses_context_api(self) -> bool:
        if not hasattr(self.provider, "choose_context"):
            return False
        provider_type = type(self.provider)
        if isinstance(self.provider, RuleProvider) and provider_type is not RuleProvider:
            legacy_choose = getattr(provider_type, "choose", None)
            inherited_context = getattr(provider_type, "choose_context", None) is RuleProvider.choose_context
            if legacy_choose is not RuleProvider.choose and inherited_context:
                return False
        return True

    def _semantic_outcome_provider(self):
        if not hasattr(self.provider, "assess_outcome"):
            return None
        if not isinstance(self.provider, RuleProvider):
            return self.provider
        provider_type = type(self.provider)
        custom_assess = getattr(provider_type, "assess_outcome", None)
        return self.provider if custom_assess is not RuleProvider.assess_outcome else None

    def _assess_previous_outcome(self, observation: ScreenObservation) -> OutcomeAssessment:
        if self._previous_observation is None or self._previous_action_id is None:
            self.last_outcome_event = None
            return OutcomeAssessment("unknown", 0.0, "no previous action")
        event = self.outcome_detector.detect(
            self._previous_observation,
            self._previous_action_id,
            observation,
        )
        self.last_outcome_event = event
        return event.to_assessment()
