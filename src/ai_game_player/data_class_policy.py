"""Privacy data class 1件の既定保存・期限・外部送信policy（Issue #224）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

SENSITIVITIES = ("low", "medium", "high", "forbidden")
PERSISTENCE_MODES = ("never", "off", "transient", "local_bounded", "local")
REMOTE_MODES = ("never", "opt_in")
_DATA_CLASS_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_MODULE_NAME = re.compile(r"^ai_game_player(\.[a-z_][a-z0-9_]*)+$")
# 既定で保存されるmode。offは利用者が明示的に有効化した時だけ保存する。
_PERSISTED_BY_DEFAULT = frozenset({"transient", "local_bounded", "local"})
# TTLを持たないmode。期限の意味がないため、値があれば誤設定として拒否する。
_NO_TTL_MODES = frozenset({"never", "transient", "local"})


# {
#   責務: [DataClassPolicyError: data class policyが契約外で、保存・送信判断に使えないことを通知する]
#   フィールド: [なし: ValueErrorの理由文だけを持つ]
# }
class DataClassPolicyError(ValueError):
    """A data class policy violates the privacy inventory contract."""


# {
#   責務: [DataClassPolicy: 1つのdata classの機微度・1.0既定保存・既定期限・外部送信可否・所有moduleを不変値で固定する]
#   フィールド: [data_class: class ID,
#     sensitivity: low/medium/high/forbidden。forbiddenはcredential等の通常data flow禁止,
#     default_persistence: never（常に保存禁止）/off（既定OFF、明示opt-in時だけ保存）/transient（処理中だけの一時file）/
#       local_bounded（既定で端末内に期限付き保存）/local（利用者が消すまで端末内保存）,
#     default_retention_ms: 保存する場合の既定TTL（ms）。Noneは期限なし,
#     remote: never（端末外へ送らない）/opt_in（利用者の明示同意がある時だけ送れる。既定は送らない）,
#     contains: このclassのpayloadが内包し得る他class ID,
#     owner_modules: 生成・保存を担うmodule名,
#     implemented: 現行コードに生成元があるか,
#     description: 利用者向けの説明]
# }
@dataclass(frozen=True)
class DataClassPolicy:
    data_class: str
    sensitivity: str
    default_persistence: str
    default_retention_ms: int | None
    remote: str
    contains: tuple[str, ...]
    owner_modules: tuple[str, ...]
    implemented: bool
    description: str

    # {
    #   責務: [__post_init__: 列挙値の誤りと、privacy-preservingでない既定を生成時点で拒否する]
    #   処理: [ID・列挙値・TTL・module名を検査し、forbiddenは保存も送信も禁止、
    #     highは無期限local保存禁止、local_boundedはTTL必須、never/transient/localはTTLなし、という不変条件を確認する]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [DataClassPolicyError: いずれかの値・不変条件が契約外]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.data_class, str) or not _DATA_CLASS_ID.fullmatch(
            self.data_class
        ):
            raise DataClassPolicyError("data class ID must be a lowercase identifier")
        name = self.data_class
        _require_choice(self.sensitivity, SENSITIVITIES, f"{name}.sensitivity")
        _require_choice(
            self.default_persistence,
            PERSISTENCE_MODES,
            f"{name}.default_persistence",
        )
        _require_choice(self.remote, REMOTE_MODES, f"{name}.remote")
        retention = self.default_retention_ms
        if retention is not None and (type(retention) is not int or retention < 1):
            raise DataClassPolicyError(
                f"{name}.default_retention_ms must be null or a positive integer"
            )
        if not isinstance(self.contains, tuple) or not all(
            isinstance(item, str) and _DATA_CLASS_ID.fullmatch(item)
            for item in self.contains
        ):
            raise DataClassPolicyError(f"{name}.contains must list data class IDs")
        if name in self.contains or len(set(self.contains)) != len(self.contains):
            raise DataClassPolicyError(
                f"{name}.contains must not repeat itself or another class"
            )
        if not isinstance(self.owner_modules, tuple) or not all(
            isinstance(item, str) and _MODULE_NAME.fullmatch(item)
            for item in self.owner_modules
        ):
            raise DataClassPolicyError(
                f"{name}.owner_modules must list ai_game_player modules"
            )
        if type(self.implemented) is not bool:
            raise DataClassPolicyError(f"{name}.implemented must be a boolean")
        if self.implemented and not self.owner_modules:
            raise DataClassPolicyError(f"{name} is implemented but has no owner")
        if not isinstance(self.description, str) or not self.description.strip():
            raise DataClassPolicyError(f"{name}.description must not be empty")
        if self.sensitivity == "forbidden" and (
            self.default_persistence != "never" or self.remote != "never"
        ):
            raise DataClassPolicyError(f"{name} is forbidden and must never be stored")
        if self.sensitivity == "high" and self.default_persistence == "local":
            raise DataClassPolicyError(f"{name} is high sensitivity and needs a bound")
        if self.default_persistence == "local_bounded" and retention is None:
            raise DataClassPolicyError(f"{name} is local_bounded and needs a TTL")
        if self.default_persistence in _NO_TTL_MODES and retention is not None:
            raise DataClassPolicyError(
                f"{name} is {self.default_persistence} and must not declare a TTL"
            )

    # {
    #   責務: [persisted_by_default: 利用者の操作なしに端末内へ保存される既定かを返す]
    #   処理: [transient/local_bounded/localならTrue、never/offならFalse]
    #   引数: [なし]
    #   戻り値: [bool: 既定で保存されるならTrue]
    # }
    @property
    def persisted_by_default(self) -> bool:
        return self.default_persistence in _PERSISTED_BY_DEFAULT

    # {
    #   責務: [may_leave_machine: 利用者の明示同意があれば端末外へ送れるclassかを返す]
    #   処理: [remoteがopt_inならTrue。既定で送信されることは意味しない]
    #   引数: [なし]
    #   戻り値: [bool: 同意後に送信可能ならTrue]
    # }
    @property
    def may_leave_machine(self) -> bool:
        return self.remote == "opt_in"

    # {
    #   責務: [from_dict: inventory JSONのclass 1件を厳格に復元する]
    #   処理: [キー集合を検査し、配列をtupleへ変換して値検査は__post_init__へ委ねる]
    #   引数: [value: data class JSON object]
    #   戻り値: [DataClassPolicy: 検証済みpolicy]
    #   エラー: [DataClassPolicyError: objectでない、キー不一致、値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> DataClassPolicy:
        if not isinstance(value, dict):
            raise DataClassPolicyError("data class must be an object")
        expected = {
            "id",
            "sensitivity",
            "default_persistence",
            "default_retention_ms",
            "remote",
            "contains",
            "owner_modules",
            "implemented",
            "description",
        }
        if set(value) != expected:
            raise DataClassPolicyError(
                f"invalid data class fields: {sorted(map(str, set(value) ^ expected))}"
            )
        contains = value["contains"]
        owners = value["owner_modules"]
        if not isinstance(contains, list) or not isinstance(owners, list):
            raise DataClassPolicyError("contains and owner_modules must be arrays")
        return cls(
            data_class=value["id"],
            sensitivity=value["sensitivity"],
            default_persistence=value["default_persistence"],
            default_retention_ms=value["default_retention_ms"],
            remote=value["remote"],
            contains=tuple(contains),
            owner_modules=tuple(owners),
            implemented=value["implemented"],
            description=value["description"],
        )


# {
#   責務: [_require_choice: 値が許可された列挙値の1つか検査する]
#   処理: [str型かつ候補に含まれることを確認する]
#   引数: [value: 検査値, choices: 許可値, name: エラー文に出すフィールド名]
#   戻り値: [なし]
#   エラー: [DataClassPolicyError: 列挙値でない]
# }
def _require_choice(value: object, choices: tuple[str, ...], name: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise DataClassPolicyError(f"{name} must be one of {list(choices)}")
