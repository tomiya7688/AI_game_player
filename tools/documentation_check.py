"""Offline Markdown-link and Mermaid-envelope checks for reader documentation.

The scanner understands fenced/code-span boundaries and link destinations; it
does not replace a Markdown renderer or Mermaid's complete syntax parser.
"""

from collections import Counter
import html
from pathlib import Path
import re
import string
from urllib.parse import unquote, urlsplit


FENCE = "`" * 3
REQUIRED_PAGES = (
    "README.md",
    "src/ai_game_player/README.md",
    "doc/architecture/runtime_layers.md",
)
OPTIONAL_PAGES = (
    "doc/architecture/README.md",
    "doc/architecture/flow_for_readers.md",
    "doc/architecture/runtime_map_for_readers.md",
    "doc/architecture/qt_rust_migration.md",
    "doc/contributor/documentation_update_guide.md",
)
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(.*)$", re.MULTILINE
)
NODE_DEFINITION = re.compile(r"(?<![\w-])([A-Za-z_][\w-]*|[0-9]+)[ \t]*(?=[\[({])")
MERMAID_KINDS = frozenset(
    {
        "flowchart",
        "graph",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "journey",
        "gantt",
        "pie",
        "quadrantChart",
        "requirementDiagram",
        "gitGraph",
        "C4Context",
        "C4Container",
        "C4Component",
        "C4Dynamic",
        "C4Deployment",
        "mindmap",
        "timeline",
        "zenuml",
        "sankey-beta",
        "xychart-beta",
        "block-beta",
        "packet-beta",
        "kanban",
        "architecture-beta",
        "radar-beta",
        "treemap-beta",
    }
)


def pages_to_check(root: Path) -> list[Path]:
    """Require established entrypoints and add guide pages as their PRs land."""
    pages = {root / relative for relative in REQUIRED_PAGES}
    pages.update(
        root / relative for relative in OPTIONAL_PAGES if (root / relative).is_file()
    )
    for directory in ("doc/guide", "doc/models"):
        if (root / directory).is_dir():
            pages.update((root / directory).rglob("*.md"))
    return sorted(pages)


def _mask(text: str) -> str:
    return "".join("\n" if character == "\n" else " " for character in text)


def _fenced_blocks(text: str) -> tuple[str, list[tuple[str, str, int]], list[str]]:
    """Return prose, closed blocks and authoring errors with CommonMark fence rules."""
    prose: list[str] = []
    blocks: list[tuple[str, str, int]] = []
    errors: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    language = ""
    opened_at = 0
    content: list[str] = []
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        if fence_character is not None:
            prose.append(_mask(line))
            if re.fullmatch(
                r" {0,3}"
                + re.escape(fence_character)
                + "{"
                + str(fence_length)
                + r",}[ \t]*",
                body,
            ):
                blocks.append((language, "".join(content), line_number))
                fence_character = None
                content = []
            else:
                content.append(line)
            continue
        match = FENCE_OPEN.fullmatch(body)
        if match is None or (match[1][0] == "`" and "`" in match[2]):
            prose.append(line)
            continue
        fence_character, fence_length = match[1][0], len(match[1])
        language = match[2].strip().split()[0] if match[2].strip() else ""
        opened_at = line_number
        prose.append(_mask(line))
    if fence_character is not None:
        errors.append(
            f"line {opened_at}: unclosed code fence: {language or 'plain text'}"
        )
    return "".join(prose), blocks, errors


def _without_code_spans(text: str) -> str:
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            pieces.append(text[index : index + 2])
            index += 2
            continue
        if text[index] != "`":
            pieces.append(text[index])
            index += 1
            continue
        end = index
        while end < len(text) and text[end] == "`":
            end += 1
        delimiter = text[index:end]
        closing = re.search(r"(?<!`)" + re.escape(delimiter) + r"(?!`)", text[end:])
        if closing is None:
            pieces.append(delimiter)
            index = end
        else:
            span_end = end + closing.end()
            pieces.append(_mask(text[index:span_end]))
            index = span_end
    return "".join(pieces)


def _unescape(destination: str) -> str:
    return html.unescape(
        re.sub(r"\\([" + re.escape(string.punctuation) + r"])", r"\1", destination)
    )


def _destination(text: str, index: int) -> tuple[str, int] | None:
    """Read one angle-enclosed or balanced bare Markdown destination, without its title."""
    if index < len(text) and text[index] == "<":
        cursor = index + 1
        while cursor < len(text):
            if text[cursor] == "\\" and cursor + 1 < len(text):
                cursor += 2
            elif text[cursor] == ">":
                return _unescape(text[index + 1 : cursor]), cursor + 1
            elif text[cursor] in "\r\n<":
                return None
            else:
                cursor += 1
        return None
    cursor, depth = index, 0
    while cursor < len(text):
        character = text[cursor]
        if character == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if character.isspace():
            break
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                break
            depth -= 1
        cursor += 1
    if depth:
        return None
    return _unescape(text[index:cursor]), cursor


