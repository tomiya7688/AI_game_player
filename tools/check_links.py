"""Check local Markdown links in explicitly selected instruction documents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\((<[^>\n]+>|[^)\s]+)(?:\s+['\"][^\n]*?['\"])?\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(.*?)(\1)")
WINDOWS_PATH = re.compile(r"^[a-zA-Z]:[/\\]")


def check_document(path: Path) -> dict:
    """Check link target existence only; never open the linked files."""
    result = {"document": str(path), "missing": [], "skipped": [], "checked": 0}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        result["error"] = str(exc)
        return result
    fence = None
    for number, line in enumerate(lines, 1):
        match = FENCE.match(line)
        if match:
            token = match.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        line = INLINE_CODE.sub("", line)
        for match in LINK.finditer(line):
            raw = match.group(1).strip("<>")
            if raw.startswith("#"):
                continue
            decoded = unquote(raw)
            if decoded[:2].replace("/", "\\") == "\\\\":
                result["skipped"].append(
                    {"line": number, "target": raw, "reason": "network path"}
                )
                continue
            if re.match(r"^[a-zA-Z]:(?![/\\])", decoded):
                result["skipped"].append(
                    {"line": number, "target": raw, "reason": "drive-relative path"}
                )
                continue
            if WINDOWS_PATH.match(raw):
                raw_path = raw.split("#", 1)[0].split("?", 1)[0]
            else:
                try:
                    parsed = urlsplit(raw)
                except ValueError as exc:
                    result["skipped"].append(
                        {"line": number, "target": raw, "reason": str(exc)}
                    )
                    continue
                if parsed.scheme.lower() == "file":
                    result["skipped"].append(
                        {
                            "line": number,
                            "target": raw,
                            "reason": "unsupported file URI",
                        }
                    )
                    continue
                if parsed.scheme or parsed.netloc:
                    continue
                raw_path = parsed.path
            if not raw_path or any(ch in raw_path for ch in "*{}$"):
                result["skipped"].append(
                    {"line": number, "target": raw, "reason": "dynamic or empty path"}
                )
                continue
            target = Path(unquote(raw_path))
            if not target.is_absolute():
                target = path.parent / target
            try:
                target.stat()
                result["checked"] += 1
            except FileNotFoundError:
                result["checked"] += 1
                result["missing"].append({"line": number, "target": raw})
            except (OSError, ValueError) as exc:
                result["skipped"].append(
                    {"line": number, "target": raw, "reason": str(exc)}
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "documents", nargs="+", type=Path, help="Explicit Markdown files to inspect"
    )
    args = parser.parse_args()
    results = [check_document(path) for path in args.documents]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return int(
        any(item.get("error") or item["missing"] or item["skipped"] for item in results)
    )


if __name__ == "__main__":
    sys.exit(main())
