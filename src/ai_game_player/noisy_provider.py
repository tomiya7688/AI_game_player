import random

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


# 合成評価で意味のある推論をしないproviderが返す、意図的に低いOutcome確信度。
NOISY_OUTCOME_CONFIDENCE = 0.1


# {
#   責務: [NoisyLanguageProvider: 固定seedで許可候補を擬似ランダム選択し、意味を持たない理由と低確信Outcomeを返す]
#   保持状態: [selection_random: 候補選択を再現する専用乱数器, reason_random: 選択順に影響しない説明生成用乱数器]
#   処理: [DecisionContextの許可IDと候補ごとの許可状態を二重に照合してから選択し、応答の画面参照を元のsnapshotへ結び付ける]
# }
class NoisyLanguageProvider:
    _REASON_WORDS = (
        "amber", "velvet", "orbit", "quiet", "comet", "lattice", "silver", "meadow",
        "curious", "cobalt", "drifting", "marble", "echo", "soft", "harbor", "distant",
    )

    # {
    #   責務: [__init__: 候補選択と無意味な理由生成で独立して再現可能な乱数系列を初期化する]
    #   引数: [seed: 同じ整数seedを渡したprovider間で同じ応答列を作る乱数系列の識別値]
    #   戻り値: [なし。以後の選択と理由生成に使う乱数器を保持する]
    #   エラー: [ValueError: seedが整数でない場合。boolは整数として受け付けない]
    # }
    def __init__(self, seed: int) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        self._selection_random = random.Random(seed)
        self._reason_random = random.Random(seed ^ 0x5EED178)

    # {
    #   責務: [choose_context: 判断材料の許可候補から1件を再現可能に選び、画面参照付き回答を作る]
    #   処理: [allowed_action_idsと候補allowedの両方が許可した候補だけを抽出し、seed付き乱数器で選ぶ]
    #   引数: [context: 現在画面・許可候補・候補評価をまとめた判断snapshot, personality: 互換性のため受け取る応答方針。この合成providerでは参照しない]
    #   戻り値: [ActionDecision: 許可候補ID、無意味な選択理由、provider名、同じsnapshotの画面参照]
    #   エラー: [ValueError: 二重照合後に選択可能な候補がない場合]
    # }
    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        del personality
        allowed_ids = set(context.allowed_action_ids)
        allowed_candidates = [
            candidate
            for candidate in context.candidates
            if candidate.allowed and candidate.action_id in allowed_ids
        ]
        if not allowed_candidates:
            raise ValueError("NoisyLanguageProvider requires at least one allowed candidate")

        candidate = self._selection_random.choice(allowed_candidates)
        return ActionDecision(
            candidate.action_id,
            self._word_salad_reason(),
            "noisy_language_fixture",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )

    # {
    #   責務: [assess_outcome: 画面状態から意味を推論せず、unknownと低い確信度を返す]
    #   引数: [observation: 評価対象の現在画面。このfixtureは画面内容を解釈しない, previous: 比較対象の前画面。このfixtureは前画面も解釈しない]
    #   戻り値: [OutcomeAssessment: semantic fallbackの挙動検証に使うunknown・0.1確信度・無意味な理由]
    # }
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None = None,
    ) -> OutcomeAssessment:
        del observation, previous
        return OutcomeAssessment("unknown", NOISY_OUTCOME_CONFIDENCE, self._word_salad_reason())

    # {
    #   責務: [_word_salad_reason: 候補名や画面内容を参照しない短い無意味文を生成する]
    #   引数: []
    #   戻り値: [str: provider応答の形式確認に使う、seedで再現可能な英単語列]
    # }
    def _word_salad_reason(self) -> str:
        words = self._reason_random.sample(self._REASON_WORDS, 5)
        return " ".join(words)
