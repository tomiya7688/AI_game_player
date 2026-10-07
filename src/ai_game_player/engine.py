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
#     GamePlayerEngine: 画面と操作候補から選択判断を作り、検査結果と判断を操作履歴へ結び付ける
#   ]
#   フィールド: [
#     decision_verifier: 選択候補が現在の画面と許可一覧に合うかを実行規則で調べる検証器
#     last_reliability_result: 最後の判断に対する検査状態と根拠。拒否時は停止理由の記録にも使う
#   ]
#   処理: [
#     1: 前回の操作結果と現在画面を基に、候補選択に必要な判断材料を組み立てる
#     2: 判断元の回答を画面・候補・参照IDと照合し、不整合があれば拒否記録を残す
#     3: 有効な選択だけを操作履歴と判断根拠へ保存し、次回の結果判定に備える
#   ]
# }
class GamePlayerEngine:
    # {
    #   責務: [
    #     __init__: ゲーム固有の保存先と差し替え可能な判断部品を接続し、初回用の状態を用意する
    #   ]
    #   処理: [
    #     1: 操作評価器、判断元、ゲームごとの行動・知識・監査記録の保存先を用意する
    #     2: 画面と過去の結果から判断材料を作る処理、判断検証器、結果検出器を接続する
    #     3: 前回画面・前回操作・未完了の履歴参照を、操作前の初期状態にする
    #   ]
    #   引数: [
    #     game_directory: 行動履歴、知識、判断記録をこのゲーム用に保存するディレクトリ
    #     provider: 操作候補を選ぶ判断元。省略時は外部通信を行わないルール方式を使う
    #     context_builder: 判断材料の作成処理を差し替える場合に渡す。省略時はゲームの知識保存先と共に生成する
    #     outcome_detector: 操作前後の画面を比較する結果検出器。省略時は既定の検出器を使う
    #   ]
    #   戻り値: [なし。各部品をインスタンスへ接続し、以後のstep呼び出しで使えるようにする]
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
        self._last_decision_trace_reference: tuple[str, str] | None = None
        self._previous_action_trace_reference: tuple[str, str] | None = None
        self._previous_action_state_before_last_decision: tuple[
            ScreenObservation | None,
            str | None,
            tuple[str, str] | None,
        ] | None = None

    # {
    #   責務: [
    #     step: 現在画面の操作候補を選び、信頼性検査でREJECTされなかった判断と根拠を記録する
    #   ]
    #   処理: [
    #     1: 候補を評価し、現在画面・過去の実行結果・ゲーム目標を含む判断材料を作る
    #     2: 判断元の回答を画面参照と許可候補へ照合し、明確な不整合なら拒否記録を保存する
    #     3: 拒否されなかった回答をActionDecisionへ正規化し、行動履歴と全検査根拠を保存する
    #     4: 今回の判断を次の画面比較へ結び付け、実行前停止に備えた状態も保持する
    #   ]
    #   引数: [
    #     observation: 候補と照合する、今回の判断時点で取得した画面と解析情報
    #     candidates: 画面解析器が検出・統合した操作候補。評価器が実行可否を絞り込む
    #     purpose: 判断元と危険度検査に渡す、利用者が指定したゲーム内の達成目的
    #     personality: 判断元が対応している場合だけ渡す応答方針。候補の許可条件を変更するものではない
    #   ]
    #   戻り値: [
    #     ActionDecision: 選択された許可候補、選択理由、画面参照を含む実行前判断
    #   ]
    #   エラー: [
    #     ValueError: 回答が現在の候補・画面と明確に矛盾するか、検査後に有効な判断へ変換できない場合
    #   ]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        previous_action_state = (
            self._previous_observation,
            self._previous_action_id,
            self._previous_action_trace_reference,
        )
        self._previous_action_state_before_last_decision = None
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
        trace_reference = (context.snapshot_id, decision.action_id)
        self._previous_action_state_before_last_decision = previous_action_state
        self._previous_observation = observation
        self._previous_action_id = decision.action_id
        self._last_decision_trace_reference = trace_reference
        self._previous_action_trace_reference = trace_reference
        return decision

    # {
    #   責務: [
    #     mark_last_decision_not_executed: 安全確認などで実行しなかった直近判断を履歴から除き、以前の操作状態へ戻す
    #   ]
    #   処理: [
    #     1: 直近判断の画面IDと操作IDに停止理由を保存し、行動履歴から除外する
    #     2: 今回の判断を作る前に退避した、直前に実行済みの操作状態を復元する
    #     3: 次の画面観測を直前の実行済み操作の結果として比較できるよう参照を戻す
    #   ]
    #   引数: [
    #     reason: 操作を送らなかった理由。安全判定や実行条件の監査履歴へ保存する
    #   ]
    #   戻り値: [なし。直近の判断を停止済みとして記録し、以前の操作状態へ戻す]
    #   エラー: [
    #     RuntimeError: 直近判断または退避状態がなく、対応する記録を更新できない場合
    #   ]
    # }
    def mark_last_decision_not_executed(self, reason: str) -> None:
        trace_reference = self._last_decision_trace_reference
        if trace_reference is None:
            raise RuntimeError("実行前停止を記録する判断traceがありません")
        previous_action_state = self._previous_action_state_before_last_decision
        if previous_action_state is None:
            raise RuntimeError("実行前停止の前に保持した操作状態がありません")
        snapshot_id, action_id = trace_reference
        if not self.trace.mark_action_not_executed(snapshot_id, action_id, reason):
            raise RuntimeError("実行前停止を記録する判断traceを更新できません")
        self._last_decision_trace_reference = None
        self._previous_action_state_before_last_decision = None
        (
            self._previous_observation,
            self._previous_action_id,
            self._previous_action_trace_reference,
        ) = previous_action_state

    # {
    #   責務: [
    #     _bind_legacy_context_references: 旧形式のchoose呼び出しが画面参照を返さない場合に限り、現在の判断参照を補う
    #   ]
    #   処理: [
    #     1: ActionDecision以外の値は形を変えずに返す
    #     2: 画面観測ID・画面ID・状態照合値のうち未設定の項目だけ、現在の判断材料から埋める
    #     3: 既に明示された参照、判断元、候補、理由は書き換えない
    #   ]
    #   引数: [
    #     decision_output: 従来のchoose APIが返した値。形式検証前なので任意の型を取り得る
    #     context: 同じ呼び出しで判断元へ渡した画面IDと状態照合値を持つ判断材料
    #   ]
    #   戻り値: [
    #     object: 不足参照だけを補ったActionDecision、または入力と同じ値
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
        if self._previous_action_trace_reference is not None:
            outcome = event.to_dict()
            self.trace.record_action_outcome(*self._previous_action_trace_reference, outcome)
        return event.to_assessment()
