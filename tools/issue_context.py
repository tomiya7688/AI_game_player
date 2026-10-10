"""Metadata-first task routing; generated packs are pointers, not specifications."""

import argparse
import json
import re
import subprocess
from pathlib import Path

REPO = "tomiya7688/AI_game_player"
OUT = Path(".codex/next_issue.md")
REPO_MAP = Path("generated/repo_map.json")
RANK = {f"p{i}": i for i in range(6)}
CODE_SUFFIXES = {".bat", ".c", ".cpp", ".h", ".hpp", ".json", ".md", ".py", ".ps1", ".toml", ".txt", ".yml", ".yaml"}
GENERIC_SYMBOLS = {"check", "config", "error", "issue", "map", "module", "name", "path", "repo", "result", "run", "source", "state", "symbol", "task", "test", "value"}
GENERIC_SYMBOL_TERMS = GENERIC_SYMBOLS | {"candidate", "confidence", "file", "input", "output", "pack", "repository", "text"}
QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $endCursor) {
      nodes { number title url labels(first: 100) { nodes { name } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
DOCS = {
    "spec:recognition": ["doc/画像解析機能説明書.md", "doc/OCR候補検出機能説明書.md", "doc/候補統合機能説明書.md"],
    "spec:capture": ["doc/画面キャプチャ機能説明書.md", "doc/観測入力機能説明書.md"],
    "spec:decision": ["doc/判断パイプライン機能説明書.md", "doc/評価指標機能説明書.md"],
    "spec:execution": ["doc/操作実行機能説明書.md"],
    "spec:persistence": ["doc/設定永続化機能説明書.md", "doc/知識ベース機能説明書.md"],
}

def gh_json(args):
    result = subprocess.check_output(
        ["gh", *args], text=True, encoding="utf-8", timeout=120
    )
    return json.loads(result)


def fetch_issues():
    """Paginate metadata only, including older Issues beyond the first 100."""
    owner, name = REPO.split("/")
    pages = gh_json([
        "api", "graphql", "--paginate", "--slurp", "-f", f"query={QUERY}",
        "-f", f"owner={owner}", "-f", f"name={name}",
    ])
    issues = []
    for page in pages:
        if page.get("errors"):
            raise ValueError("GitHub returned GraphQL errors; metadata is incomplete.")
        connection = page["data"]["repository"]["issues"]
        for node in connection["nodes"]:
            issues.append({**node, "labels": node["labels"]["nodes"]})
    if pages and connection["pageInfo"]["hasNextPage"]:
        raise ValueError("GitHub pagination is incomplete; refusing partial selection.")
    return issues


def fetch_issue(number):
    """Fetch requirements for exactly one Issue, without loading discussion."""
    issue = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    if issue.get("pull_request") is not None or issue["state"] != "open":
        raise ValueError("The selected item must be an open Issue, not a PR.")
    return {**issue, "url": issue["html_url"]}


def fetch_worktree_paths():
    """Return changed repository paths without reading their contents."""
    raw_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        timeout=10,
    )
    entries = raw_status.decode("utf-8", errors="replace").split("\0")
    changed_paths = []
    skip_source_path = False
    for entry in entries:
        if not entry:
            continue
        if skip_source_path:
            skip_source_path = False
            continue
        status = entry[:2]
        path = entry[3:].replace("\\", "/")
        if status.strip() and not path.startswith(".codex/") and path != "AGENTS.override.md":
            if Path(path).suffix.lower() in CODE_SUFFIXES:
                changed_paths.append({"status": status.strip(), "path": path})
        skip_source_path = "R" in status or "C" in status
    return sorted(changed_paths, key=lambda item: item["path"])


def load_repository_map(path=REPO_MAP):
    """Load only a schema-valid Repository Map; never infer from malformed JSON."""
    try:
        repository_map = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"Repository Map unavailable: {error}"
    if (
        not isinstance(repository_map, dict)
        or repository_map.get("format") != "kadoka-repository-map"
        or type(repository_map.get("schema_version")) is not int
        or repository_map.get("schema_version") != 1
        or not isinstance(repository_map.get("modules"), list)
        or any(
            not isinstance(module, dict)
            or not isinstance(module.get("module"), str)
            or not isinstance(module.get("path"), str)
            or not isinstance(module.get("symbols"), list)
            or any(not isinstance(symbol, dict) for symbol in module.get("symbols", []))
            or not isinstance(module.get("dependencies"), list)
            or any(not isinstance(dependency, str) for dependency in module.get("dependencies", []))
            or not isinstance(module.get("imports"), list)
            or any(
                not isinstance(imported_module, dict)
                or not isinstance(imported_module.get("local_dependencies"), list)
                or any(
                    not isinstance(dependency, str)
                    for dependency in imported_module.get("local_dependencies", [])
                )
                for imported_module in module.get("imports", [])
            )
            for module in repository_map["modules"]
        )
    ):
        return None, "Repository Map has an unsupported format or schema version."
    return repository_map, None


def _search_terms(value):
    terms = set()
    for match in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[ぁ-んァ-ヶ一-龠]{2,}", value):
        normalized = match.lower()
        terms.add(normalized)
        terms.update(part for part in normalized.split("_") if part)
    return terms


def _contains_identifier(text, identifier):
    if not identifier:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(identifier.lower())}(?![A-Za-z0-9_])",
        text.lower(),
    ) is not None


