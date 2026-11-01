"""推論・学習のModeとPhaseから許可operationを導く共通契約。"""

from dataclasses import dataclass
from enum import Enum
import re


RUNTIME_MODE_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")


# {
#   責務: [RuntimeMode: 推論と学習の実行方針を互いに排他的な5種類で表す]
#   フィールド: [値: 永続化とJournalで使う版1の識別子]
# }
class RuntimeMode(str, Enum):
    INFERENCE_ONLY = "INFERENCE_ONLY"
    LEARNING_ONLY = "LEARNING_ONLY"
    INFERENCE_WITH_LEARNING = "INFERENCE_WITH_LEARNING"
    ALTERNATING_INFER_LEARN = "ALTERNATING_INFER_LEARN"
    INFERENCE_WITH_LIGHT_ADAPTATION = "INFERENCE_WITH_LIGHT_ADAPTATION"


# {
#   責務: [RuntimePhase: Modeと独立した現在の処理段階を8種類で表す]
#   フィールド: [値: 学習・評価・昇格の再開条件を識別する段階名]
# }
class RuntimePhase(str, Enum):
    IDLE = "IDLE"
    INFER = "INFER"
    LEARN = "LEARN"
    EVALUATE = "EVALUATE"
    PROMOTE = "PROMOTE"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


# {
#   責務: [RuntimeOperation: Subsystemが実行許可を照合する操作種別を固定する]
#   フィールド: [値: Mode/Phaseの能力表と照合する識別子。Safetyは常に必須。
#     ARTIFACT_EVALUATION/EVALUATION: 学習candidateの評価だけを指す互換名で、通常のOutcome評価やVerifierを制御しない]
# }
class RuntimeOperation(str, Enum):
    INFERENCE = "inference"
    ACTION = "action"
    EXPERIENCE_WRITE = "experience_write"
    KNOWLEDGE_READ = "knowledge_read"
    LIGHT_UPDATE = "light_update"
    KNOWLEDGE_BUILD = "knowledge_build"
    HEAVY_TRAINING = "heavy_training"
    ARTIFACT_EVALUATION = "evaluation"
    EVALUATION = "evaluation"
    PROMOTION = "promotion"
    SAFETY_CORE = "safety_core"


DEFAULT_RUNTIME_MODE = RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION
PHASE_REQUIRED_OPERATION = {
    RuntimePhase.INFER: RuntimeOperation.INFERENCE,
    RuntimePhase.LEARN: RuntimeOperation.KNOWLEDGE_BUILD,
    RuntimePhase.EVALUATE: RuntimeOperation.ARTIFACT_EVALUATION,
    RuntimePhase.PROMOTE: RuntimeOperation.PROMOTION,
}
_PLAY = frozenset(
    {
        RuntimeOperation.INFERENCE,
        RuntimeOperation.ACTION,
        RuntimeOperation.EXPERIENCE_WRITE,
    }
)
_LIGHT = frozenset(
    {
        RuntimeOperation.LIGHT_UPDATE,
        RuntimeOperation.KNOWLEDGE_BUILD,
        RuntimeOperation.EVALUATION,
        RuntimeOperation.PROMOTION,
    }
)
_BASE = frozenset({RuntimeOperation.KNOWLEDGE_READ, RuntimeOperation.SAFETY_CORE})
MODE_CAPABILITIES = {
    RuntimeMode.INFERENCE_ONLY: _BASE | _PLAY,
    RuntimeMode.LEARNING_ONLY: _BASE | _LIGHT | {RuntimeOperation.HEAVY_TRAINING},
    RuntimeMode.INFERENCE_WITH_LEARNING: _BASE
    | _PLAY
    | _LIGHT
    | {RuntimeOperation.HEAVY_TRAINING},
    RuntimeMode.ALTERNATING_INFER_LEARN: _BASE
    | _PLAY
    | _LIGHT
    | {RuntimeOperation.HEAVY_TRAINING},
    RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION: _BASE | _PLAY | _LIGHT,
}
LEARNING_OPERATIONS = _LIGHT | {RuntimeOperation.HEAVY_TRAINING}
FLAG_OPERATIONS = {
    f"{operation.value}_enabled": operation for operation in RuntimeOperation
}


