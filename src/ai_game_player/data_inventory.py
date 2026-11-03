"""Privacy data classの版付きinventoryを読み、componentへpolicyを問い合わせる窓口（Issue #224）。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ai_game_player.bounded_file import read_regular_file
from ai_game_player.data_class_policy import (
    SENSITIVITIES,
    DataClassPolicy,
    DataClassPolicyError,
)

DATA_INVENTORY_SCHEMA_VERSION = 1
DEFAULT_DATA_INVENTORY_PATH = (
    Path(__file__).resolve().parent / "assets" / "privacy" / "data_inventory.json"
)
MAX_DATA_INVENTORY_BYTES = 256 * 1024
_SENSITIVITY_RANK = {name: rank for rank, name in enumerate(SENSITIVITIES)}
_ARTIFACT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SITE = re.compile(r"^ai_game_player(\.[a-z_][a-z0-9_]*)+:[A-Za-z_][A-Za-z0-9_.]*$")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "policy_version",
        "classes",
        "artifact_types",
        "writers",
        "transmitters",
        "non_data_paths",
        "forbidden_field_names",
    }
)


# {
#   責務: [DataInventoryError: data inventoryが契約外、または問合せ対象が未登録であることを通知する]
#   フィールド: [なし: ValueErrorの理由文だけを持つ]
# }
class DataInventoryError(ValueError):
    """The data inventory is invalid or a queried item is not registered."""


# {
#   責務: [DataInventory: data class policyと、保存・送信を行うコード位置・artifact型・file名からclassへの対応を不変に保持し問合せに答える]
#   フィールド: [policy_version: 既定policyの版（例 1.0）,
#     classes: class ID→DataClassPolicy,
#     artifact_types: Artifact Storeのartifact_type→class ID,
#     writers: 永続化を行うコード位置（module:qualname）→class ID。Noneは呼出側でclassが決まる基盤,
#     persisted_paths: writerが作るfile/directory名→class ID（基盤ならNone）,
#     transmitters: 端末外へ送り得るコード位置→送信し得るclass IDの集合,
#     non_data_paths: 利用者dataを保存しないpath名→理由,
#     forbidden_field_names: credentialとして通常data flowで禁止するfield名（正規化済み）]
#   処理: [policy・class_for_artifact_type・effective_classesでclass判定を返し、
#     forbidden_field_pathsでcredential混入を検出し、ttl_ms_by_artifact_typeでRetention Coreへ既定TTLを渡す]
# }
@dataclass(frozen=True)
class DataInventory:
    policy_version: str
    classes: Mapping[str, DataClassPolicy]
    artifact_types: Mapping[str, str]
    writers: Mapping[str, str | None]
    persisted_paths: Mapping[str, str | None]
    transmitters: Mapping[str, frozenset[str]]
    non_data_paths: Mapping[str, str]
    forbidden_field_names: frozenset[str]

    # {
    #   責務: [policy: class IDのpolicyを返す]
    #   処理: [登録済みclassを引き、未登録なら推測せず拒否する]
    #   引数: [data_class: class ID]
    #   戻り値: [DataClassPolicy: 該当policy]
    #   エラー: [DataInventoryError: 未登録class]
    # }
    def policy(self, data_class: str) -> DataClassPolicy:
        try:
            return self.classes[data_class]
        except KeyError:
            raise DataInventoryError(f"unknown data class: {data_class!r}") from None

    # {
    #   責務: [class_for_artifact_type: Artifact Storeのartifact_typeが属するclass policyを返す]
    #   処理: [artifact_types表を引く。未登録型は既定で許可せず拒否する]
    #   引数: [artifact_type: ArtifactReference.artifact_type]
    #   戻り値: [DataClassPolicy: 該当policy]
    #   エラー: [DataInventoryError: 未登録のartifact_type]
    # }
    def class_for_artifact_type(self, artifact_type: str) -> DataClassPolicy:
        try:
            return self.classes[self.artifact_types[artifact_type]]
        except KeyError:
            raise DataInventoryError(
                f"artifact type is not mapped to a data class: {artifact_type!r}"
            ) from None

    # {
    #   責務: [effective_classes: 指定classと、そのpayloadが内包し得る全classの集合を返す]
    #   処理: [containsを推移的に辿る。送信同意の判定では内包classも同意対象にするため使う]
    #   引数: [data_classes: 起点のclass ID列]
    #   戻り値: [frozenset[str]: 起点と内包classのID集合]
    #   エラー: [DataInventoryError: 未登録class]
    # }
    def effective_classes(self, data_classes: Iterable[str]) -> frozenset[str]:
        pending = list(data_classes)
        seen: set[str] = set()
        while pending:
            data_class = pending.pop()
            if data_class in seen:
                continue
            seen.add(data_class)
            pending.extend(self.policy(data_class).contains)
        return frozenset(seen)

    # {
    #   責務: [remote_eligible_classes: 利用者の明示同意があれば端末外へ送れるclass ID集合を返す]
    #   処理: [remoteがopt_inのclassだけを集める。既定で送信されることは意味しない]
    #   引数: [なし]
    #   戻り値: [frozenset[str]: 同意後に送信可能なclass ID]
    # }
    def remote_eligible_classes(self) -> frozenset[str]:
        return frozenset(
            name for name, policy in self.classes.items() if policy.may_leave_machine
        )

    # {
    #   責務: [ttl_ms_by_artifact_type: RetentionPolicy.ttl_ms_by_typeへ渡す、artifact型ごとの既定TTLを返す]
    #   処理: [各artifact_typeのclassのdefault_retention_msを型名順に並べる]
    #   引数: [なし]
    #   戻り値: [dict[str, int | None]: artifact_type→TTL ms（Noneは無期限）]
    # }
    def ttl_ms_by_artifact_type(self) -> dict[str, int | None]:
        return {
            artifact_type: self.classes[data_class].default_retention_ms
            for artifact_type, data_class in sorted(self.artifact_types.items())
        }

    # {
    #   責務: [forbidden_field_paths: JSON互換値にcredential禁止field名が含まれる位置を列挙する]
    #   処理: [dict/list/tupleを再帰的に辿り、小文字化・`-`を`_`へ置換したkeyが禁止名と一致するか、
    #     `_禁止名`で終わる位置を`$.a.b[0]`形式で返す。値の中身は判定に使わない]
    #   引数: [value: 送信・保存前のpayload]
    #   戻り値: [tuple[str, ...]: 禁止field位置。空ならcredential名のfieldはない]
    # }
    def forbidden_field_paths(self, value: object) -> tuple[str, ...]:
        found: list[str] = []
        stack: list[tuple[str, object]] = [("$", value)]
        while stack:
            path, current = stack.pop()
            if isinstance(current, Mapping):
                for key, child in current.items():
                    child_path = f"{path}.{key}"
                    if isinstance(key, str) and self._is_forbidden_name(key):
                        found.append(child_path)
                    stack.append((child_path, child))
            elif isinstance(current, (list, tuple)):
                for index, child in enumerate(current):
                    stack.append((f"{path}[{index}]", child))
        return tuple(sorted(found))

    # {
    #   責務: [_is_forbidden_name: 1つのfield名がcredential禁止名に当たるか判定する]
    #   処理: [小文字化と`-`→`_`の正規化後、完全一致または`_禁止名`の接尾一致を調べる]
    #   引数: [key: field名]
    #   戻り値: [bool: 禁止名ならTrue]
    # }
    def _is_forbidden_name(self, key: str) -> bool:
        normalized = key.strip().lower().replace("-", "_")
        return any(
            normalized == name or normalized.endswith("_" + name)
            for name in self.forbidden_field_names
        )

    # {
    #   責務: [from_dict: inventory JSON全体を厳格に復元する]
    #   処理: [版とキー集合を検査し、class・artifact型・writer・transmitter・非data path・禁止field名を
    #     それぞれ検証して、未登録classへの参照や重複を部分結果なしで拒否する]
    #   引数: [value: inventory JSON object]
    #   戻り値: [DataInventory: 検証済みinventory]
    #   エラー: [DataInventoryError: 版・キー不一致、重複、未登録class参照、値が契約外]
    # }
    @classmethod
    def from_dict(cls, value: object) -> DataInventory:
        if not isinstance(value, dict):
            raise DataInventoryError("data inventory must be an object")
        if set(value) != _TOP_LEVEL_FIELDS:
            raise DataInventoryError(
                "invalid data inventory fields: "
                f"{sorted(map(str, set(value) ^ _TOP_LEVEL_FIELDS))}"
            )
        version = value["schema_version"]
        if type(version) is not int or version != DATA_INVENTORY_SCHEMA_VERSION:
            raise DataInventoryError(f"unsupported data inventory version: {version!r}")
        policy_version = value["policy_version"]
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise DataInventoryError("policy_version must be a non-empty string")
        classes = _parse_classes(value["classes"])
        known = frozenset(classes)
        artifact_types = _parse_artifact_types(value["artifact_types"], known)
        writers, persisted_paths = _parse_writers(value["writers"], known)
        transmitters = _parse_transmitters(value["transmitters"], known)
        non_data_paths = _parse_non_data_paths(value["non_data_paths"])
        overlap = set(persisted_paths) & set(non_data_paths)
        if overlap:
            raise DataInventoryError(
                f"paths are both persisted and non-data: {sorted(overlap)}"
            )
        names = value["forbidden_field_names"]
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and _is_field_name(name) for name in names)
            or len(set(names)) != len(names)
        ):
            raise DataInventoryError(
                "forbidden_field_names must be unique lowercase field names"
            )
        if not any(policy.sensitivity == "forbidden" for policy in classes.values()):
            raise DataInventoryError(
                "data inventory needs a forbidden credential class"
            )
        return cls(
            policy_version=policy_version,
            classes=MappingProxyType(classes),
            artifact_types=MappingProxyType(artifact_types),
            writers=MappingProxyType(writers),
            persisted_paths=MappingProxyType(persisted_paths),
            transmitters=MappingProxyType(transmitters),
            non_data_paths=MappingProxyType(non_data_paths),
            forbidden_field_names=frozenset(names),
        )


# {
#   責務: [load_data_inventory: inventory JSON fileを上限付きで読み検証済みinventoryを返す]
#   処理: [通常fileだけを上限byte以内で読み、UTF-8 JSONとして解析してfrom_dictへ渡す]
#   引数: [path: inventory JSON。省略時は同梱の正本]
#   戻り値: [DataInventory: 検証済みinventory]
#   エラー: [DataInventoryError: 読取・解析・検証の失敗]
# }
def load_data_inventory(path: Path | None = None) -> DataInventory:
    target = DEFAULT_DATA_INVENTORY_PATH if path is None else path
    try:
        raw = read_regular_file(target, max_bytes=MAX_DATA_INVENTORY_BYTES)
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise DataInventoryError(f"cannot read data inventory: {exc}") from exc
    return DataInventory.from_dict(document)


# {
#   責務: [_parse_classes: class配列をID→policyへ変換する]
#   処理: [各要素をDataClassPolicyへ復元し、ID重複とcontainsの未登録参照を拒否する。
#     外側classが内包classより低い機微度を持つ場合と、端末外送信可能なclassがlocal-only classを内包する場合も拒否する]
#   引数: [value: classes配列]
#   戻り値: [dict[str, DataClassPolicy]: ID→policy]
#   エラー: [DataInventoryError: 配列でない、空、重複、未登録参照、内包関係の矛盾、policy不正]
# }
def _parse_classes(value: object) -> dict[str, DataClassPolicy]:
    if not isinstance(value, list) or not value:
        raise DataInventoryError("classes must be a non-empty array")
    classes: dict[str, DataClassPolicy] = {}
    for item in value:
        try:
            policy = DataClassPolicy.from_dict(item)
        except DataClassPolicyError as exc:
            raise DataInventoryError(str(exc)) from exc
        if policy.data_class in classes:
            raise DataInventoryError(f"duplicate data class: {policy.data_class}")
        classes[policy.data_class] = policy
    for policy in classes.values():
        missing = set(policy.contains) - set(classes)
        if missing:
            raise DataInventoryError(
                f"{policy.data_class}.contains references unknown classes: {sorted(missing)}"
            )
        for contained in map(classes.__getitem__, policy.contains):
            # 内包classより緩い機微度・送信可否を外側classに付けると、外側経由で制約を迂回できる。
            if (
                _SENSITIVITY_RANK[policy.sensitivity]
                < _SENSITIVITY_RANK[contained.sensitivity]
            ):
                raise DataInventoryError(
                    f"{policy.data_class} is less sensitive than {contained.data_class}"
                )
            if policy.may_leave_machine and not contained.may_leave_machine:
                raise DataInventoryError(
                    f"{policy.data_class} may leave the machine but contains "
                    f"local-only {contained.data_class}"
                )
    return classes


# {
#   責務: [_parse_artifact_types: artifact_type→class表を検証する]
#   処理: [型名の書式と参照先classの登録を確認する]
#   引数: [value: artifact_types object, known: 登録済みclass ID]
#   戻り値: [dict[str, str]: artifact_type→class ID]
#   エラー: [DataInventoryError: objectでない、型名不正、未登録class参照]
# }
def _parse_artifact_types(value: object, known: frozenset[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise DataInventoryError("artifact_types must be an object")
    for artifact_type, data_class in value.items():
        if not isinstance(artifact_type, str) or not _ARTIFACT_TYPE.fullmatch(
            artifact_type
        ):
            raise DataInventoryError(f"invalid artifact type: {artifact_type!r}")
        _require_known(data_class, known, f"artifact_types[{artifact_type}]")
    return dict(value)


# {
#   責務: [_parse_writers: 永続化コード位置の表を、位置→classとpath名→classの2表へ変換する]
#   処理: [site書式・class参照・path名を検査する。classがnullの基盤はnoteを必須にし、
#     同じsiteや同じpath名の重複登録を拒否する]
#   引数: [value: writers配列, known: 登録済みclass ID]
#   戻り値: [tuple[dict, dict]: site→class ID|None と path名→class ID|None]
#   エラー: [DataInventoryError: 形・書式・参照・重複の不正]
# }
def _parse_writers(
    value: object, known: frozenset[str]
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    if not isinstance(value, list):
        raise DataInventoryError("writers must be an array")
    writers: dict[str, str | None] = {}
    paths: dict[str, str | None] = {}
    for item in value:
        if not isinstance(item, dict) or not {"site", "data_class", "paths"} <= set(
            item
        ):
            raise DataInventoryError("writer needs site, data_class and paths")
        if not set(item) <= {"site", "data_class", "paths", "note"}:
            raise DataInventoryError(f"invalid writer fields: {sorted(map(str, item))}")
        site = _require_site(item["site"], writers, "writer")
        data_class = item["data_class"]
        if data_class is None:
            note = item.get("note")
            if not isinstance(note, str) or not note.strip():
                raise DataInventoryError(f"{site} without data_class needs a note")
        else:
            _require_known(data_class, known, site)
        writers[site] = data_class
        site_paths = item["paths"]
        if not isinstance(site_paths, list):
            raise DataInventoryError(f"{site}.paths must be an array")
        for path in site_paths:
            if not isinstance(path, str) or not path.strip() or path in paths:
                raise DataInventoryError(f"invalid or duplicate path: {path!r}")
            paths[path] = data_class
    return writers, paths


# {
#   責務: [_parse_transmitters: 端末外へ送り得るコード位置→送信class集合の表を検証する]
#   処理: [site書式・重複・各class参照を確認し、noteを必須にする]
#   引数: [value: transmitters配列, known: 登録済みclass ID]
#   戻り値: [dict[str, frozenset[str]]: site→送信し得るclass ID]
#   エラー: [DataInventoryError: 形・書式・参照・重複の不正]
# }
def _parse_transmitters(
    value: object, known: frozenset[str]
) -> dict[str, frozenset[str]]:
    if not isinstance(value, list):
        raise DataInventoryError("transmitters must be an array")
    transmitters: dict[str, frozenset[str]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"site", "data_classes", "note"}:
            raise DataInventoryError("transmitter needs site, data_classes and note")
        site = _require_site(item["site"], transmitters, "transmitter")
        classes = item["data_classes"]
        if not isinstance(classes, list) or len(set(classes)) != len(classes):
            raise DataInventoryError(f"{site}.data_classes must be a unique array")
        for data_class in classes:
            _require_known(data_class, known, site)
        if not isinstance(item["note"], str) or not item["note"].strip():
            raise DataInventoryError(f"{site}.note must not be empty")
        transmitters[site] = frozenset(classes)
    return transmitters


# {
#   責務: [_parse_non_data_paths: 利用者dataを保存しないpath名→理由の表を検証する]
#   処理: [keyと理由が空でない文字列であることを確認する]
#   引数: [value: non_data_paths object]
#   戻り値: [dict[str, str]: path名→理由]
#   エラー: [DataInventoryError: objectでない、空文字]
# }
def _parse_non_data_paths(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and key.strip()
        and isinstance(reason, str)
        and reason.strip()
        for key, reason in value.items()
    ):
        raise DataInventoryError("non_data_paths must map path names to reasons")
    return dict(value)


# {
#   責務: [_require_site: コード位置文字列の書式と重複を検査する]
#   処理: [`ai_game_player.module:Qualname`形式で、既登録でないことを確認する]
#   引数: [value: site値, existing: 既登録site, kind: エラー文に出す種別]
#   戻り値: [str: 検証済みsite]
#   エラー: [DataInventoryError: 書式不正または重複]
# }
def _require_site(value: object, existing: Mapping[str, object], kind: str) -> str:
    if not isinstance(value, str) or not _SITE.fullmatch(value):
        raise DataInventoryError(f"invalid {kind} site: {value!r}")
    if value in existing:
        raise DataInventoryError(f"duplicate {kind} site: {value}")
    return value


# {
#   責務: [_require_known: 参照class IDが登録済みか検査する]
#   処理: [文字列かつ登録済みIDであることを確認する]
#   引数: [value: 参照値, known: 登録済みclass ID, owner: エラー文に出す参照元]
#   戻り値: [なし]
#   エラー: [DataInventoryError: 未登録class]
# }
def _require_known(value: object, known: frozenset[str], owner: str) -> None:
    if not isinstance(value, str) or value not in known:
        raise DataInventoryError(f"{owner} references unknown data class: {value!r}")


# {
#   責務: [_is_field_name: 禁止field名が正規化済み（小文字・`_`区切り）か判定する]
#   処理: [小文字英数字と`_`だけで構成されるか確認する]
#   引数: [value: field名]
#   戻り値: [bool: 正規化済みならTrue]
# }
def _is_field_name(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]*", value) is not None
