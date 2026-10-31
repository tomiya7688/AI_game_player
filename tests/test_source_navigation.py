"""Lightweight local-link and Mermaid-envelope checks for public guide pages.

This extends the established README/source navigation test; it does not replace
code-derived documentation generation or claim to validate Mermaid semantics.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


LINK_PATTERN = re.compile(r"!?\[[^\]]+\]\(([^)]+)\)")
MERMAID_START = re.compile(
    r"^(?:flowchart\s+(?:TD|TB|BT|LR|RL)|graph\s+(?:TD|TB|BT|LR|RL)|sequenceDiagram)\b"
)
# Find a definition such as A[日本語] or B{条件}. Edges reusing an ID are not definitions.
NODE_DEFINITION = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_-]*)\s*(?=\[|\{)")
FENCE = "\x60" * 3


def _pages_to_check(root: Path) -> list[Path]:
    """Scan only the public entrypoints and area guides, not unrelated developer docs."""
    relative = [
        Path("README.md"),
        Path("src/ai_game_player/README.md"),
        Path("doc/architecture/README.md"),
        Path("doc/architecture/flow_for_readers.md"),
        Path("doc/architecture/runtime_map_for_readers.md"),
    ]
    for directory in (Path("doc/guide"), Path("doc/models")):
        full = root / directory
        if full.is_dir():
            relative.extend(path.relative_to(root) for path in full.rglob("*.md"))
    return sorted({root / item for item in relative if (root / item).is_file()})


def _relative_link_errors(page: Path, root: Path, text: str) -> list[str]:
    """Check local Markdown destinations and reject links escaping the repository."""
    repository_root = root.resolve()
    errors: list[str] = []
    for link in LINK_PATTERN.findall(text):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (page.parent / unquote(parsed.path)).resolve()
        if not target.is_relative_to(repository_root):
            errors.append(f"{link}: link escapes repository root")
        elif not target.exists():
            errors.append(f"{link}: missing path")
    return errors


def _mermaid_errors(text: str) -> list[str]:
    """Catch unclosed fences, unsupported diagram headers and repeated flow-node declarations.

    This is a deliberately shallow, offline check. Rendering and meaning are reviewed
    separately; for example it does not prove arrows show the actual call order.
    """
    errors: list[str] = []
    open_language: str | None = None
    lines: list[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            if open_language is None:
                open_language = stripped[len(FENCE):].strip()
                lines = []
            else:
                if stripped != FENCE:
                    errors.append(f"line {line_no}: malformed closing code fence")
                if open_language == "mermaid":
                    diagram = "\n".join(lines).strip()
                    diagram_lines = [line.strip() for line in lines if line.strip()]
                    if not diagram_lines or MERMAID_START.match(diagram_lines[0]) is None:
                        errors.append(f"line {line_no}: unknown Mermaid opening directive")
                    if diagram_lines and diagram_lines[0].startswith(("flowchart", "graph")):
                        definitions = [match.group(1) for match in NODE_DEFINITION.finditer(diagram)]
                        repeated = sorted({value for value in definitions if definitions.count(value) > 1})
                        if repeated:
                            errors.append(f"line {line_no}: repeated Mermaid node definition: {', '.join(repeated)}")
                open_language = None
                lines = []
        elif open_language is not None:
            lines.append(line)
    if open_language is not None:
        errors.append(f"unclosed code fence: {open_language or 'plain text'}")
    return errors


class SourceNavigationTests(unittest.TestCase):
    def test_relative_markdown_links_point_to_existing_paths(self):
        root = Path(__file__).resolve().parents[1]
        pages = _pages_to_check(root)
        self.assertIn(root / "README.md", pages)
        self.assertIn(root / "src/ai_game_player/README.md", pages)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            with self.subTest(page=page.relative_to(root)):
                errors = _relative_link_errors(page, root, text)
                self.assertEqual(errors, [], "Invalid local Markdown links: " + "; ".join(errors))

    def test_public_guide_diagram_fences(self):
        root = Path(__file__).resolve().parents[1]
        for page in _pages_to_check(root):
            if page == root / "README.md" or page == root / "src/ai_game_player/README.md":
                continue
            with self.subTest(page=page.relative_to(root)):
                errors = _mermaid_errors(page.read_text(encoding="utf-8"))
                self.assertEqual(errors, [], "Invalid Mermaid block: " + "; ".join(errors))

    def test_invalid_local_links_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            page = root / "guide.md"
            page.write_text("[missing](missing.md)\n[escape](../secret.md)", encoding="utf-8")
            errors = _relative_link_errors(page, root, page.read_text(encoding="utf-8"))
            self.assertEqual(len(errors), 2)
            self.assertTrue(any("missing path" in error for error in errors))
            self.assertTrue(any("escapes repository" in error for error in errors))

    def test_invalid_mermaid_fences_and_nodes_fail(self):
        self.assertTrue(_mermaid_errors("~~~markdown\n" + FENCE + "mermaid\nflowchart TD"))
        self.assertTrue(_mermaid_errors(FENCE + "mermaid\nwrongDiagram\n" + FENCE))
        duplicated = FENCE + "mermaid\nflowchart TD\nA[一つ]\nA[二つ]\n" + FENCE
        self.assertTrue(any("repeated" in error for error in _mermaid_errors(duplicated)))
        valid = FENCE + "mermaid\nflowchart LR\nA[最初] --> B[次]\nB --> A\n" + FENCE
        self.assertEqual(_mermaid_errors(valid), [])


if __name__ == "__main__":
    unittest.main()
