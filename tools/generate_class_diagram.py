import argparse
import ast
from pathlib import Path


def render_class_diagram(source_root: Path) -> str:
    classes: list[tuple[str, list[str], list[str]]] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(base) for base in node.bases]
            methods = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append((node.name, bases, methods))
    lines = ["classDiagram"]
    for name, bases, methods in classes:
        for base in bases:
            lines.append(f"    {base} <|-- {name}")
        lines.append(f"    class {name} {{")
        for method in methods:
            lines.append(f"        +{method}()")
        lines.append("    }")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Mermaid class diagram from Python sources")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_class_diagram(args.source_root), encoding="utf-8")


if __name__ == "__main__":
    main()