def _contains_module_identifier(text, module_name):
    if not module_name:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_\\/]){re.escape(module_name.lower())}(?![A-Za-z0-9_\\/])",
        text.lower(),
    ) is not None


def _rank_module_candidates(issue, repository_map, changed_paths, *, tests_only, related_sources=None):
    if repository_map is None:
        return []
    issue_text = f"{issue['title']}\n{issue.get('body') or ''}"
    issue_terms = _search_terms(issue_text)
    changed_by_path = {item["path"]: item["status"] for item in changed_paths}
    candidates = []
    for module in repository_map["modules"]:
        path = module.get("path")
        module_name = module.get("module")
        if not isinstance(path, str) or not isinstance(module_name, str):
            continue
        is_test_module = path.startswith("tests/") or "/tests/" in path
        if is_test_module != tests_only:
            continue
        confidence = 0.0
        reasons = []
        matched_symbols = []
        if path in changed_by_path:
            confidence = 0.98
            reasons.append(f"working tree marks this path {changed_by_path[path]}")
        if _contains_identifier(issue_text, path):
            path_confidence = 0.99 if tests_only else 0.92
            confidence = max(confidence, path_confidence)
            path_description = "test" if tests_only else "source"
            reasons.append(f"Issue text names this {path_description} path")
        if _contains_module_identifier(issue_text, module_name):
            confidence = max(confidence, 0.86)
            reasons.append("Issue text names this module")
        for symbol in module.get("symbols", []):
            symbol_name = symbol.get("qualified_name") or symbol.get("name")
            if (
                isinstance(symbol_name, str)
                and symbol_name.rsplit(".", 1)[-1].lower() not in GENERIC_SYMBOLS
                and (
                    _contains_identifier(issue_text, symbol_name)
                    or _contains_identifier(issue_text, symbol_name.rsplit(".", 1)[-1])
                )
            ):
                matched_symbols.append(symbol_name)
        if matched_symbols:
            confidence = max(confidence, 0.84)
            reasons.append("Issue text names symbol(s): " + ", ".join(sorted(set(matched_symbols))[:3]))
        if not tests_only:
            candidate_terms = _search_terms(f"{path} {module_name}")
            matched_terms = sorted(
                (issue_terms & candidate_terms)
                - {"ai", "ai_game_player", "and", "for", "from", "game", "h", "hpp", "player", "py", "src", "test", "tests", "the", "tools", "with", "cpp"}
                - GENERIC_SYMBOL_TERMS
            )
            if matched_terms:
                confidence = max(confidence, min(0.68, 0.34 + 0.08 * len(matched_terms)))
                reasons.append("shared identifier(s): " + ", ".join(matched_terms[:3]))
        if tests_only:
            module_dependencies = set(module.get("dependencies", []))
            for imported_module in module.get("imports", []):
                module_dependencies.update(imported_module.get("local_dependencies", []))
            for source_module in related_sources or []:
                if source_module.get("module") in module_dependencies:
                    dependency_confidence = min(0.95, source_module.get("confidence", 0.0) + 0.03)
                    confidence = max(confidence, dependency_confidence)
                    reasons.append(
                        f"Repository Map dependency connects this test to `{source_module['module']}`"
                    )
            test_stem = Path(path).stem.removeprefix("test_")
            for source_module in related_sources or []:
                source_path = source_module.get("path", "")
                if (
                    isinstance(source_path, str)
                    and not source_path.startswith("tests/")
                    and Path(source_path).stem == test_stem
                ):
                    filename_confidence = min(0.99, source_module.get("confidence", 0.0) + 0.04)
                    confidence = max(confidence, filename_confidence)
                    reasons.append(f"test filename matches `{source_path}`")
            if reasons:
                candidates.append({
                    "path": path,
                    "module": module_name,
                    "confidence": confidence,
                    "reason": "; ".join(dict.fromkeys(reasons)),
                })
                continue
        if confidence:
            candidates.append({
                "path": path,
                "module": module_name,
                "confidence": confidence,
                "reason": "; ".join(dict.fromkeys(reasons)),
            })
    return sorted(candidates, key=lambda item: (-item["confidence"], item["path"]))


