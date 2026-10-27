"""Check that the main-branch ruleset requires exactly the current CI job names.

GitHub reports a required status check by the job's display name. When a job is
renamed or a matrix value changes, a ruleset that still lists the old name waits
forever for a check that never reports, and a ruleset that lacks a new job lets a
failing job through. This checker derives the expected names from the required
workflows and fails when `.github/rulesets/main-protection.json` differs, so the
rename and the ruleset update land in the same pull request. It also checks that
the ruleset file keeps the protection settings described in doc/main保護方針.md
(active, default branch, no bypass, checks pinned to GitHub Actions).

The checker reads the ruleset file in the repository. It does not read or change
the ruleset that is applied on GitHub.

Only the workflow forms used in this repository are evaluated: a job `name:` whose
`${{ matrix.KEY }}` placeholders come from quoted or plain-text inline lists
(`KEY: ['3.10', ubuntu-latest]`), or from an `include:`-only matrix. Forms whose
reported name or reporting behaviour this checker cannot predict raise an error
instead of being guessed: a matrix job without `name:`, reusable-workflow jobs
(`uses:`), job-level `if:`, other `${{ }}` expressions in a name, unquoted values
that YAML would turn into numbers or booleans, mixed lists and `include:`,
`exclude:`, and block lists.
"""

import argparse
import itertools
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULESET = REPO_ROOT / ".github" / "rulesets" / "main-protection.json"
GITHUB_ACTIONS_APP_ID = 15368

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The merge gate (tools/finish_pr_merge.py) and this ruleset must require the same
# workflows, so both read the single list in pr_workflow_evidence.
from tools.pr_workflow_evidence import REQUIRED_WORKFLOW_PATHS  # noqa: E402

_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_JOB_FIELD = re.compile(r"^    ([A-Za-z0-9_-]+):\s*(.*?)\s*$")
_MATRIX = re.compile(r"^(\s+)matrix:\s*(?:#.*)?$")
_MATRIX_WITH_VALUE = re.compile(r"^\s+(?:matrix|strategy):\s*[^\s#]")
_INLINE_LIST = re.compile(r"^\s+([A-Za-z0-9_-]+):\s*\[([^\]]*)\]\s*(?:#.*)?$")
_BLOCK_KEY = re.compile(r"^\s+([A-Za-z0-9_-]+):\s*(?:#.*)?$")
_PAIR = re.compile(r"^([A-Za-z0-9_-]+):\s*(.+?)\s*$")
_LIST_ITEM = re.compile(r"""\s*('[^']*'|"[^"]*"|[^,'"]+)\s*(?:,|$)""")
_PLACEHOLDER = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_ANY_EXPRESSION = re.compile(r"\$\{\{")
_YAML_NON_STRING = re.compile(
    r"^(?:[-+]?(?:[\d_]+\.?[\d_]*|\.\d+)(?:[eE][-+]?\d+)?|0x[0-9a-f_]+|0o[0-7_]+|[-+]?\.inf|\.nan"
    r"|true|false|yes|no|on|off|null|~)$",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"""^('[^']*'|"[^"]*")\s*(?:#.*)?$""")
_PLAIN_SPECIAL = re.compile(r"[\[\]{}&*!|>%@`]|\$\{\{")


class RulesetCheckError(ValueError):
    """A workflow or ruleset uses a form this checker cannot evaluate."""


def _scalar(value: str, where: str, *, is_job_name: bool = False) -> str:
    """Return a YAML scalar as GitHub displays it, rejecting values YAML would retype.

    Job names legitimately contain `${{ }}` and brackets, so the YAML-syntax check
    applies only to matrix values.
    """
    value = value.strip()
    quoted = _QUOTED.match(value)
    if quoted:
        return quoted.group(1)[1:-1]
    if value[:1] in ("'", '"'):
        raise RulesetCheckError(f"{where}: cannot read quoted value {value!r}")
    value = re.sub(r"\s+#.*$", "", value).strip()
    if not value or value.startswith("#"):
        raise RulesetCheckError(f"{where}: empty value")
    if not is_job_name and _PLAIN_SPECIAL.search(value):
        raise RulesetCheckError(
            f"{where}: quote the value {value!r}; it contains YAML syntax"
        )
    if _YAML_NON_STRING.match(value):
        raise RulesetCheckError(
            f"{where}: quote the value {value!r}; YAML may display it differently"
        )
    return value


def _inline_list(text: str, where: str) -> list[str]:
    items: list[str] = []
    position = 0
    text = text.strip()
    while position < len(text):
        match = _LIST_ITEM.match(text, position)
        if not match or match.end() == position:
            raise RulesetCheckError(f"{where}: cannot read inline list [{text}]")
        items.append(_scalar(match.group(1), where))
        position = match.end()
    if not items:
        raise RulesetCheckError(f"{where}: empty matrix list")
    return items