# {
#   責務: [RuntimeModeConfig: 利用者のModeとresource policy参照から矛盾しない能力を導く]
#   フィールド: [mode: 5種類の方針, resource_policy_id: #425が解決する資源方針ID,
#     schema_version: この宣言契約の版。個別flagは保持せずModeから算出する]
# }
@dataclass(frozen=True, slots=True)
class RuntimeModeConfig:
    mode: RuntimeMode = DEFAULT_RUNTIME_MODE
    resource_policy_id: str = "default"
    schema_version: int = RUNTIME_MODE_SCHEMA_VERSION

    # {
    #   責務: [__post_init__: configの版、Mode、方針IDを検証する]
    #   処理: [自由なboolの組合せを受けず、既知enumと有界IDだけを保持する]
    #   引数: [self: 構築中config]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正なMode/版/識別子]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, RuntimeMode)
            or type(self.schema_version) is not int
            or self.schema_version != RUNTIME_MODE_SCHEMA_VERSION
        ):
            raise ValueError("runtime mode configuration version or mode is invalid")
        validate_runtime_identifier(self.resource_policy_id, "resource policy")

    # {
    #   責務: [flags: Modeから各能力flagを一意に導く]
    #   処理: [宣言済みoperationの有無と、昇格・degradeの固定方針を新しい辞書で返す]
    #   引数: [self: 対象config]
    #   戻り値: [dict: 個別に変更してもconfig本体へ影響しない能力宣言]
    # }
    @property
    def flags(self) -> dict:
        capabilities = MODE_CAPABILITIES[self.mode]
        return {
            **{
                name: operation in capabilities
                for name, operation in FLAG_OPERATIONS.items()
            },
            "auto_promotion_policy": "evaluated_safe_boundary"
            if RuntimeOperation.PROMOTION in capabilities
            else "disabled",
            "degrade_policy": "pause_learning"
            if self.mode == RuntimeMode.LEARNING_ONLY
            else "learning_first",
        }

    # {
    #   責務: [to_dict: Modeとderiveした能力をmachine-readableな辞書へ変換する]
    #   処理: [schema版、Mode、resource参照と能力表を出力する]
    #   引数: [self: 対象config]
    #   戻り値: [dict: JSON保存・UI参照用の独立データ]
    # }
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "resource_policy_id": self.resource_policy_id,
            **self.flags,
        }

    # {
    #   責務: [from_dict: 保存configを読み、Modeと矛盾する能力flagを拒否する]
    #   処理: [最小3項目を検証し、任意に付いたderive項目は正しい値と型のときだけ受け付ける]
    #   引数: [value: machine-readable config辞書]
    #   戻り値: [RuntimeModeConfig: 不変の検証済み宣言]
    #   エラー: [ValueError: 未知フィールド、版不一致、能力の矛盾]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "RuntimeModeConfig":
        required = {"schema_version", "mode", "resource_policy_id"}
        derived = set(FLAG_OPERATIONS) | {"auto_promotion_policy", "degrade_policy"}
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - derived
        ):
            raise ValueError("runtime mode configuration fields are invalid")
        try:
            result = cls(
                RuntimeMode(value["mode"]),
                value["resource_policy_id"],
                value["schema_version"],
            )
        except (TypeError, ValueError) as error:
            raise ValueError("runtime mode configuration is invalid") from error
        for name, expected in result.flags.items():
            if name in value and (
                type(value[name]) is not type(expected) or value[name] != expected
            ):
                raise ValueError("runtime capability flags contradict their mode")
        return result

    # {
    #   責務: [operations: 現Phaseで許可される能力をModeと合わせて導く]
    #   処理: [停止・待機PhaseではSafetyのみとし、ALTERNATINGの推論と学習を同時に許可しない]
    #   引数: [phase: 検証済みの現在段階]
    #   戻り値: [frozenset: 変更不能なoperation集合。実行時にはControllerの世代・停止・held検査も必要]
    #   エラー: [ValueError: Phaseがenumでない]
    # }
    def operations(self, phase: RuntimePhase) -> frozenset[RuntimeOperation]:
        if not isinstance(phase, RuntimePhase):
            raise ValueError("runtime phase is invalid")
        capabilities = MODE_CAPABILITIES[self.mode]
        if phase in {
            RuntimePhase.IDLE,
            RuntimePhase.PAUSED,
            RuntimePhase.STOPPING,
            RuntimePhase.ERROR,
        }:
            return frozenset({RuntimeOperation.SAFETY_CORE})
        if phase == RuntimePhase.INFER:
            allowed = _BASE | _PLAY
            if self.mode in {
                RuntimeMode.INFERENCE_WITH_LEARNING,
                RuntimeMode.INFERENCE_WITH_LIGHT_ADAPTATION,
            }:
                allowed |= LEARNING_OPERATIONS - {RuntimeOperation.PROMOTION}
        elif phase == RuntimePhase.LEARN:
            allowed = _BASE | LEARNING_OPERATIONS | {RuntimeOperation.EXPERIENCE_WRITE}
            allowed -= {RuntimeOperation.PROMOTION}
        elif phase == RuntimePhase.EVALUATE:
            allowed = _BASE | {
                RuntimeOperation.EVALUATION,
                RuntimeOperation.EXPERIENCE_WRITE,
            }
        else:
            allowed = _BASE | {
                RuntimeOperation.PROMOTION,
                RuntimeOperation.EXPERIENCE_WRITE,
            }
        return frozenset(capabilities & allowed)


# {
#   責務: [load_runtime_mode_config: 不正な利用者configを既定の安全な宣言へ戻す]
#   処理: [厳密な読取に失敗した場合、DEFAULT_LIGHTと固定reason codeを返す]
#   引数: [value: 保存・UIからのconfig候補]
#   戻り値: [tuple: configとfallback理由。正常なら理由はNone。Phaseを自動開始しない]
# }
def load_runtime_mode_config(value: object) -> tuple[RuntimeModeConfig, str | None]:
    try:
        return RuntimeModeConfig.from_dict(value), None
    except ValueError:
        return RuntimeModeConfig(), "invalid_config"


# {
#   責務: [validate_runtime_identifier: 方針・版・理由を自由文やcredentialを含まない有界IDへ制限する]
#   処理: [型、長さ、許可文字、既知credential prefixを検査する]
#   引数: [value: 入力識別子, name: 固定の検査項目名]
#   戻り値: [str: 検証済み識別子]
#   エラー: [ValueError: 不正な識別子。入力文字列自体を返さない]
# }
def validate_runtime_identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or value.startswith(("sk-", "ghp_", "github_pat_"))
    ):
        raise ValueError(f"{name} must be a bounded runtime identifier")
    return value
