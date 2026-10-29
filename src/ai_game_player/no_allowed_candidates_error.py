"""意思決定前に許可された操作候補が空だったことを識別します。"""


# {
#   責務: [NoAllowedCandidatesError: 許可候補が空で、入力実行前の意思決定を中止した状態を表すValueErrorの派生型]
#   保持状態: [args: ValueErrorから継承する例外の説明文。候補の形式不正や安全拒否とは区別する]
# }
class NoAllowedCandidatesError(ValueError):
    """許可候補なしを、他の判断・入力失敗と区別するための例外です。"""
