"""Planningと将来のUnknownness policyで共有する、入力権限を持たない判断種別。"""

from enum import Enum


# {
#   責務: [PlanningOutcome: 操作選択、再観測、保留、追加判断要求を共通の語彙で表す]
#   フィールド: [SELECT: 候補IDの提案, OBSERVE_MORE: 有界な新観測要求,
#     ABSTAIN: 入力を提案しない, ESCALATE: 別判断段階の要求。Remote送信の許可ではない]
# }
class PlanningOutcome(str, Enum):
    SELECT = "select"
    OBSERVE_MORE = "observe_more"
    ABSTAIN = "abstain"
    ESCALATE = "escalate"