def _job_blocks(text: str) -> list[tuple[str, list[str]]]:
    lines = text.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if re.match(r"^jobs:\s*(?:#.*)?$", line)
        )
    except StopIteration as error:
        raise RulesetCheckError("workflow has no top-level jobs: section") from error
    blocks: list[tuple[str, list[str]]] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        match = _JOB_KEY.match(line)
        if match:
            blocks.append((match.group(1), []))
        elif blocks:
            blocks[-1][1].append(line)
    return blocks


def _matrix_combinations(
    job_id: str, body: list[str], where: str
) -> list[dict[str, str]] | None:
    """Expand a job's matrix into the value combinations GitHub runs, or None without a matrix.

    Supported: only inline lists (cartesian product), or only `include:` items of
    scalar `key: value` pairs, where GitHub runs each item as one combination.
    """
    for index, line in enumerate(body):
        found = _MATRIX.match(line)
        if not found:
            if _MATRIX_WITH_VALUE.match(line):
                raise RulesetCheckError(
                    f"{where}: job {job_id} declares an inline matrix or strategy; not supported"
                )
            continue
        indent = len(found.group(1))
        lists: dict[str, list[str]] = {}
        includes: list[dict[str, str]] = []
        in_include = False
        for entry in body[index + 1 :]:
            if not entry.strip() or entry.lstrip().startswith("#"):
                continue
            depth = len(entry) - len(entry.lstrip())
            if depth <= indent:
                break
            stripped = entry.strip()
            if depth == indent + 2:
                in_include = False
                inline = _INLINE_LIST.match(entry)
                if inline:
                    lists[inline.group(1)] = _inline_list(
                        inline.group(2), f"{where}: job {job_id}"
                    )
                    continue
                key = _BLOCK_KEY.match(entry)
                if key and key.group(1) == "include":
                    in_include = True
                    continue
                raise RulesetCheckError(
                    f"{where}: job {job_id} matrix entry is not supported: {stripped}"
                )
            if not in_include:
                raise RulesetCheckError(
                    f"{where}: job {job_id} matrix entry is not supported: {stripped}"
                )
            if stripped.startswith("- "):
                includes.append({})
                stripped = stripped[2:].strip()
            pair = _PAIR.match(stripped)
            if not includes or not pair:
                raise RulesetCheckError(
                    f"{where}: job {job_id} include entry is not supported: {stripped}"
                )
            includes[-1][pair.group(1)] = _scalar(
                pair.group(2), f"{where}: job {job_id}"
            )
        if lists and includes:
            raise RulesetCheckError(
                f"{where}: job {job_id} mixes matrix lists and include; not supported"
            )
        if includes:
            return includes
        if not lists:
            raise RulesetCheckError(
                f"{where}: job {job_id} has an empty or unreadable matrix"
            )
        keys = list(lists)
        return [
            dict(zip(keys, values))
            for values in itertools.product(*(lists[key] for key in keys))
        ]
    return None


def job_contexts(workflow_text: str, workflow_name: str = "workflow") -> list[str]:
    """Return the status-check names GitHub reports for every job in one workflow."""
    contexts: list[str] = []
    for job_id, body in _job_blocks(workflow_text):
        fields = {m.group(1): m.group(2) for m in map(_JOB_FIELD.match, body) if m}
        where = f"{workflow_name}: job {job_id}"
        if "uses" in fields:
            raise RulesetCheckError(
                f"{where} calls a reusable workflow; its check name is not modelled"
            )
        if "if" in fields:
            raise RulesetCheckError(
                f"{where} has a job-level if:; a skipped required check would block or pass PRs"
            )
        combinations = _matrix_combinations(job_id, body, workflow_name)
        if "name" not in fields:
            if combinations is not None:
                raise RulesetCheckError(
                    f"{where} uses a matrix without name:; add an explicit name"
                )
            contexts.append(job_id)
            continue
        name = _scalar(fields["name"], where, is_job_name=True)
        keys = list(dict.fromkeys(_PLACEHOLDER.findall(name)))
        if _ANY_EXPRESSION.search(_PLACEHOLDER.sub("", name)):
            raise RulesetCheckError(f"{where} name has a non-matrix expression: {name}")
        if not keys:
            if combinations is not None and len(combinations) > 1:
                raise RulesetCheckError(
                    f"{where} runs {len(combinations)} matrix jobs under the same name: {name}"
                )
            contexts.append(name)
            continue
        if combinations is None:
            raise RulesetCheckError(f"{where} uses matrix.{keys[0]} but has no matrix")
        for combination in combinations:
            absent = [key for key in keys if key not in combination]
            if absent:
                raise RulesetCheckError(
                    f"{where} uses matrix.{absent[0]} without a value in every combination"
                )
            contexts.append(_PLACEHOLDER.sub(lambda m: combination[m.group(1)], name))
    return contexts