def _inline_destination(text: str, index: int) -> tuple[str, int] | None:
    while index < len(text) and text[index].isspace():
        index += 1
    parsed = _destination(text, index)
    if parsed is None:
        return None
    destination, cursor = parsed
    spaced = cursor < len(text) and text[cursor].isspace()
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] == ")":
        return destination, cursor + 1
    if not spaced or cursor >= len(text) or text[cursor] not in "\"'(":
        return None
    closer = ")" if text[cursor] == "(" else text[cursor]
    cursor += 1
    while cursor < len(text):
        if text[cursor] == "\\" and cursor + 1 < len(text):
            cursor += 2
        elif text[cursor] == closer:
            cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            return (
                (destination, cursor + 1)
                if cursor < len(text) and text[cursor] == ")"
                else None
            )
        else:
            cursor += 1
    return None


def _link_destinations(text: str) -> list[str]:
    prose, _, _ = _fenced_blocks(text)
    prose = _without_code_spans(prose)
    prose = re.sub(r"<!--.*?-->", lambda match: _mask(match[0]), prose, flags=re.DOTALL)
    destinations: list[str] = []
    # Definitions are checked even before they are reused, so broken reference
    # destinations do not remain dormant until another page links to them.
    for definition in REFERENCE_DEFINITION.finditer(prose):
        destination = _destination(definition[1], 0)
        if destination is not None:
            destinations.append(destination[0])
    prose = REFERENCE_DEFINITION.sub(lambda match: _mask(match[0]), prose)
    cursor = 0
    while cursor < len(prose):
        if prose[cursor] == "\\":
            cursor += 2
            continue
        if prose[cursor] != "[":
            cursor += 1
            continue
        depth, end = 1, cursor + 1
        while end < len(prose) and depth:
            if prose[end] == "\\":
                end += 2
                continue
            if prose[end] == "[":
                depth += 1
            elif prose[end] == "]":
                depth -= 1
            end += 1
        if depth == 0 and end < len(prose) and prose[end] == "(":
            destination = _inline_destination(prose, end + 1)
            if destination is not None:
                destinations.append(destination[0])
                cursor = destination[1]
                continue
        cursor = end
    return destinations


def relative_link_errors(page: Path, root: Path, text: str) -> list[str]:
    repository_root = root.resolve()
    errors: list[str] = []
    for link in _link_destinations(text):
        try:
            parsed = urlsplit(link)
        except ValueError:
            errors.append(f"{link}: invalid link destination")
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (page.parent / unquote(parsed.path)).resolve()
        if not target.is_relative_to(repository_root):
            errors.append(f"{link}: link escapes repository root")
        elif not target.exists():
            errors.append(f"{link}: missing path")
    return errors


def _mermaid_code(text: str) -> str:
    """Mask quoted labels and comments before looking for declarations."""
    result: list[str] = []
    quoted: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if quoted is not None:
            if character == "\\" and index + 1 < len(text):
                result.append("  ")
                index += 2
                continue
            if character == quoted:
                quoted = None
            result.append("\n" if character == "\n" else " ")
        elif text.startswith("%%", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            result.append(" " * (end - index))
            index = end
            continue
        elif character == "|":
            end = text.find("|", index + 1)
            newline = text.find("\n", index + 1)
            if end >= 0 and (newline < 0 or end < newline):
                result.append(" " * (end + 1 - index))
                index = end + 1
                continue
            result.append(character)
        elif character == '"' or (
            character == "'" and (index == 0 or not text[index - 1].isalnum())
        ):
            quoted = character
            result.append(" ")
        else:
            result.append(character)
        index += 1
    return "".join(result)


def _node_definitions(code: str) -> list[str]:
    definitions: list[str] = []
    cursor = 0
    while match := NODE_DEFINITION.search(code, cursor):
        definitions.append(match[1])
        cursor = match.end()
        stack = [{"[": "]", "(": ")", "{": "}"}[code[cursor]]]
        cursor += 1
        while cursor < len(code) and stack:
            character = code[cursor]
            if character in "[({":
                stack.append({"[": "]", "(": ")", "{": "}"}[character])
            elif character == stack[-1]:
                stack.pop()
            cursor += 1
    return definitions


def mermaid_errors(text: str) -> list[str]:
    _, blocks, errors = _fenced_blocks(text)
    for language, diagram, line_number in blocks:
        if language != "mermaid":
            continue
        code = _mermaid_code(diagram)
        lines = [line.strip() for line in code.splitlines() if line.strip()]
        if lines and lines[0] == "---" and "---" in lines[1:]:
            lines = lines[lines.index("---", 1) + 1 :]
        kind = lines[0].split()[0] if lines else ""
        if kind not in MERMAID_KINDS:
            errors.append(f"line {line_number}: unknown Mermaid opening directive")
        if kind in {"flowchart", "graph"}:
            repeated = sorted(
                identifier
                for identifier, count in Counter(_node_definitions(code)).items()
                if count > 1
            )
            if repeated:
                errors.append(
                    f"line {line_number}: repeated Mermaid node definition: {', '.join(repeated)}"
                )
    return errors
