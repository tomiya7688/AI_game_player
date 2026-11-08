"""Generate a table of contents for every Markdown document under doc/ and the root."""

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


def _plain(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*>#]", "", text)
    return " ".join(text.split()).replace("|", "/")


def _first_sentence(text: str) -> str:
    sentence = SENTENCE_END.split(text, maxsplit=1)[0]
    if len(sentence) <= SUMMARY_LIMIT:
        return sentence
    return sentence[: SUMMARY_LIMIT - 1] + "…"


def summarize_document(text: str) -> tuple[str, str, dict[str, str]]:
    """Return (title, one-line summary, front-matter keys) of a Markdown text."""
    title = ""
    summary = ""
    front: dict[str, str] = {}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        heading = HEADING_PATTERN.match(line)
        if heading and not title:
            title = _plain(heading.group("title"))
            continue
        match = FRONT_PATTERN.match(line)
        if match and match.group("key") in FRONT_KEYS and not title:
            front[match.group("key")] = match.group("value")
            continue
        is_list = line.startswith(LIST_PREFIXES) or re.match(r"^\d+[.)]\s", line)
        if title and not summary and not is_list:
            summary = _first_sentence(_plain(line))
    return title or "(題名なし)", summary or "-", front


def render_doc_index(root: Path) -> str:
    """Render the Markdown table of contents for all documents."""
    paths = [root / name for name in ROOT_DOCS if (root / name).is_file()]
    paths += sorted(
        p
        for p in (root / "doc").rglob("*.md")
        if not SKIP_PARTS & set(p.relative_to(root).parts)
    )
    lines = [
        "<!-- 自動生成: tools/generate_doc_index.py。手で編集しない。 -->",
        "# 文書目次",
        "",
        "文書を探す前にここを読みます。題名と先頭の1文だけを載せます。内容の正本は各文書です。",
        "",
        "| 文書 | 題名 | 先頭の要約 | 親Issue | 確認日 |",
        "|---|---|---|---|---|",
    ]
    for path in paths:
        title, summary, front = summarize_document(path.read_text(encoding="utf-8"))
        relative = path.relative_to(root).as_posix()
        lines.append(
            f"| [`{relative}`](../{relative}) | {title} | {summary} | {front.get('Parent', '-')} | {front.get('Last-Checked', '-')} |"
        )
    lines.append("")
    return "\n".join(lines)