def _rank_symbol_candidates(issue, repository_map, modules):
    if repository_map is None:
        return []
    issue_text = f"{issue['title']}\n{issue.get('body') or ''}"
    issue_terms = _search_terms(issue_text)
    modules_by_path = {module.get("path"): module for module in repository_map["modules"]}
    candidates = []
    for module_candidate in modules[:4]:
        module = modules_by_path.get(module_candidate["path"], {})
        for symbol in module.get("symbols", []):
            symbol_name = symbol.get("qualified_name") or symbol.get("name")
            if not isinstance(symbol_name, str):
                continue
            terminal_name = symbol_name.rsplit(".", 1)[-1]
            if terminal_name.lower() in GENERIC_SYMBOLS:
                continue
            if (
                _contains_identifier(issue_text, symbol_name)
                or _contains_identifier(issue_text, terminal_name)
            ):
                candidates.append({
                    "name": f"{module_candidate['module']}.{symbol_name}",
                    "confidence": 0.84,
                    "reason": "Issue text names this qualified or terminal symbol",
                })
                continue
            symbol_terms = {
                part.lower()
                for part in re.findall(r"[A-Za-z][A-Za-z0-9]*", re.sub(r"([a-z])([A-Z])", r"\1 \2", symbol_name))
            }
            matched_terms = sorted((issue_terms & symbol_terms) - GENERIC_SYMBOL_TERMS)
            if matched_terms:
                confidence = min(0.68, 0.36 + 0.08 * len(matched_terms))
                candidates.append({
                    "name": f"{module_candidate['module']}.{symbol_name}",
                    "confidence": confidence,
                    "reason": "shared identifier(s): " + ", ".join(matched_terms[:3]),
                })
    return sorted(candidates, key=lambda item: (-item["confidence"], item["name"]))[:4]


