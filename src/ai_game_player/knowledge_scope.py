"""Knowledgeをゲーム・場面・利用設定・Player系統ごとに隔離する識別子。"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
SCOPE_FIELDS = frozenset(
    {"game_id", "scene_id", "profile_id", "player_id", "lineage_id"}
)


# {
#   責務: [validate_identifier: 自由文やパスを含まない有界の不透明識別子だけを受理する]
#   処理: [許可文字と長さを検査し、既知のcredential形式を拒否する]
#   引数: [value: game/action/source等の識別子, name: エラーに使う固定フィールド名]
#   戻り値: [str: 検証済み識別子]
#   エラー: [ValueError: 識別子が不正。入力値自体はエラーへ含めない]
# }
def validate_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded opaque identifier")
    if value.startswith(("sk-", "ghp_", "github_pat_")):
        raise ValueError(f"{name} must not contain a credential")
    return value


# {
#   責務: [KnowledgeScope: 同じPlayerでもゲーム・場面・設定・系統が異なる知識を分離する]
#   フィールド: [game_id: ゲーム識別子, scene_id: 場面識別子, profile_id: 設定識別子,
#     player_id: Player instance識別子, lineage_id: 明示共有を行わない系統識別子]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    game_id: str
    scene_id: str
    profile_id: str
    player_id: str
    lineage_id: str

    # {
    #   責務: [__post_init__: scopeの全要素を有界の識別子に制限する]
    #   処理: [各要素を検証し、欠落・自由文・credentialを拒否する]
    #   引数: [self: 構築中のscope]
    #   戻り値: [なし]
    #   エラー: [ValueError: 一つでも不正な識別子がある]
    # }
    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            validate_identifier(value, name)

    # {
    #   責務: [from_dict: 宣言された5要素だけから隔離scopeを作る]
    #   処理: [不足と余分なフィールドを拒否して型へ変換する]
    #   引数: [value: 読取ったscope辞書]
    #   戻り値: [KnowledgeScope: 全要素を固定した値]
    #   エラー: [ValueError: 辞書以外、フィールド不一致、識別子不正]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "KnowledgeScope":
        if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
            raise ValueError("knowledge scope fields do not match contract")
        return cls(**value)

    # {
    #   責務: [to_dict: scopeを独立したJSON辞書へ変換する]
    #   処理: [保持する5識別子をコピーする]
    #   引数: [self: 変換元scope]
    #   戻り値: [dict: 呼出側が変更できる独立コピー]
    # }
    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    # {
    #   責務: [key: scopeの全要素を含む固定長ストレージキーを返す]
    #   処理: [正規化JSONのSHA-256を計算し識別子をファイルパスへ直接使わない]
    #   引数: [self: キー生成元scope]
    #   戻り値: [str: 64文字のhex digest]
    # }
    @property
    def key(self) -> str:
        return sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
