"""任意ゲームのpure model/qualityと有限探索予算を結ぶ契約。"""

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Generic, Protocol, TypeVar

from ai_game_player.belief_validation import belief_id, belief_integer, belief_number
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.transition_model import TransitionModel


StateT = TypeVar("StateT")
StateInT = TypeVar("StateInT", contravariant=True)
_PYTHON_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


# {
#   責務: [StateQualityEvaluator: 予測stateのnormalized qualityを純粋に評価する]
#   フィールド: [なし: 6軸のゲーム固有投影は注入側が所有し、実Outcomeや入力へ接続しない]
# }
class StateQualityEvaluator(Protocol[StateInT]):
    # {
    #   責務: [evaluate: 一つの仮想stateへ数値品質と支持不足の区間を返す]
    #   処理: [画像/DOM/実入力/外部Modelへ接続せず、渡されたstateだけで評価する]
    #   引数: [state: 不変の仮想状態] 戻り値: [NumericQuality: 校正world確率とは独立した数値品質]
    # }
    def evaluate(self, state: StateInT) -> NumericQuality: ...


# {
#   責務: [PlanningDomain: trusted登録factoryが作るゲーム非依存の純粋な計画Adapter]
#   フィールド: [state_type: 不変state型, decode_state: canonical typed JSONを当該型へ戻す関数,
#     model: 確定/chance遷移, legal_actions: 各stateの合法な抽象IDを有限tupleで返す関数,
#     evaluator: 仮想stateの数値品質。すべて制限worker内だけで呼ぶ]
# }
@dataclass(frozen=True, slots=True)
class PlanningDomain(Generic[StateT]):
    state_type: type[StateT]
    decode_state: Callable[[bytes], StateT]
    model: TransitionModel[StateT]
    legal_actions: Callable[[StateT], tuple[str, ...]]
    evaluator: StateQualityEvaluator[StateT]

    # {
    #   責務: [__post_init__: 純粋なmodel/codec/合法性/qualityの実在callableを照合する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.state_type, type)
            or not callable(self.decode_state)
            or not callable(self.legal_actions)
            or not callable(getattr(self.model, "apply_action", None))
            or not callable(getattr(self.model, "chance_outcomes", None))
            or not callable(getattr(self.evaluator, "evaluate", None))
        ):
            raise ValueError(
                "planning domain requires pure typed model, codec, legality and quality"
            )


# {
#   責務: [PlanningRegistration: trustedコードが認可したfactoryと数値の版を固定する]
#   フィールド: [registration_id/module_name/factory_name: 画面やModel応答から作らない登録先,
#     game_id/state_schema_id: 対象state契約, model/evaluator id/version: 出所,
#     quality_projection_version/profile_version/value_unit: #491の共有数値品質の意味]
# }
@dataclass(frozen=True, slots=True)
class PlanningRegistration:
    registration_id: str
    module_name: str
    factory_name: str
    game_id: str
    state_schema_id: str
    model_id: str
    model_version: str
    evaluator_id: str
    evaluator_version: str
    quality_projection_version: int = 1
    quality_profile_version: str = "default.1"
    value_unit: str = "normalized_quality.v1"

    # {
    #   責務: [__post_init__: 出所IDとtrusted factoryのPython名を検証する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        for value in (
            self.registration_id,
            self.game_id,
            self.state_schema_id,
            self.model_id,
            self.model_version,
            self.evaluator_id,
            self.evaluator_version,
            self.quality_profile_version,
            self.value_unit,
        ):
            belief_id(value)
        belief_integer(self.quality_projection_version, 1, 1)
        for name in (self.module_name, self.factory_name):
            if (
                not isinstance(name, str)
                or len(name) > 256
                or not _PYTHON_NAME.fullmatch(name)
            ):
                raise ValueError(
                    "planning factory must be a trusted bounded Python name"
                )


# {
#   責務: [SearchBudget: 深さ/node/trace/cache/IPC/worker資源を有限に制限する]
#   フィールド: [max_depth/max_nodes: action深さと全反復合計node訪問,
#     max_actions/max_chance_outcomes: callbackのtuple上限, max_state_bytes: canonical state上限,
#     max_cache_entries/max_cache_bytes: cacheの論理予算, max_trace_values: 分布の最大値数,
#     max_request_bytes/max_result_bytes: JSON file IPC上限, wall_time_ms: 親の受領deadline,
#     worker_memory_bytes: OS worker address-space/private-commit上限,
#     max_unknown_mass: default選択で許容するBelief未知質量,
#     allow_conditional_selection: 未校正仮説条件付きの明示実験profileだけで選択を許可]
# }
@dataclass(frozen=True, slots=True)
class SearchBudget:
    max_depth: int = 3
    max_nodes: int = 10000
    max_actions: int = 16
    max_chance_outcomes: int = 256
    max_state_bytes: int = 16384
    max_cache_entries: int = 256
    max_cache_bytes: int = 4 * 1024 * 1024
    max_trace_values: int = 32
    max_request_bytes: int = 1024 * 1024
    max_result_bytes: int = 512 * 1024
    wall_time_ms: int = 250
    worker_memory_bytes: int = 256 * 1024 * 1024
    max_unknown_mass: float = 0.25
    allow_conditional_selection: bool = False

    # {
    #   責務: [__post_init__: 深さ・node・byte・OS予算を有限区間へ限定する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        limits = (
            (self.max_depth, 1, 16),
            (self.max_nodes, 1, 1_000_000),
            (self.max_actions, 1, 32),
            (self.max_chance_outcomes, 1, 4096),
            (self.max_state_bytes, 1, 65536),
            (self.max_cache_entries, 0, 4096),
            (self.max_cache_bytes, 0, 64 * 1024 * 1024),
            (self.max_trace_values, 1, 256),
            (self.max_request_bytes, 4096, 16 * 1024 * 1024),
            (self.max_result_bytes, 4096, 16 * 1024 * 1024),
            (self.wall_time_ms, 10, 60000),
            (self.worker_memory_bytes, 64 * 1024 * 1024, 2 * 1024**3),
        )
        for value, minimum, maximum in limits:
            belief_integer(value, minimum, maximum)
        belief_number(self.max_unknown_mass, 1.0)
        if type(self.allow_conditional_selection) is not bool:
            raise ValueError("conditional planning selection must be explicit boolean")
