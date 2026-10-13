from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FINE_UNITS = {"pixel", "token", "candidate"}
REQUIRED_LANGUAGES = {"python", "cpp", "lua", "csharp", "javascript_typescript"}


def _python_contract(path: Path) -> tuple[int, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    version = None
    capabilities: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "RUNTIME_CONTRACT_VERSION":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                        version = node.value.value
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeCapability":
            for item in node.body:
                if not isinstance(item, ast.Assign) or not isinstance(item.value, ast.Constant):
                    continue
                if isinstance(item.value.value, str):
                    capabilities.add(item.value.value)
    if version is None:
        raise ValueError("RUNTIME_CONTRACT_VERSION was not found")
    if not capabilities:
        raise ValueError("RuntimeCapability values were not found")
    return version, capabilities


def _native_contract(path: Path) -> tuple[int, set[str]]:
    text = path.read_text(encoding="utf-8")
    version_match = re.search(r"^#define\s+KADOKA_RUNTIME_ABI_VERSION\s+(\d+)u\s*$", text, re.MULTILINE)
    if version_match is None:
        raise ValueError("KADOKA_RUNTIME_ABI_VERSION was not found")
    capabilities = {
        match.group(1).lower()
        for match in re.finditer(r"^#define\s+KADOKA_CAP_([A-Z0-9_]+)\s+", text, re.MULTILINE)
    }
    if not capabilities:
        raise ValueError("native capability macros were not found")
    return int(version_match.group(1)), capabilities


def check_boundary(root: Path = ROOT, manifest_path: Path | None = None) -> list[str]:
    manifest_file = manifest_path or root / "config" / "runtime_boundary.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        return ["runtime boundary manifest must be a JSON object"]

    errors: list[str] = []
    if manifest.get("schema") != "kadoka-runtime-boundary/v1":
        errors.append("manifest schema must be kadoka-runtime-boundary/v1")

    try:
        python_version, python_capabilities = _python_contract(
            root / "src" / "ai_game_player" / "runtime" / "contracts.py"
        )
        native_version, native_capabilities = _native_contract(
            root / "native" / "include" / "kadoka" / "runtime_api.h"
        )
    except (OSError, SyntaxError, ValueError) as exc:
        return [str(exc)]

    manifest_version = manifest.get("contract_version")
    if manifest_version != python_version or manifest_version != native_version:
        errors.append(
            "contract version mismatch: "
            f"manifest={manifest_version!r} python={python_version} native={native_version}"
        )

    raw_capabilities = manifest.get("capabilities", [])
    manifest_capabilities = {str(value) for value in raw_capabilities} if isinstance(raw_capabilities, list) else set()
    if manifest_capabilities != python_capabilities:
        errors.append(
            "manifest/Python capability mismatch: "
            f"manifest={sorted(manifest_capabilities)} python={sorted(python_capabilities)}"
        )
    if manifest_capabilities != native_capabilities:
        errors.append(
            "manifest/native capability mismatch: "
            f"manifest={sorted(manifest_capabilities)} native={sorted(native_capabilities)}"
        )

    raw_units = manifest.get("coarse_boundary_units", [])
    coarse_units = {str(value) for value in raw_units} if isinstance(raw_units, list) else set()
    if not coarse_units:
        errors.append("coarse_boundary_units must not be empty")
    forbidden = coarse_units & FORBIDDEN_FINE_UNITS
    if forbidden:
        errors.append(f"fine-grained units must not be allowed at language boundaries: {sorted(forbidden)}")

    raw_languages = manifest.get("languages", {})
    languages = raw_languages if isinstance(raw_languages, dict) else {}
    missing_languages = REQUIRED_LANGUAGES - set(languages)
    if missing_languages:
        errors.append(f"language evaluation is missing: {sorted(missing_languages)}")
    for name, value in languages.items():
        if not isinstance(value, dict) or value.get("decision") not in {"adopted", "deferred", "optional", "rejected"}:
            errors.append(f"language {name!r} needs an explicit adoption decision")

    raw_components = manifest.get("components", [])
    components = raw_components if isinstance(raw_components, list) else []
    seen: set[str] = set()
    capability_components: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            errors.append(f"components[{index}] must be an object")
            continue
        name = str(component.get("name", "")).strip()
        if not name:
            errors.append(f"components[{index}] has no name")
            continue
        if name in seen:
            errors.append(f"duplicate component: {name}")
        seen.add(name)
        preferred_language = str(component.get("preferred_language", ""))
        if preferred_language not in languages:
            errors.append(f"component {name!r} uses unevaluated language {preferred_language!r}")
        unit = str(component.get("boundary_unit", ""))
        if unit not in coarse_units:
            errors.append(f"component {name!r} uses unapproved boundary unit {unit!r}")
        capability = component.get("capability")
        if capability is not None:
            capability_name = str(capability)
            capability_components.add(capability_name)
            if capability_name not in manifest_capabilities:
                errors.append(f"component {name!r} references unknown capability {capability_name!r}")
    if capability_components != manifest_capabilities:
        errors.append(
            "every runtime capability needs exactly one placement component: "
            f"components={sorted(capability_components)} expected={sorted(manifest_capabilities)}"
        )

    distribution = manifest.get("distribution", {})
    if not isinstance(distribution, dict) or distribution.get("self_contained") is not True:
        errors.append("end-user distribution must be self-contained")
    else:
        entrypoint = root / str(distribution.get("windows_bundle_entrypoint", ""))
        if not entrypoint.is_file():
            errors.append(f"Windows bundle entrypoint does not exist: {entrypoint}")
        else:
            bundle_text = entrypoint.read_text(encoding="utf-8")
            native_name = str(distribution.get("bundled_native_library", ""))
            if not native_name or native_name not in bundle_text:
                errors.append("Windows bundle entrypoint does not include the declared native runtime library")

    architecture_doc = root / "doc" / "architecture" / "runtime_layers.md"
    if not architecture_doc.is_file():
        errors.append("runtime architecture document is missing")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the versioned high-level/native runtime boundary")
    parser.add_argument("--manifest", type=Path, default=ROOT / "config" / "runtime_boundary.json")
    args = parser.parse_args()
    errors = check_boundary(ROOT, args.manifest)
    if errors:
        for error in errors:
            print(f"RUNTIME BOUNDARY ERROR: {error}", file=sys.stderr)
        return 1
    print("RUNTIME BOUNDARY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
