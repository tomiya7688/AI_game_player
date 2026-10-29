"""配置manifestとPython/C ABIの宣言を照合する開発用検査。

実行時の利用可否や配布物の起動を証明する検査ではありません。manifestの型、
重複、粒度、言語選定、配布入口の宣言を検査し、不正な設定はエラー一覧で返します。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COARSE_UNITS = {
    "frame",
    "observation",
    "candidate_set",
    "decision",
    "batch",
    "control_lease",
}
REQUIRED_LANGUAGES = {"python", "cpp", "lua", "csharp", "javascript_typescript"}
LANGUAGE_DECISIONS = {"adopted", "deferred", "optional", "rejected"}


def _python_contract(path: Path) -> tuple[int, set[str]]:
    """Pythonをimportせず、契約versionと能力名をASTから読み取ります。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    version = None
    capabilities: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "RUNTIME_CONTRACT_VERSION"
                ):
                    if (
                        isinstance(node.value, ast.Constant)
                        and type(node.value.value) is int
                    ):
                        version = node.value.value
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeCapability":
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(
                    item.value, ast.Constant
                ):
                    if isinstance(item.value.value, str):
                        capabilities.add(item.value.value)
    if version is None or version <= 0:
        raise ValueError("positive integer RUNTIME_CONTRACT_VERSION was not found")
    if not capabilities:
        raise ValueError("RuntimeCapability values were not found")
    return version, capabilities


def _native_contract(path: Path) -> tuple[int, set[str]]:
    """Cヘッダーを実行せず、ABI versionと能力マクロを読み取ります。"""
    text = path.read_text(encoding="utf-8")
    # 整数リテラルだけを読みます。Cの式やマクロ展開は評価しません。
    version = re.search(
        r"^#define\s+KADOKA_RUNTIME_ABI_VERSION\s+(\d+)[uU]?\s*$", text, re.MULTILINE
    )
    capabilities = {
        match.group(1).lower()
        for match in re.finditer(
            r"^#define\s+KADOKA_CAP_([A-Z0-9_]+)\s+", text, re.MULTILINE
        )
    }
    if version is None or int(version.group(1)) <= 0:
        raise ValueError("positive KADOKA_RUNTIME_ABI_VERSION was not found")
    if not capabilities:
        raise ValueError("native capability macros were not found")
    return int(version.group(1)), capabilities


def _string_set(value: object, label: str, errors: list[str]) -> set[str]:
    """文字列一覧を検査し、重複を集合化して隠す前にエラーへ残します。"""
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty string list")
        return set()
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label} contains an invalid string")
        elif item in result:
            errors.append(f"duplicate {label}: {item}")
        else:
            result.add(item)
    return result


def _check_languages(value: object, errors: list[str]) -> set[str]:
    """言語ごとの採否、用途、保留時の再評価条件を検査します。"""
    if not isinstance(value, dict):
        errors.append("languages must be an object")
        return set()
    if missing := REQUIRED_LANGUAGES - set(value):
        errors.append(f"language evaluation is missing: {sorted(missing)}")
    for name, description in value.items():
        if not isinstance(description, dict):
            errors.append(f"language {name!r} must be an object")
            continue
        decision = description.get("decision")
        if not isinstance(decision, str) or decision not in LANGUAGE_DECISIONS:
            errors.append(f"language {name!r} needs an explicit adoption decision")
        _string_set(description.get("roles"), f"language {name!r} roles", errors)
        if description.get("decision") == "deferred":
            condition = description.get("revisit_when")
            if not isinstance(condition, str) or not condition.strip():
                errors.append(f"deferred language {name!r} needs revisit_when")
    return set(value)


def _check_components(
    value: object,
    languages: set[str],
    units: set[str],
    capabilities: set[str],
    errors: list[str],
) -> None:
    """責務名の一意性と、能力ごとに配置が一件だけあることを検査します。"""
    if not isinstance(value, list) or not value:
        errors.append("components must be a non-empty object list")
        return
    names: set[str] = set()
    assigned: set[str] = set()
    for index, component in enumerate(value):
        if not isinstance(component, dict):
            errors.append(f"components[{index}] must be an object")
            continue
        for field in (
            "name",
            "preferred_language",
            "current_language",
            "boundary_unit",
            "status",
            "reason",
        ):
            if (
                not isinstance(component.get(field), str)
                or not component[field].strip()
            ):
                errors.append(f"components[{index}].{field} must be a non-empty string")
        name = component.get("name")
        label = (
            f"component {name!r}"
            if isinstance(name, str) and name.strip()
            else f"components[{index}]"
        )
        if isinstance(name, str):
            if name in names:
                errors.append(f"duplicate component: {name}")
            names.add(name)
        language = component.get("preferred_language")
        if not isinstance(language, str) or language not in languages:
            errors.append(f"{label} uses an unevaluated language")
        unit = component.get("boundary_unit")
        if not isinstance(unit, str) or unit not in units:
            errors.append(f"{label} uses an unapproved boundary unit")
        if "capability" in component:
            capability = component["capability"]
            if not isinstance(capability, str) or capability not in capabilities:
                errors.append(f"{label} references an unknown capability")
            elif capability in assigned:
                errors.append(f"duplicate capability placement: {capability}")
            else:
                assigned.add(capability)
    if assigned != capabilities:
        errors.append(
            f"capability placements do not match: {sorted(assigned)} expected={sorted(capabilities)}"
        )


