"""Generate a module table of contents, responsibility table and dependency diagram.

The output is derived only from ``generated/repo_map.json`` (AST index) and the
source text, so it is deterministic and never hand-edited.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_PREFIX = "src/ai_game_player/"
ROOT_PACKAGE = "ai_game_player"
TOP_LEVEL_GROUP = "(その他の直下)"
MIN_PREFIX_GROUP_SIZE = 4
NAMED_BODY_PATTERN = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_.]*\s*[:：]\s*(?P<text>.+)$")
RESPONSIBILITY_PATTERN = re.compile(r"^\s*#\s*責務:\s*\[(?P<body>[^\]\n]+)\]\s*$")
DOCSTRING_PATTERN = re.compile(r'^\s*(?:r|u)?"""(?P<body>.*?)(?:"""|$)')
MAX_RESPONSIBILITY_CHARS = 90
HUB_LIMIT = 15
NO_RESPONSIBILITY = "(責務コメントなし)"


def _truncate(text: str) -> str:
    text = " ".join(text.split()).replace("|", "/")
    if len(text) <= MAX_RESPONSIBILITY_CHARS:
        return text
    return text[: MAX_RESPONSIBILITY_CHARS - 1] + "…"


def extract_responsibility(source_text: str) -> str:
    """Return the first one-line 責務 comment, else the module docstring's first line."""
    lines = source_text.splitlines()
    for line in lines:
        match = RESPONSIBILITY_PATTERN.match(line)
        if match:
            body = match.group("body")
            # "Class: 説明" の形式（名前が識別子のとき）だけ名前を落とし、名前の無い説明は先頭から残す。
            named = NAMED_BODY_PATTERN.match(body)
            return _truncate(named.group("text") if named else body)
    for line in lines[:5]:
        match = DOCSTRING_PATTERN.match(line)
        if match and match.group("body").strip():
            return _truncate(match.group("body"))
    return NO_RESPONSIBILITY


def _prefix_groups(module_names: list[str]) -> set[str]:
    """Return file-name prefixes shared by enough top-level modules to form a group."""
    counts = Counter(
        n.split(".")[1].split("_")[0] for n in module_names if n.count(".") == 1
    )
    return {
        prefix for prefix, count in counts.items() if count >= MIN_PREFIX_GROUP_SIZE
    }


def _group_of(module_name: str, prefixes: set[str]) -> str:
    parts = module_name.split(".")
    if len(parts) > 2:
        return parts[1]
    prefix = parts[1].split("_")[0]
    return f"{prefix}_*" if prefix in prefixes else TOP_LEVEL_GROUP


def _anchor(group: str) -> str:
    if group == TOP_LEVEL_GROUP:
        return "group-misc-top"
    suffix = "-prefix" if group.endswith("_*") else ""
    return "group-" + re.sub(r"[^0-9a-z]+", "-", group.lower()).strip("-") + suffix


def _group_label(group: str) -> str:
    if group == TOP_LEVEL_GROUP:
        return "接頭辞で束ねられない直下のモジュール"
    if group.endswith("_*"):
        return f"直下の`{group[:-1]}`で始まるモジュール"
    return f"`{group}/`"


def _top_level_classes(module: dict[str, Any]) -> list[str]:
    return [
        s["name"]
        for s in module["symbols"]
        if s.get("kind") == "class" and "." not in s["qualified_name"]
    ]


def render_module_index(root: Path, repo_map_text: str) -> str:
    """Render the Markdown index for the source modules listed in the repo map."""
    modules = [
        m
        for m in json.loads(repo_map_text)["modules"]
        if m["path"].startswith(SOURCE_PREFIX) and m["module"] != ROOT_PACKAGE
    ]
    names = {m["module"] for m in modules}
    prefixes = _prefix_groups(sorted(names))
    dependents: Counter[str] = Counter()
    edges: Counter[tuple[str, str]] = Counter()
    for module in modules:
        source_group = _group_of(module["module"], prefixes)
        for dependency in module["dependencies"]:
            if dependency not in names or dependency == module["module"]:
                continue
            dependents[dependency] += 1
            target_group = _group_of(dependency, prefixes)
            if target_group != source_group:
                edges[(source_group, target_group)] += 1

    groups: dict[str, list[dict[str, Any]]] = {}
    for module in sorted(modules, key=lambda m: m["module"]):
        groups.setdefault(_group_of(module["module"], prefixes), []).append(module)
    ordered_groups = sorted(groups, key=lambda g: (g == TOP_LEVEL_GROUP, g))

    responsibilities = {
        m["module"]: extract_responsibility(
            (root / m["path"]).read_text(encoding="utf-8")
        )
        for m in modules
    }
    missing = sum(text == NO_RESPONSIBILITY for text in responsibilities.values())
    lines = [
        "<!-- 自動生成: tools/generate_module_index.py。手で編集しない。 -->",
        "# モジュール目次・責務表・依存図",
        "",
        "ソースを探す前にここを読みます。責務は各クラス直前の構造化コメント「責務」（なければモジュールの説明）の1行目です。",
        "依存先・依存元は`src/ai_game_player`内のimportだけを数えます。",
        "",
        f"責務コメントなし: {missing}件（読む前に、先に構造化コメントの追加を検討する）。",
        "",
        "## 目次",
        "",
        "| グループ | モジュール数 | 内容 |",
        "|---|---|---|",
    ]
    for group in ordered_groups:
        label = _group_label(group)
        lines.append(
            f"| [{group}](#{_anchor(group)}) | {len(groups[group])} | {label} |"
        )
    lines += [
        "",
        "## 最初に読む場所（依存元が多いモジュール）",
        "",
        "| モジュール | 依存元の数 | 責務 |",
        "|---|---|---|",
    ]
    by_name = {m["module"]: m for m in modules}
    for name, count in sorted(dependents.items(), key=lambda item: (-item[1], item[0]))[
        :HUB_LIMIT
    ]:
        module = by_name[name]
        text = (root / module["path"]).read_text(encoding="utf-8")
        lines.append(
            f"| [`{name}`](../{module['path']}) | {count} | {extract_responsibility(text)} |"
        )

    lines += ["", "## グループ間の依存図", "", "```mermaid", "flowchart LR"]
    for group in ordered_groups:
        lines.append(
            f'    {_anchor(group).replace("-", "_")}["{group} ({len(groups[group])})"]'
        )
    for (source, target), count in sorted(edges.items()):
        lines.append(
            f"    {_anchor(source).replace('-', '_')} -->|{count}| {_anchor(target).replace('-', '_')}"
        )
    lines += ["```", ""]

    for group in ordered_groups:
        lines += [
            f'<a id="{_anchor(group)}"></a>',
            f"## {group}",
            "",
            "| モジュール | 主なクラス | 責務 | 依存先 | 依存元 |",
            "|---|---|---|---|---|",
        ]
        for module in groups[group]:
            classes = ", ".join(f"`{c}`" for c in _top_level_classes(module)[:3]) or "-"
            outgoing = len(
                [
                    d
                    for d in module["dependencies"]
                    if d in names and d != module["module"]
                ]
            )
            short = module["module"].removeprefix(ROOT_PACKAGE + ".")
            lines.append(
                f"| [`{short}`](../{module['path']}) | {classes} | {responsibilities[module['module']]} | {outgoing} | {dependents[module['module']]} |"
            )
        lines.append("")
    return "\n".join(lines)
