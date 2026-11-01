"""Offline navigation and fenced-diagram checks, not a complete Markdown renderer."""

from __future__ import annotations

import html
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED_PAGES = (
    "README.md",
    "src/ai_game_player/README.md",
    "doc/architecture/runtime_layers.md",
)
FENCE_START = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)
LINK_START = re.compile(r"!?\[(?:\\.|[^\]\\])*\]\(")
REFERENCE_START = re.compile(r"^ {0,3}\[(?:\\.|[^\]\\])+\]:[ \t]*", re.MULTILINE)
MERMAID_START = re.compile(
    r"^(?:flowchart\s+(?:TD|TB|BT|LR|RL)|graph\s+(?:TD|TB|BT|LR|RL)|"
    r"sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|"
    r"gantt|pie|mindmap|timeline|gitGraph|requirementDiagram|quadrantChart|"
    r"sankey-beta|xychart-beta|block-beta|packet-beta|architecture-beta|"
    r"kanban|radar-beta|treemap-beta|C4Context|C4Container|C4Component|"
    r"C4Dynamic|C4Deployment)\b"
)
NODE_DEFINITION = re.compile(r"(?<![\w-])([A-Za-z_][A-Za-z0-9_-]*)\s*(?=[\[({])")


def _pages_to_check(root: Path) -> list[Path]:
    """Include required existing pages and progressively merged reader guides."""
    pages = {root / name for name in REQUIRED_PAGES}
    optional = (
        "doc/architecture/README.md",
        "doc/architecture/flow_for_readers.md",
        "doc/architecture/runtime_map_for_readers.md",
        "doc/contributor/documentation_update_guide.md",
    )
    pages.update(root / name for name in optional if (root / name).is_file())
    for name in ("doc/guide", "doc/models"):
        pages.update((root / name).rglob("*.md"))
    return sorted(pages)