def _check_distribution(root: Path, value: object, errors: list[str]) -> None:
    """配布入口がリポジトリ内にあり、DLL名の宣言を含むことを検査します。"""
    if not isinstance(value, dict) or value.get("self_contained") is not True:
        errors.append("end-user distribution must declare self_contained true")
        return
    relative = value.get("windows_bundle_entrypoint")
    if not isinstance(relative, str) or not relative.strip():
        errors.append("Windows bundle entrypoint must be a relative file path")
        return
    try:
        entrypoint = (root / relative).resolve()
    except (OSError, ValueError) as error:
        errors.append(f"invalid Windows bundle entrypoint: {error}")
        return
    if Path(relative).is_absolute() or not entrypoint.is_relative_to(root.resolve()):
        errors.append("Windows bundle entrypoint must remain inside the repository")
        return
    try:
        text = entrypoint.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(f"cannot read Windows bundle entrypoint: {error}")
        return
    library = value.get("bundled_native_library")
    if not isinstance(library, str) or not library.strip() or library not in text:
        errors.append("Windows bundle entrypoint does not declare the native library")
    # この文字列検査は実行ファイル・DLLの生成や起動の成功を保証しません。


def check_boundary(root: Path = ROOT, manifest_path: Path | None = None) -> list[str]:
    """指定ルートの配置manifestと契約宣言を照合し、成功なら空の一覧を返します。"""
    manifest_file = (
        manifest_path
        if manifest_path is not None
        else root / "config/runtime_boundary.json"
    )
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        return [f"cannot read runtime boundary manifest {manifest_file}: {error}"]
    if not isinstance(manifest, dict):
        return ["runtime boundary manifest must be a JSON object"]
    python_path = root / "src/ai_game_player/runtime/contracts.py"
    native_path = root / "native/include/kadoka/runtime_api.h"
    try:
        python_version, python_capabilities = _python_contract(python_path)
    except (OSError, UnicodeError, SyntaxError, ValueError) as error:
        return [f"cannot read Python contract {python_path}: {error}"]
    try:
        native_version, native_capabilities = _native_contract(native_path)
    except (OSError, UnicodeError, ValueError) as error:
        return [f"cannot read native contract {native_path}: {error}"]
    errors: list[str] = []
    if manifest.get("schema") != "kadoka-runtime-boundary/v1":
        errors.append("manifest schema must be kadoka-runtime-boundary/v1")
    version = manifest.get("contract_version")
    if (
        type(version) is not int
        or version != python_version
        or version != native_version
    ):
        errors.append(
            f"contract version mismatch: manifest={version!r} python={python_version} native={native_version}"
        )
    capabilities = _string_set(manifest.get("capabilities"), "capabilities", errors)
    if capabilities != python_capabilities or capabilities != native_capabilities:
        errors.append("manifest/Python/native capability mismatch")
    units = _string_set(
        manifest.get("coarse_boundary_units"), "coarse_boundary_units", errors
    )
    if unknown := units - COARSE_UNITS:
        errors.append(f"unapproved or fine-grained boundary units: {sorted(unknown)}")
    languages = _check_languages(manifest.get("languages"), errors)
    _check_components(
        manifest.get("components"), languages, units, capabilities, errors
    )
    _check_distribution(root, manifest.get("distribution"), errors)
    if not (root / "doc/architecture/runtime_layers.md").is_file():
        errors.append("runtime architecture document is missing")
    return errors


def check_runtime_boundary_cli() -> int:
    """任意のmanifestを検査し、不整合を標準エラーへ表示して終了値1を返します。"""
    parser = argparse.ArgumentParser(
        description="Validate runtime placement and ABI declarations (not runtime availability)"
    )
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "config/runtime_boundary.json"
    )
    arguments = parser.parse_args()
    errors = check_boundary(ROOT, arguments.manifest)
    if errors:
        for error in errors:
            print(f"RUNTIME BOUNDARY ERROR: {error}", file=sys.stderr)
        return 1
    print("RUNTIME BOUNDARY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_runtime_boundary_cli())