def expected_contexts(workflow_dir: Path) -> set[str]:
    """Collect the job names of every required workflow; duplicates are an error."""
    seen: dict[str, str] = {}
    for workflow_path in sorted(REQUIRED_WORKFLOW_PATHS):
        path = workflow_dir / Path(workflow_path).name
        if not path.is_file():
            raise RulesetCheckError(f"required workflow is missing: {path}")
        for context in job_contexts(path.read_text(encoding="utf-8"), path.name):
            if context in seen:
                source = (
                    f"twice in {path.name}"
                    if seen[context] == path.name
                    else f"by both {seen[context]} and {path.name}"
                )
                raise RulesetCheckError(
                    f"check name {context!r} is reported {source}; "
                    "a required check cannot tell them apart"
                )
            seen[context] = path.name
    return set(seen)


def ruleset_contexts(ruleset_path: Path) -> set[str]:
    """Validate the ruleset's protection settings and return its required check names."""
    data = json.loads(ruleset_path.read_text(encoding="utf-8"))
    try:
        return _validated_contexts(data)
    except (AttributeError, TypeError) as error:
        raise RulesetCheckError(
            f"ruleset has an unexpected structure: {error}"
        ) from error


def _validated_contexts(data: object) -> set[str]:
    if not isinstance(data, dict):
        raise RulesetCheckError("ruleset JSON must be an object")
    if data.get("enforcement") != "active":
        raise RulesetCheckError("ruleset enforcement must be 'active'")
    ref_name = data.get("conditions", {}).get("ref_name", {})
    ref_include = ref_name.get("include")
    if (
        data.get("target") != "branch"
        or not isinstance(ref_include, list)
        or "~DEFAULT_BRANCH" not in ref_include
    ):
        raise RulesetCheckError("ruleset must target the default branch")
    if ref_name.get("exclude", []) != []:
        raise RulesetCheckError("ruleset must not exclude branches from the target")
    if data.get("bypass_actors") != []:
        raise RulesetCheckError(
            "ruleset must not define bypass actors (see doc/main保護方針.md)"
        )
    rules = data.get("rules")
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        raise RulesetCheckError("ruleset rules must be a list of objects")
    types = [rule.get("type") for rule in rules]
    for required in ("deletion", "non_fast_forward", "pull_request"):
        if required not in types:
            raise RulesetCheckError(f"ruleset must contain the {required} rule")
    status_rules = [
        rule for rule in rules if rule.get("type") == "required_status_checks"
    ]
    if len(status_rules) != 1:
        raise RulesetCheckError(
            "ruleset must contain exactly one required_status_checks rule"
        )
    checks = status_rules[0].get("parameters", {}).get("required_status_checks")
    if (
        not isinstance(checks, list)
        or not checks
        or not all(isinstance(check, dict) for check in checks)
    ):
        raise RulesetCheckError(
            "required_status_checks must be a non-empty list of objects"
        )
    contexts = [str(check.get("context", "")) for check in checks]
    if len(contexts) != len(set(contexts)) or not all(contexts):
        raise RulesetCheckError(
            "required status check contexts must be non-empty and unique"
        )
    unpinned = [
        check.get("context")
        for check in checks
        if check.get("integration_id") != GITHUB_ACTIONS_APP_ID
    ]
    if unpinned:
        raise RulesetCheckError(
            f"checks must set integration_id {GITHUB_ACTIONS_APP_ID} (GitHub Actions): {unpinned}"
        )
    return set(contexts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check main-branch ruleset required checks against CI jobs."
    )
    parser.add_argument("--ruleset", type=Path, default=DEFAULT_RULESET)
    parser.add_argument(
        "--workflows", type=Path, default=REPO_ROOT / ".github" / "workflows"
    )
    parser.add_argument(
        "--print-expected",
        action="store_true",
        help="print the expected checks as ruleset JSON",
    )
    args = parser.parse_args(argv)
    try:
        expected = expected_contexts(args.workflows)
        if args.print_expected:
            items = [
                {"context": c, "integration_id": GITHUB_ACTIONS_APP_ID}
                for c in sorted(expected)
            ]
            print(json.dumps(items, indent=2, ensure_ascii=False))
            return 0
        actual = ruleset_contexts(args.ruleset)
    except (
        RulesetCheckError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"RULESET CHECK ERROR: {error}", file=sys.stderr)
        return 2
    missing, extra = sorted(expected - actual), sorted(actual - expected)
    for context in missing:
        print(f"missing from ruleset: {context}")
    for context in extra:
        print(f"ruleset requires a check no workflow reports: {context}")
    if missing or extra:
        print("Update .github/rulesets/main-protection.json (see --print-expected).")
        return 1
    print(f"RULESET CHECK OK ({len(expected)} required checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