def _split_fences(text: str) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Separate prose and fences, preserving the opening character and length."""
    prose: list[str] = []
    blocks: list[tuple[str, str]] = []
    errors: list[str] = []
    marker = ""
    language = ""
    body: list[str] = []
    for line in text.splitlines():
        match = FENCE_START.match(line)
        if marker:
            if match and match[1][0] == marker[0] and len(match[1]) >= len(marker) and not match[2].strip():
                blocks.append((language, "\n".join(body)))
                marker = ""
                body = []
            else:
                body.append(line)
            prose.append("")
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            marker = match[1]
            info = match[2].strip().split()
            language = info[0] if info else ""
            body = []
            prose.append("")
        else:
            prose.append(line)
    if marker:
        errors.append(f"unclosed code fence: {marker} ({language or 'plain text'})")
    return "\n".join(prose), blocks, errors


def _read_destination(text: str, start: int) -> str | None:
    """Read an inline/reference destination, excluding an optional link title."""
    pos = start
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    angled = pos < len(text) and text[pos] == "<"
    if angled:
        pos += 1
    value: list[str] = []
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char == "\\" and pos + 1 < len(text):
            value.append(text[pos + 1])
            pos += 2
            continue
        if angled:
            if char == ">":
                return html.unescape("".join(value))
            if char in "\r\n":
                return None
        elif char.isspace() or (char == ")" and depth == 0):
            return html.unescape("".join(value)) if depth == 0 else None
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        value.append(char)
        pos += 1
    if angled or depth:
        return None
    return html.unescape("".join(value))


def _relative_link_errors(page: Path, root: Path, text: str) -> list[str]:
    """Validate local inline and reference destinations outside fenced/inline code.

    Raw HTML links and heading anchors are intentionally outside this small check.
    Reference definitions are checked even if no reference currently uses them.
    """
    prose, _, _ = _split_fences(text)
    prose = INLINE_CODE.sub("", prose)
    errors: list[str] = []
    for pattern in (LINK_START, REFERENCE_START):
        for match in pattern.finditer(prose):
            link = _read_destination(prose, match.end())
            if link is None:
                continue
            try:
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (page.parent / unquote(parsed.path)).resolve()
                if not target.is_relative_to(root.resolve()):
                    errors.append(f"{link}: link escapes repository root")
                elif not target.exists():
                    errors.append(f"{link}: missing path")
            except (OSError, ValueError) as error:
                errors.append(f"{link}: invalid local link ({type(error).__name__})")
    return errors


def _mermaid_errors(text: str) -> list[str]:
    """Check fences, common diagram headers and repeated flow-node declarations.

    A supported header is not proof that Mermaid can render the complete diagram.
    Rendering, diagram meaning and new Mermaid syntax still need separate review.
    """
    _, blocks, errors = _split_fences(text)
    for language, diagram in blocks:
        if language != "mermaid":
            continue
        lines = [line.strip() for line in diagram.splitlines() if line.strip() and not line.lstrip().startswith("%%")]
        if lines and lines[0] == "---":
            try:
                lines = lines[lines.index("---", 1) + 1:]
            except ValueError:
                errors.append("unclosed Mermaid front matter")
                continue
        if not lines or MERMAID_START.match(lines[0]) is None:
            errors.append("unknown Mermaid opening directive")
            continue
        if lines[0].startswith(("flowchart", "graph")):
            # Quoted labels, edge labels and comments are not node declarations.
            clean = re.sub(r'"(?:\\.|[^"\\])*"', '\"\"', "\n".join(lines))
            clean = re.sub(r"%%[^\n]*|\|[^|\n]*\|", "", clean)
            counts = Counter(NODE_DEFINITION.findall(clean))
            repeated = sorted(name for name, count in counts.items() if count > 1)
            if repeated:
                errors.append("repeated Mermaid node definition: " + ", ".join(repeated))
    return errors


class SourceNavigationTests(unittest.TestCase):
    def test_relative_markdown_links_point_to_existing_paths(self):
        root = Path(__file__).resolve().parents[1]
        for page in _pages_to_check(root):
            with self.subTest(page=page.relative_to(root)):
                self.assertTrue(page.is_file(), f"Required documentation missing: {page}")
                self.assertEqual(_relative_link_errors(page, root, page.read_text(encoding="utf-8")), [])

    def test_public_guide_diagram_fences(self):
        root = Path(__file__).resolve().parents[1]
        for page in _pages_to_check(root):
            with self.subTest(page=page.relative_to(root)):
                self.assertTrue(page.is_file(), f"Required documentation missing: {page}")
                self.assertEqual(_mermaid_errors(page.read_text(encoding="utf-8")), [])


class NavigationParserTests(unittest.TestCase):
    def test_titles_spaces_parentheses_and_reference_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target (one).md").touch()
            page = root / "guide.md"
            valid = '\n'.join([
                '[title](target%20(one).md "説明")',
                "[title](<target (one).md> '説明')",
                '[reference][r]',
                '[r]: <target (one).md> "説明"',
            ])
            self.assertEqual(_relative_link_errors(page, root, valid), [])
            broken = '[missing](missing.md "title")\n[x][r]\n[r]: absent.md\n[out](../outside.md)'
            errors = _relative_link_errors(page, root, broken)
            self.assertEqual(len(errors), 3)
            self.assertTrue(any("escapes repository" in error for error in errors))

    def test_code_examples_are_not_links(self):
        text = "`[x](missing.md)`\n``[x](missing.md)``\n~~~md\n[x](missing.md)\n~~~"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_relative_link_errors(root / "guide.md", root, text), [])

    def test_long_fences_protect_inner_examples(self):
        text = "````markdown\n```mermaid\nnotADiagram\n```\n[x](missing.md)\n````"
        self.assertEqual(_mermaid_errors(text), [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_relative_link_errors(root / "guide.md", root, text), [])

    def test_unclosed_tilde_fence_has_specific_error(self):
        self.assertEqual(_mermaid_errors("~~~mermaid\nflowchart TD"), ["unclosed code fence: ~~~ (mermaid)"])

    def test_wrong_or_short_closer_does_not_close(self):
        for closer in ("~~~", "``"):
            with self.subTest(closer=closer):
                self.assertTrue(any("unclosed" in error for error in _mermaid_errors("```mermaid\nflowchart TD\n" + closer)))
        self.assertEqual(_mermaid_errors("~~~mermaid\nflowchart TD\n~~~~"), [])

    def test_invalid_diagram_inside_tilde_fence(self):
        self.assertEqual(_mermaid_errors("~~~mermaid\nwrongDiagram\n~~~"), ["unknown Mermaid opening directive"])

    def test_class_state_and_sequence_diagrams(self):
        for directive in ("classDiagram", "stateDiagram-v2", "sequenceDiagram", "erDiagram", "pie"):
            with self.subTest(directive=directive):
                self.assertEqual(_mermaid_errors("```mermaid\n" + directive + "\n```"), [])

    def test_parenthesis_and_other_duplicate_nodes(self):
        for shape in ("[一つ]", "{条件}", "(処理)", "((丸))"):
            with self.subTest(shape=shape):
                errors = _mermaid_errors("```mermaid\nflowchart TD\nA" + shape + "\nA" + shape + "\n```")
                self.assertTrue(any("repeated" in error for error in errors))

    def test_quoted_labels_comments_and_reused_ids_are_not_duplicates(self):
        diagram = 'flowchart TD\nA["B[説明]"] --> B(処理)\nB --> A\n%% A[例]'
        self.assertEqual(_mermaid_errors("```mermaid\n" + diagram + "\n```"), [])

    def test_comments_and_front_matter_before_diagram(self):
        text = "~~~mermaid\n---\ntitle: 説明\n---\n%% comment\nflowchart TD\nA[最初] --> B[次]\n~~~"
        self.assertEqual(_mermaid_errors(text), [])

    def test_required_runtime_page_does_not_silently_disappear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIn(root / "doc/architecture/runtime_layers.md", _pages_to_check(root))

    def test_optional_update_guide_is_scanned_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "doc/contributor/documentation_update_guide.md"
            page.parent.mkdir(parents=True)
            page.touch()
            self.assertIn(page, _pages_to_check(root))


if __name__ == "__main__":
    unittest.main()
