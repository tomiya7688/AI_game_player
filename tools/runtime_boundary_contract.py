"""Python/C ABIの宣言だけを読み、能力名と64bitの割当を照合する補助処理。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

UINT64_LIMIT = 1 << 64


def _integer_value(node: ast.AST, names: dict[str, int] | None = None) -> int:
    """整数・名前・左シフト・ORだけを解釈し、コードや巨大なシフトは実行しません。"""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        value = node.value
    elif isinstance(node, ast.Name) and names is not None and node.id in names:
        value = names[node.id]
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.LShift):
        shift = _integer_value(node.right, names)
        if shift >= 64:
            raise ValueError("capability shift must be less than 64")
        value = _integer_value(node.left, names) << shift
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        value = _integer_value(node.left, names) | _integer_value(node.right, names)
    else:
        raise ValueError("unsupported capability integer declaration")
    if not 0 <= value < UINT64_LIMIT:
        raise ValueError("capability value must fit uint64")
    return value


def _validate_bits(values: dict[str, int]) -> None:
    """能力ごとの割当が非ゼロの一ビットで、互いに重ならないことを確認します。"""
    assigned: set[int] = set()
    if not values:
        raise ValueError("capability bits were not found")
    for name, bit in values.items():
        if bit == 0 or bit & (bit - 1):
            raise ValueError(f"capability {name} must have one nonzero bit")
        if bit in assigned:
            raise ValueError(f"duplicate capability bit: {name}")
        assigned.add(bit)


def read_python_contract(path: Path) -> tuple[int, set[str]]:
    """高水準契約のversionと能力名をASTから読み取ります。"""
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
    if version is None or version <= 0 or not capabilities:
        raise ValueError("Python contract version or capabilities were not found")
    return version, capabilities


def read_native_contract(path: Path) -> tuple[int, dict[str, int]]:
    """Cの整数宣言を限定的に読み、versionと能力ビットを返します。"""
    text = path.read_text(encoding="utf-8")
    declarations = re.findall(
        r"^#define\s+KADOKA_RUNTIME_ABI_VERSION\b([^\r\n]*)", text, re.MULTILINE
    )
    if len(declarations) != 1:
        raise ValueError(
            "exactly one KADOKA_RUNTIME_ABI_VERSION declaration is required"
        )
    version = re.fullmatch(r"\s*(\d+)[uU]?\s*", declarations[0])
    if version is None or int(version.group(1)) <= 0:
        raise ValueError("positive KADOKA_RUNTIME_ABI_VERSION was not found")
    bits: dict[str, int] = {}
    for match in re.finditer(
        r"^#define\s+KADOKA_CAP_([A-Z0-9_]+)\s+([^\r\n]+)", text, re.MULTILINE
    ):
        name = match.group(1).lower()
        if name in bits:
            raise ValueError(f"duplicate native capability declaration: {name}")
        expression = re.sub(r"/\*.*?\*/|//.*", "", match.group(2)).strip()
        expression = re.sub(
            r"UINT64_C\(\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)", r"\1", expression
        )
        expression = re.sub(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", expression)
        bits[name] = _integer_value(ast.parse(expression, mode="eval").body)
    _validate_bits(bits)
    return int(version.group(1)), bits


def read_binding_contract(path: Path) -> tuple[int, dict[str, int]]:
    """NativeRuntimeの実際のdescriptor割当とCAP定数をASTで確認します。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, int] = {}
    version = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id.startswith("CAP_"):
                constants[target.id] = _integer_value(node.value, constants)
            if target.id == "NATIVE_ABI_VERSION":
                version = _integer_value(node.value)
    bits: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
            continue
        bit, capability = node.elts
        if not (
            isinstance(capability, ast.Attribute)
            and isinstance(capability.value, ast.Name)
            and capability.value.id == "RuntimeCapability"
        ):
            continue
        name = capability.attr.lower()
        value = _integer_value(bit, constants)
        if name in bits and bits[name] != value:
            raise ValueError(f"conflicting Python binding capability: {name}")
        bits[name] = value
    _validate_bits(bits)
    for name, bit in constants.items():
        if bits.get(name[4:].lower()) != bit:
            raise ValueError(
                f"Python binding constant disagrees with descriptor: {name}"
            )
    if version is None or version <= 0:
        raise ValueError("positive binding NATIVE_ABI_VERSION was not found")
    return version, bits