def render_repository_evidence(issue, repository_map, changed_paths, map_error=None, max_chars=900):
    """Render bounded, evidence-backed source and test candidates."""
    working_lines = [
        f"- `{item['path']}` ({item['status']})" for item in changed_paths[:4]
    ] or ["- No relevant changed or untracked repository paths were reported by Git."]
    modules = _rank_module_candidates(issue, repository_map, changed_paths, tests_only=False)
    if modules:
        module_lines = [
            f"- `{item['path']}` — confidence {item['confidence']:.2f}; {item['reason']}"
            for item in modules[:4]
        ]
    elif map_error:
        module_lines = [f"- {map_error}; inspect the Issue's named files or regenerate the map."]
    else:
        module_lines = ["- No module matched the Issue text or Git working-tree paths."]
    symbols = _rank_symbol_candidates(issue, repository_map, modules)
    if symbols:
        symbol_lines = [
            f"- `{item['name']}` — confidence {item['confidence']:.2f}; {item['reason']}"
            for item in symbols[:3]
        ]
    else:
        symbol_lines = ["- No declaration matched a specific Issue identifier; inspect the listed module declarations."]
    tests = _rank_module_candidates(
        issue,
        repository_map,
        changed_paths,
        tests_only=True,
        related_sources=modules[:4],
    )
    if tests:
        test_lines = [
            f"- Test candidate `{item['path']}` — confidence {item['confidence']:.2f}; {item['reason']}"
            for item in tests[:3]
        ]
    else:
        test_lines = ["- No test module matched; identify a fixture after reading the candidate source."]

    def compose_evidence():
        return "\n".join([
            "## Working-tree paths",
            *working_lines,
            "",
            "## Related module and symbol candidates",
            "Confidence is a heuristic match strength, not a probability or a verified dependency.",
            *module_lines,
            "Symbol candidates:",
            *symbol_lines,
            "",
            "## Candidate tests and checks",
            *test_lines,
            "- Required project check: `finish_task.bat`.",
            "- Treat candidate paths as navigation hints; confirm behavior in source and tests.",
        ])

    evidence = compose_evidence()
    while len(evidence) > max_chars:
        for candidate_lines in (symbol_lines, module_lines, working_lines, test_lines):
            if len(candidate_lines) > 1:
                candidate_lines.pop()
                break
        else:
            def compact_candidate_line(line, label):
                path_match = re.search(r"`([^`]+)`", line)
                if path_match is None:
                    if "Repository Map" in line:
                        return f"- {label}: map unavailable"
                    return f"- {label}: none"
                candidate_path = path_match.group(1)
                confidence_match = re.search(r"confidence ([0-9.]+)", line)
                confidence = f" ({confidence_match.group(1)})" if confidence_match else ""
                if "working tree marks" in line:
                    reason = "Git status"
                elif "Issue text names this source path" in line:
                    reason = "Issue path"
                elif "Issue text names this module" in line:
                    reason = "Issue module"
                elif "Issue text names symbol" in line:
                    reason = "Issue symbol"
                elif "shared identifier(s):" in line:
                    reason = "shared terms"
                elif "Repository Map dependency connects" in line:
                    reason = "map dependency"
                elif "test filename matches" in line:
                    reason = "filename match"
                elif confidence_match:
                    reason = "heuristic match"
                else:
                    reason = ""
                suffix = f"; {reason}" if reason else ""
                return f"- {label}: `{candidate_path}`{confidence}{suffix}"

            compact_sections = [
                ("Changed", working_lines),
                ("Module", module_lines),
                ("Symbol", symbol_lines),
                ("Test", test_lines),
            ]

            def compose_compact_evidence():
                compact_lines = ["## Repository evidence"]
                for _, candidate_lines in compact_sections:
                    compact_lines.extend(candidate_lines)
                compact_lines.append("- Required check: `finish_task.bat`.")
                return "\n".join(compact_lines)

            for label, candidate_lines in compact_sections:
                candidate_lines[:] = [
                    compact_candidate_line(line, label) for line in candidate_lines
                ]
            evidence = compose_compact_evidence()
            while len(evidence) > max_chars:
                for _, candidate_lines in (
                    compact_sections[2], compact_sections[1], compact_sections[0], compact_sections[3]
                ):
                    if any(": `" in line for line in candidate_lines):
                        candidate_lines[:] = [
                            line.split(": `", 1)[0] + ": none"
                            if ": `" in line else line
                            for line in candidate_lines
                        ]
                        break
                else:
                    map_unavailable = any(
                        "map unavailable" in line.lower()
                        for _, candidate_lines in compact_sections
                        for line in candidate_lines
                    )
                    if map_unavailable:
                        evidence = "Map unavailable; `finish_task.bat`"
                    else:
                        evidence = "No candidate fits; `finish_task.bat`"
                    if len(evidence) > max_chars:
                        raise ValueError("Context limit cannot fit the required project check.")
                    break
                evidence = compose_compact_evidence()
            break
        evidence = compose_evidence()
    return evidence

def key(issue):
    labels = {x["name"].lower() for x in issue.get("labels", [])}
    return min([RANK[x] for x in labels if x in RANK] or [50]), issue["number"]

def select_issue(issues):
    work = [
        issue for issue in issues
        if issue["number"] != 19
        and not re.search(r"\bparent\b|親Issue", issue["title"], re.IGNORECASE)
        and not any(x["name"].lower() == "parent" for x in issue.get("labels", []))
    ]
    return min(work, key=key) if work else None


