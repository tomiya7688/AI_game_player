"""Generate a table of contents for the Markdown documents under doc/ and the main root documents."""

from __future__ import annotations

import re
from pathlib import Path

HEADING_PATTERN = re.compile(r"^#\s+(?P<title>.+?)\s*$")
FRONT_KEYS = ("Parent", "Spec-Status", "Last-Checked")
FRONT_PATTERN = re.compile(r"^(?P<key>[A-Za-z-]+):\s*(?P<value>.+?)\s*$")
SUMMARY_LIMIT = 100
ROOT_DOCS = ("README.md", "AGENTS.md", "AI_CONTEXT.md", "key_info.md")
SKIP_PARTS = {"generated", "__pycache__"}
SENTENCE_END = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+")
LIST_PREFIXES = ("#", "|", "-", "*", "+", "<", "!")
FENCE_MARKERS = ("```", "~~~")
LINK_ONLY_PATTERN = re.compile(r"^(?:\s*(?:\[[^\]]*\]\([^)]*\)|[|・/｜-])\s*)+$")


def _link_destination(relative: str) -> str:
    """Escape only the characters that break a Markdown link destination (space and parentheses)."""
    return relative.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _plain(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*>#]", "", text)
    return " ".join(text.split()).replace("|", "/")


def _first_sentence(text: str) -> str:
    sentence = SENTENCE_END.split(text, maxsplit=1)[0]
    if len(sentence) <= SUMMARY_LIMIT:
        return sentence
    return sentence[: SUMMARY_LIMIT - 1] + "…"


def _is_prose(line: str) -> bool:
    """Return True for a paragraph line (not a list, table, heading, HTML or navigation-only links)."""
    if line.startswith(LIST_PREFIXES) or re.match(r"^\d+[.)]\s", line):
        return False
    return not LINK_ONLY_PATTERN.match(line)


def summarize_document(text: str) -> tuple[str, str, dict[str, str]]:
    """Return (title, one-line summary, front-matter keys) of a Markdown text."""
    title = ""
    paragraph: list[str] = []
    front: dict[str, str] = {}
    fence = ""
    for line in text.splitlines():
        marker = line.lstrip()[:3]
        if marker in FENCE_MARKERS:
            fence = "" if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        heading = HEADING_PATTERN.match(line)
        if heading and not title:
            title = _plain(heading.group("title"))
            continue
        match = FRONT_PATTERN.match(line)
        if match and match.group("key") in FRONT_KEYS and not title:
            front[match.group("key")] = match.group("value")
            continue
        if title and _is_prose(line):
            paragraph.append(line.strip())
        elif paragraph:
            break
    summary = _first_sentence(_plain(" ".join(paragraph))) if paragraph else "-"
    return title or "(題名なし)", summary or "-", front


def _document_paths(root: Path) -> list[Path]:
    candidates = [root / name for name in ROOT_DOCS]
    candidates += sorted(
        p
        for p in (root / "doc").rglob("*.md")
        if not SKIP_PARTS & set(p.relative_to(root).parts)
    )
    resolved_root = root.resolve()
    # symlink経由でリポジトリ外のファイルを索引へ取り込まない。
    return [
        p
        for p in candidates
        if p.is_file()
        and not p.is_symlink()
        and p.resolve().is_relative_to(resolved_root)
    ]


def render_doc_index(root: Path) -> str:
    """Render the Markdown table of contents for the indexed documents."""
    lines = [
        "<!-- 自動生成: tools/generate_doc_index.py。手で編集しない。 -->",
        "# 文書目次",
        "",
        "文書を探す前にここを読みます。`doc/`配下の全文書と、主要なルート文書（README、AGENTS、AI_CONTEXT、key_info）の題名と先頭の1文を載せます。内容の正本は各文書です。",
        "",
        "| 文書 | 題名 | 先頭の要約 | 親Issue | 確認日 |",
        "|---|---|---|---|---|",
    ]
    for path in _document_paths(root):
        title, summary, front = summarize_document(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root).as_posix()
        lines.append(
            f"| [`{relative}`](../{_link_destination(relative)}) | {title} | {summary} | {front.get('Parent', '-')} | {front.get('Last-Checked', '-')} |"
        )
    lines.append("")
    return "\n".join(lines)
