import argparse
import ast
from pathlib import Path


# {
#   責務: [
#     render_sequence_diagram: 対象method内の直接呼び出しをsource順のMermaid図へ変換する
#   ]
#   処理: [
#     1: source fileをASTへ変換する
#     2: 対象classとmethodを探す
#     3: method直下の呼び出しを行位置順に並べる
#     4: 呼び出し順をMermaid sequence diagramへ出力する
#   ]
#   引数: [
#     source_file: 解析するPython source file
#     class_name: 呼び出しを抽出するclass名
#     method_name: 呼び出しを抽出するmethod名
#   ]
#   戻り値: [
#     str: Mermaid形式のsequence diagram
#   ]
#   エラー: [
#     ValueError: 指定したclassまたはmethodが存在しない
#   ]
# }
def render_sequence_diagram(source_file: Path, class_name: str, method_name: str) -> str:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    target = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if target is None:
        raise ValueError(f"class not found: {class_name}")
    method = next((node for node in target.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name), None)
    if method is None:
        raise ValueError(f"method not found: {class_name}.{method_name}")
    lines = ["sequenceDiagram", "    participant caller as Caller", f"    participant target as {class_name}"]
    call_nodes = sorted(
        (
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in call_nodes:
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
