import argparse
import ast
from pathlib import Path


def render_sequence_diagram(source_file: Path, class_name: str, method_name: str) -> str:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    target = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if target is None:
        raise ValueError(f"class not found: {class_name}")
    method = next((node for node in target.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name), None)
    if method is None:
        raise ValueError(f"method not found: {class_name}.{method_name}")
    lines = ["sequenceDiagram", f"    participant caller as Caller", f"    participant target as {class_name}"]
    for node in ast.walk(method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = ast.unparse(node.func.value)
        participant = receiver.replace("self", class_name)
        lines.append(f"    caller->>target: {participant}.{node.func.attr}()")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Mermaid sequence diagram from a Python method")
    parser.add_argument("source_file", type=Path)
    parser.add_argument("class_name")
    parser.add_argument("method_name")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_sequence_diagram(args.source_file, args.class_name, args.method_name), encoding="utf-8")


if __name__ == "__main__":
    main()