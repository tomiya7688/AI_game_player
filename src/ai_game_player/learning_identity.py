"""明示profileの識別契約だけを、有界hashの学習keyへ変換する。"""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from collections import Counter

from ai_game_player.decision_context import DecisionContext


# {
#   責務: [LearningIdentityKind: 未実装の意味追跡を仮定せず、profileが保証する再対応だけを区別する]
#   フィールド: [DISABLED: 未知の提供元, STABLE_ID: profile保証ID, UNIQUE_LABEL: profile保証の一意表示]
# }
class LearningIdentityKind(str, Enum):
    DISABLED = "disabled"
    STABLE_ID = "profile_stable_id_v1"
    UNIQUE_LABEL = "profile_unique_label_hash_v1"


# {
#   責務: [LearningIdentityPolicy: profileの再対応宣言を保持し、衝突時は更新も適用も保留する]
#   フィールド: [kind: 版付き識別方針, profile_id: Scopeと一致すべき信頼済み設定ID]
# }
@dataclass(frozen=True, slots=True)
class LearningIdentityPolicy:
    kind: LearningIdentityKind = LearningIdentityKind.DISABLED
    profile_id: str = "unavailable"

    # {
    #   責務: [__post_init__: 任意文字列で識別保証を有効化させない]
    #   処理: [enumとopaque profile IDを検証する] 引数: [self: 方針] 戻り値: [なし]
    #   エラー: [ValueError: 不正方針]
    # }
    def __post_init__(self) -> None:
        from ai_game_player.knowledge_scope import validate_identifier

        if not isinstance(self.kind, LearningIdentityKind):
            raise ValueError("invalid learning identity kind")
        validate_identifier(self.profile_id, "learning identity profile")

    # {
    #   責務: [keys: このframeで一意に再対応できる候補だけをopaque keyへ対応させる]
    #   処理: [上限256候補、512文字の入力をhash化。同frameの重複表示・重複IDは全て保留する]
    #   引数: [context: 現在の判断材料] 戻り値: [dict: action IDからraw labelを含まないkey]
    # }
    def keys(self, context: DecisionContext) -> dict[str, str]:
        if self.kind == LearningIdentityKind.DISABLED or len(context.candidates) > 256:
            return {}
        ids = Counter(c.action_id for c in context.candidates)
        identities: list[tuple[str, str]] = []
        for candidate in context.candidates:
            if ids[candidate.action_id] != 1:
                continue
            value = (
                candidate.action_id
                if self.kind == LearningIdentityKind.STABLE_ID
                else " ".join(candidate.label.casefold().split())
            )
            if not value or len(value) > 512 or len(candidate.kind) > 128:
                continue
            digest = sha256(
                (self.kind.value + "\0" + candidate.kind + "\0" + value).encode()
            ).hexdigest()
            identities.append((candidate.action_id, "action." + digest))
        counts = Counter(key for _, key in identities)
        return {action: key for action, key in identities if counts[key] == 1}
