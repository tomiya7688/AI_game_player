"""Generate or verify code-derived project documentation."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

try:
    from .generate_class_diagram import render_class_diagram
    from .generate_sequence_diagram import render_sequence_diagram
except ImportError:  # Direct execution: python tools/generate_docs.py
    from generate_class_diagram import render_class_diagram
    from generate_sequence_diagram import render_sequence_diagram


@dataclass(frozen=True)
class GeneratedDocument:
    path: Path
    content: str


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as config_file:
        return json.load(config_file)


def generated_documents(root: Path, config: dict) -> list[GeneratedDocument]:
    paths = config["paths"]
    documents: list[GeneratedDocument] = []
    if config["docs"].get("class_diagram", False):
        documents.append(
            GeneratedDocument(
                root / paths["class_diagram"],
                render_class_diagram(root / paths["source"]),
            )
        )
    if config["docs"].get("sequence_diagram", False):
        sequence = config["sequence"]
        documents.append(
            GeneratedDocument(
                root / paths["sequence_diagram"],
                render_sequence_diagram(
                    root / sequence["source_file"],
                    sequence["class_name"],
                    sequence["method_name"],
                ),
            )
        )
    return documents


def write_documents(documents: list[GeneratedDocument]) -> None:
    for document in documents:
        document.path.parent.mkdir(parents=True, exist_ok=True)
        document.path.write_text(document.content, encoding="utf-8")


def stale_documents(documents: list[GeneratedDocument]) -> list[Path]:
    return [
        document.path
        for document in documents
        if not document.path.exists() or document.path.read_text(encoding="utf-8") != document.content
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify code-derived Mermaid documentation")
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    parser.add_argument("--config", type=Path, help="configuration path relative to the project root")
    args = parser.parse_args()

    root = project_root()
    config_path = args.config or root / "tools" / "completion_config.json"
    if not config_path.is_absolute():
        config_path = root / config_path
    documents = generated_documents(root, load_config(config_path))
    if args.check:
        stale = stale_documents(documents)
        if stale:
            for path in stale:
                print(f"stale generated document: {path.relative_to(root)}")
            return 1
        print("generated documentation is current")
        return 0

    write_documents(documents)
    print(f"generated {len(documents)} document(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())