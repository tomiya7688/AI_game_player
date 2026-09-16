from __future__ import annotations

import ast
from pathlib import Path


def run_bug_checks(paths: list[Path]) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    scanned_files = 0
    for root in paths:
        for path in _python_files(root):
            scanned_files += 1
            findings.extend(_scan_file(path))
    findings.sort(key=lambda item: (str(item["path"]), int(item["line"]), str(item["code"])))
    return {
        "schema": "ai-game-player/bug-check/v1",
        "passed": not findings,
        "scanned_files": scanned_files,
        "findings": findings,
    }


def _python_files(root: Path):
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", ".venv", "build", "dist", "__pycache__"} for part in path.parts):
            continue
        yield path


def _scan_file(path: Path) -> list[dict[str, object]]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        line = int(getattr(exc, "lineno", 1) or 1)
        return [_finding("BUG001", path, line, f"source cannot be parsed: {exc}")]
    findings: list[dict[str, object]] = []
    findings.extend(_duplicate_definitions(tree, path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(_mutable_defaults(node, path))
        elif isinstance(node, ast.Dict):
            findings.extend(_duplicate_dict_keys(node, path))
        elif isinstance(node, ast.Try):
            findings.extend(_exception_order(node, path))
            findings.extend(_finally_control_flow(node, path))
        elif isinstance(node, ast.Compare):
            findings.extend(_literal_identity(node, path))
    return findings


def _duplicate_definitions(tree: ast.AST, path: Path) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []

    def inspect_body(body: list[ast.stmt]) -> None:
        seen: dict[tuple[str, str], int] = {}
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = ("function", item.name)
            elif isinstance(item, ast.ClassDef):
                key = ("class", item.name)
            else:
                key = None
            if key is not None:
                if key in seen:
                    findings.append(
                        _finding(
                            "BUG201",
                            path,
                            item.lineno,
                            f"duplicate {key[0]} definition {item.name!r} overwrites line {seen[key]}",
                        )
                    )
                else:
                    seen[key] = item.lineno
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inspect_body(item.body)

    if isinstance(tree, ast.Module):
        inspect_body(tree.body)
    return findings


def _mutable_defaults(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    path: Path,
) -> list[dict[str, object]]:
    defaults = list(node.args.defaults) + [
        value for value in node.args.kw_defaults if value is not None
    ]
    findings = []
    for value in defaults:
        mutable_literal = isinstance(value, (ast.List, ast.Dict, ast.Set))
        mutable_factory_call = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"list", "dict", "set", "bytearray"}
        )
        if mutable_literal or mutable_factory_call:
            findings.append(
                _finding(
                    "BUG101",
                    path,
                    value.lineno,
                    f"mutable default argument in {node.name!r} is shared across calls",
                )
            )
    return findings


def _duplicate_dict_keys(node: ast.Dict, path: Path) -> list[dict[str, object]]:
    seen: dict[object, int] = {}
    findings = []
    for key in node.keys:
        if not isinstance(key, ast.Constant):
            continue
        value = key.value
        try:
            hash(value)
        except TypeError:
            continue
        if value in seen:
            findings.append(
                _finding(
                    "BUG103",
                    path,
                    key.lineno,
                    f"duplicate constant dict key {value!r} overwrites line {seen[value]}",
                )
            )
        else:
            seen[value] = key.lineno
    return findings


def _exception_order(node: ast.Try, path: Path) -> list[dict[str, object]]:
    findings = []
    broad_seen: tuple[str, int] | None = None
    for handler in node.handlers:
        if broad_seen is not None:
            findings.append(
                _finding(
                    "BUG104",
                    path,
                    handler.lineno,
                    f"exception handler is unreachable after broad {broad_seen[0]} handler on line {broad_seen[1]}",
                )
            )
            continue
        if handler.type is None:
            broad_seen = ("bare except", handler.lineno)
        elif isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
            broad_seen = (handler.type.id, handler.lineno)
    return findings


def _finally_control_flow(node: ast.Try, path: Path) -> list[dict[str, object]]:
    findings = []
    for item in node.finalbody:
        for child in ast.walk(item):
            if isinstance(child, ast.Return):
                findings.append(
                    _finding(
                        "BUG102",
                        path,
                        child.lineno,
                        "return in finally suppresses active exceptions",
                    )
                )
    return findings


def _literal_identity(node: ast.Compare, path: Path) -> list[dict[str, object]]:
    findings = []
    operands = [node.left, *node.comparators]
    for index, operator in enumerate(node.ops):
        if not isinstance(operator, (ast.Is, ast.IsNot)):
            continue
        pair = (operands[index], operands[index + 1])
        for operand in pair:
            if isinstance(operand, ast.Constant) and operand.value not in {None, True, False, Ellipsis}:
                findings.append(
                    _finding(
                        "BUG105",
                        path,
                        node.lineno,
                        "identity comparison with a non-singleton literal should use value comparison",
                    )
                )
                break
    return findings


def _finding(code: str, path: Path, line: int, message: str) -> dict[str, object]:
    return {
        "code": code,
        "path": path.as_posix(),
        "line": int(line),
        "message": message,
    }