def render_context(issue, max_chars=8000, repository_map=None, changed_paths=None, map_error=None):
    labels = [x["name"] for x in issue.get("labels", [])]
    low = {x.lower() for x in labels}
    labels_text = ", ".join(labels) or "(none)"
    if len(labels_text) > 180:
        visible_labels = []
        for label in labels:
            omitted_count = len(labels) - len(visible_labels) - 1
            omission_note = f", … ({omitted_count} labels omitted)" if omitted_count else ""
            proposed_labels = ", ".join([*visible_labels, label]) + omission_note
            if len(proposed_labels) > 180:
                break
            visible_labels.append(label)
        omitted_count = len(labels) - len(visible_labels)
        labels_text = ", ".join(visible_labels)
        if omitted_count:
            labels_text += f", … ({omitted_count} labels omitted)" if labels_text else f"{omitted_count} labels omitted"
    docs = []
    for label, paths in DOCS.items():
        if label in low:
            docs += paths
    rank = key(issue)[0]
    priority = f"P{rank}" if rank < 6 else "unlabeled"
    prefix = "\n".join([
        "# Next Issue",
        "",
        f"Issue: #{issue['number']} — {issue['title']}",
        f"Priority: {priority}",
        f"Labels: {labels_text}",
        f"Source of Truth: {issue['url']}",
        "",
        "## Requirements excerpt (not a replacement for the Issue)",
        "",
    ])
    suffix = "\n".join([
        "", "", "## Read only if needed",
        *([f"- `{x}`" for x in dict.fromkeys(docs)] or ["- Use the AGENTS.md task router for source/tests."]),
        "",
        "## Evidence / stop conditions",
        "- Discussion is not loaded. Read relevant comments at the Issue URL when requirements are unclear.",
        "- Determine Goal / Required / Acceptance, then stop broad exploration.",
        "- Missing or truncated requirements must be checked at the original Issue before implementation.",
        "- Search first; read target source/tests, then direct dependencies only as needed.",
        "- Use targeted tests while implementing; finish_task.bat remains mandatory before commit.",
        "- Report unexecuted runtime/GUI checks as Unverified.",
        "",
    ])
    marker = "\n\n[TRUNCATED: read the original Issue for omitted requirements.]"
    body = (issue.get("body") or "No description provided; clarify requirements before implementation.").strip()
    evidence_budget = min(
        900,
        max_chars - len(prefix) - len(suffix) - len(marker) - 2,
    )
    if evidence_budget <= 0:
        raise ValueError("Context limit cannot fit the issue pointers and truncation marker.")
    evidence = render_repository_evidence(
        issue,
        repository_map,
        changed_paths or [],
        map_error=map_error,
        max_chars=evidence_budget,
    )
    budget = max_chars - len(prefix) - len(evidence) - len(suffix) - 2
    if budget < len(marker):
        raise ValueError("Context limit cannot fit the required pointers and instructions.")
    if len(body) > budget:
        body = body[:budget - len(marker)] + marker
    return prefix + body + "\n\n" + evidence + suffix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, help="Explicit Issue; bypass metadata listing (including parent Issues).")
    parser.add_argument("--max-chars", type=int, default=8000, help="Pack character limit (2000-16000, not a token count).")
    args = parser.parse_args(argv)
    if args.issue is not None and args.issue <= 0:
        parser.error("--issue must be positive")
    if not 2000 <= args.max_chars <= 16000:
        parser.error("--max-chars must be between 2000 and 16000")
    try:
        selected = {"number": args.issue} if args.issue else select_issue(fetch_issues())
        if selected is None:
            raise ValueError("No open work Issues. Previous context, if any, must not be reused.")
        issue = fetch_issue(selected["number"])
        changed_paths = fetch_worktree_paths()
        repository_map, map_error = load_repository_map()
        content = render_context(
            issue,
            args.max_chars,
            repository_map=repository_map,
            changed_paths=changed_paths,
            map_error=map_error,
        )
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(content, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"[ERROR] Task context was not refreshed: {exc}")
        print("Do not use a previous .codex/next_issue.md after this failure.")
        return 1
    rank = key(issue)[0]
    priority = f"P{rank}" if rank < 6 else "unlabeled"
    print(f"[{priority}] #{issue['number']} {issue['title']}")
    print(f"Context: {OUT} ({len(content)} characters; one Issue body)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
