"""Generate a deterministic, AST-based structural index of repository Python files."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path("config/repo_map.json")
DEFAULT_SCHEMA = Path("config/repo_map.schema.json")


class RepositoryMapError(ValueError):
    """Describe a repository-map input or source error without partial output."""


class RepositoryMapGenerator:
    """Build a source-structure index from configured Python files under one root."""

    def __init__(self, repository_root: Path, config_path: Path) -> None:
        self.repository_root = repository_root.resolve()
        self.config_path = self._resolve_repository_path(config_path)
        self.config = self._load_config()

    def _resolve_repository_path(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.repository_root / path
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.repository_root):
            raise RepositoryMapError(f"repository path escapes the root: {path}")
        return resolved

    def _load_config(self) -> dict[str, Any]:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RepositoryMapError(f"cannot read repo-map config {self.config_path}: {error}") from error
        if not isinstance(config, dict):
            raise RepositoryMapError("repo-map config must be a JSON object")
        required_keys = {"schema_version", "include", "exclude", "output"}
        if set(config) != required_keys:
            missing = sorted(required_keys - set(config))
            unknown = sorted(set(config) - required_keys)
            raise RepositoryMapError(f"repo-map config keys are invalid; missing={missing}, unknown={unknown}")
        if type(config["schema_version"]) is not int or config["schema_version"] != SCHEMA_VERSION:
            raise RepositoryMapError(f"repo-map config schema_version must be {SCHEMA_VERSION}")
        for key in ("include", "exclude"):
            patterns = config[key]
            if not isinstance(patterns, list) or any(not isinstance(item, str) or not item.strip() for item in patterns):
                raise RepositoryMapError(f"repo-map config {key} must be an array of non-empty path patterns")
            for pattern in patterns:
                path_pattern = PurePosixPath(pattern)
                if path_pattern.is_absolute() or ".." in path_pattern.parts or "\\" in pattern:
                    raise RepositoryMapError(f"repo-map config {key} pattern must be a relative POSIX path: {pattern}")
        output = config["output"]
        if not isinstance(output, str) or not output.strip():
            raise RepositoryMapError("repo-map config output must be a non-empty repository-relative path")
        output_path = PurePosixPath(output)
        if output_path.is_absolute() or ".." in output_path.parts or "\\" in output:
            raise RepositoryMapError("repo-map config output must be a repository-relative POSIX path")
        return config

    def _selected_source_paths(self) -> list[Path]:
        selected: set[Path] = set()
        for pattern in self.config["include"]:
            selected.update(self.repository_root.glob(pattern))
        root = self.repository_root
        safe_paths: list[Path] = []
        for path in sorted(selected):
            if not path.is_file() or path.suffix != ".py":
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise RepositoryMapError(f"Python source symlink escapes the repository: {path}")
            relative_path = PurePosixPath(path.relative_to(root).as_posix())
            if any(self._matches_path_pattern(relative_path, pattern) for pattern in self.config["exclude"]):
                continue
            safe_paths.append(path)
        if not safe_paths:
            raise RepositoryMapError("repo-map config selected no Python source files")
        return safe_paths

    def build(self) -> dict[str, Any]:
        source_files = self._selected_source_paths()
        module_paths: dict[str, Path] = {}
        for path in source_files:
            module_name = self._module_name(path)
            if module_name in module_paths:
                previous_path = module_paths[module_name].relative_to(self.repository_root).as_posix()
                current_path = path.relative_to(self.repository_root).as_posix()
                raise RepositoryMapError(
                    f"Python module name {module_name!r} is ambiguous between {previous_path} and {current_path}"
                )
            module_paths[module_name] = path
        module_names = set(module_paths)
        modules = [self._read_module(path, module_names) for path in source_files]
        return {
            "format": "kadoka-repository-map",
            "schema_version": SCHEMA_VERSION,
            "modules": modules,
        }

    def write(self, output_path: Path | None = None, *, check: bool = False) -> bool:
        destination = self.output_path(output_path)
        repository_map = self.build()
        content = self._serialize(repository_map)
        if check:
            if not destination.is_file() or destination.read_text(encoding="utf-8") != content:
                print(f"stale repository map: {destination.relative_to(self.repository_root).as_posix()}")
                return False
            print("repository map is current")
            return True
        self.publish(content, destination)
        print(f"generated {destination.relative_to(self.repository_root).as_posix()} ({len(repository_map['modules'])} modules)")
        return True

    def publish(self, content: str, output_path: Path | None = None) -> None:
        destination = self.output_path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, destination)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise RepositoryMapError(f"cannot publish repository map {destination}: {error}") from error

    @staticmethod
    def _matches_path_pattern(path: PurePosixPath, pattern: str) -> bool:
        path_parts = path.parts
        pattern_parts = PurePosixPath(pattern).parts

        @lru_cache(maxsize=None)
        def match(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            if pattern_parts[pattern_index] == "**":
                return match(path_index, pattern_index + 1) or (
                    path_index < len(path_parts) and match(path_index + 1, pattern_index)
                )
            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and match(path_index + 1, pattern_index + 1)
            )

        return match(0, 0)

    def output_path(self, override: Path | None = None) -> Path:
        configured_path = override if override is not None else Path(self.config["output"])
        return self._resolve_repository_path(configured_path)

    def render(self) -> str:
        return self._serialize(self.build())

    @staticmethod
    def _serialize(repository_map: dict[str, Any]) -> str:
        return json.dumps(repository_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _read_module(self, path: Path, module_names: set[str]) -> dict[str, Any]:
        relative_path = path.relative_to(self.repository_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RepositoryMapError(f"cannot read Python source {relative_path}: {error}") from error
        try:
            syntax_tree = ast.parse(source, filename=relative_path, feature_version=(3, 10))
        except SyntaxError as error:
            raise RepositoryMapError(
                f"cannot parse Python source {relative_path}:{error.lineno}:{error.offset}: {error.msg}"
            ) from error
        module_name = self._module_name(path)
        imports = self._collect_imports(
            syntax_tree,
            module_name,
            module_names,
            is_package_initializer=path.name == "__init__.py",
        )
        return {
            "module": module_name,
            "path": relative_path,
            "source_location": {
                "start_line": 1,
                "end_line": max(1, len(source.splitlines())),
            },
            "imports": imports,
            "dependencies": sorted({dependency for item in imports for dependency in item["local_dependencies"]}),
            "symbols": self._collect_symbols(syntax_tree, source),
        }

    def _module_name(self, path: Path) -> str:
        parts = list(path.relative_to(self.repository_root).parts)
        if parts and parts[0] == "src":
            parts.pop(0)
        if parts and parts[-1] == "__init__.py":
            parts.pop()
        elif parts and parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        return ".".join(parts)

    @staticmethod
    def _collect_imports(
        syntax_tree: ast.Module,
        current_module: str,
        module_names: set[str],
        *,
        is_package_initializer: bool,
    ) -> list[dict[str, Any]]:
        imports: list[dict[str, Any]] = []
        current_parts = current_module.split(".") if current_module else []
        package_parts = current_parts if is_package_initializer else current_parts[:-1]
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_dependencies = [alias.name] if alias.name in module_names else []
                    imports.append({
                        "kind": "import",
                        "module": alias.name,
                        "level": 0,
                        "names": [{"name": alias.name, "alias": alias.asname}],
                        "line": node.lineno,
                        "local_dependencies": local_dependencies,
                    })
            elif isinstance(node, ast.ImportFrom):
                base_parts: list[str] = []
                if node.level:
                    base_parts = package_parts
                    parent_count = node.level - 1
                    if parent_count >= len(package_parts):
                        base_parts = []
                    elif parent_count:
                        base_parts = package_parts[:-parent_count]
                resolved_module = ".".join([*base_parts, *(node.module.split(".") if node.module else [])])
                names = [{"name": alias.name, "alias": alias.asname} for alias in node.names]
                possible_dependencies = {resolved_module} if resolved_module and node.module else set()
                possible_dependencies.update(
                    f"{resolved_module}.{alias.name}" if resolved_module else alias.name
                    for alias in node.names
                    if alias.name != "*"
                )
                local_dependencies = sorted(possible_dependencies & module_names)
                imports.append({
                    "kind": "from",
                    "module": node.module,
                    "level": node.level,
                    "names": names,
                    "line": node.lineno,
                    "local_dependencies": local_dependencies,
                })
        return sorted(imports, key=lambda item: (item["line"], item["kind"], item["module"] or ""))

    @classmethod
    def _collect_symbols(cls, syntax_tree: ast.Module, source: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []

        def collect(
            statements: list[ast.stmt],
            parent_names: list[str],
            parent_is_class: bool,
        ) -> None:
            for node in cls._find_definitions(statements):
                qualified_name = ".".join([*parent_names, node.name])
                symbols.append(cls._symbol_record(node, source, qualified_name, parent_is_class))
                if isinstance(node, ast.ClassDef):
                    collect(node.body, [*parent_names, node.name], True)
                else:
                    collect(node.body, [*parent_names, node.name], False)

        collect(syntax_tree.body, [], False)
        return sorted(symbols, key=lambda item: (item["source_location"]["start_line"], item["qualified_name"]))

    @classmethod
    def _find_definitions(cls, statements: list[ast.stmt]) -> list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]:
        definitions: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = []
        pending: list[ast.AST] = list(reversed(statements))
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions.append(node)
            else:
                pending.extend(reversed(list(ast.iter_child_nodes(node))))
        return definitions

    @classmethod
    def _symbol_record(
        cls,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        source: str,
        qualified_name: str,
        parent_is_class: bool,
    ) -> dict[str, Any]:
        if isinstance(node, ast.ClassDef):
            kind = "class"
            bases = [ast.unparse(base) for base in node.bases]
            data_model = {
                "dataclass": any(cls._terminal_name(decorator) == "dataclass" for decorator in node.decorator_list),
                "enum": any(cls._terminal_name(base) in {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"} for base in node.bases),
                "protocol": any(cls._terminal_name(base) == "Protocol" for base in node.bases),
            }
            signature = None
            parameters = []
            return_annotation = None
        else:
            is_method = parent_is_class
            kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) and is_method else "method" if is_method else "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            signature, parameters, return_annotation = cls._function_signature(node)
            bases = []
            data_model = None
        record: dict[str, Any] = {
            "name": node.name,
            "qualified_name": qualified_name,
            "kind": kind,
            "source_location": {
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "column": node.col_offset,
            },
            "decorators": [ast.unparse(decorator) for decorator in node.decorator_list],
            "signature": signature,
            "parameters": parameters,
            "return_annotation": return_annotation,
            "bases": bases,
            "data_model": data_model,
        }
        if source and node.end_lineno is None:
            record["source_location"]["end_line"] = node.lineno
        return record

    @staticmethod
    def _terminal_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Subscript):
            return RepositoryMapGenerator._terminal_name(node.value)
        if isinstance(node, ast.Call):
            return RepositoryMapGenerator._terminal_name(node.func)
        return ""

    @classmethod
    def _function_signature(
        cls,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        arguments = node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        default_start = len(positional) - len(arguments.defaults)
        parameters: list[dict[str, Any]] = []
        rendered: list[str] = []
        for index, argument in enumerate(positional):
            defaulted = index >= default_start
            kind = "positional_only" if index < len(arguments.posonlyargs) else "positional_or_keyword"
            parameters.append(cls._parameter_record(argument, kind, defaulted))
            rendered.append(cls._render_parameter(argument, defaulted))
            if arguments.posonlyargs and index + 1 == len(arguments.posonlyargs):
                rendered.append("/")
        if arguments.vararg is not None:
            parameters.append(cls._parameter_record(arguments.vararg, "var_positional", False))
            rendered.append(f"*{cls._render_parameter(arguments.vararg, False)}")
        elif arguments.kwonlyargs:
            rendered.append("*")
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
            defaulted = default is not None
            parameters.append(cls._parameter_record(argument, "keyword_only", defaulted))
            rendered.append(cls._render_parameter(argument, defaulted))
        if arguments.kwarg is not None:
            parameters.append(cls._parameter_record(arguments.kwarg, "var_keyword", False))
            rendered.append(f"**{cls._render_parameter(arguments.kwarg, False)}")
        return_annotation = ast.unparse(node.returns) if node.returns is not None else None
        suffix = f" -> {return_annotation}" if return_annotation else ""
        return f"({', '.join(rendered)}){suffix}", parameters, return_annotation

    @classmethod
    def _parameter_record(cls, argument: ast.arg, kind: str, has_default: bool) -> dict[str, Any]:
        return {
            "name": argument.arg,
            "kind": kind,
            "annotation": ast.unparse(argument.annotation) if argument.annotation is not None else None,
            "has_default": has_default,
        }

    @staticmethod
    def _render_parameter(argument: ast.arg, has_default: bool) -> str:
        rendered = argument.arg
        if argument.annotation is not None:
            rendered += f": {ast.unparse(argument.annotation)}"
        if has_default:
            rendered += " = …"
        return rendered


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="repository root; defaults to the parent of tools/")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="repository-relative JSON include/exclude config")
    parser.add_argument("--output", type=Path, help="repository-relative output path overriding config")
    parser.add_argument("--check", action="store_true", help="fail when the generated map differs from the saved file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    root = arguments.root.resolve() if arguments.root else Path(__file__).resolve().parents[1]
    try:
        generator = RepositoryMapGenerator(root, arguments.config)
        return 0 if generator.write(arguments.output, check=arguments.check) else 1
    except (OSError, RepositoryMapError) as error:
        print(f"repository map generation failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
