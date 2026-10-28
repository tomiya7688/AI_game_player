import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ruleset_check

REPO_ROOT = Path(__file__).resolve().parents[1]


def _matrix_job(matrix: str, name: str = "J ${{ matrix.os }}") -> str:
    return f"jobs:\n  j:\n    name: {name}\n    runs-on: x\n    strategy:\n      matrix:\n{matrix}"


def _ruleset(contexts, **overrides) -> dict:
    data = {
        "name": "test",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {}},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {
                            "context": c,
                            "integration_id": ruleset_check.GITHUB_ACTIONS_APP_ID,
                        }
                        for c in contexts
                    ]
                },
            },
        ],
    }
    data.update(overrides)
    return data


class JobContextTest(unittest.TestCase):
    def test_expands_inline_lists_as_a_cartesian_product(self):
        workflow = _matrix_job(
            "        python: ['3.10', \"3.14\"]  # pinned\n        os: [ubuntu-latest, windows-latest]\n",
            name="Python ${{ matrix.python }} on ${{ matrix.os }}",
        )
        self.assertEqual(
            ruleset_check.job_contexts(workflow),
            [
                "Python 3.10 on ubuntu-latest",
                "Python 3.10 on windows-latest",
                "Python 3.14 on ubuntu-latest",
                "Python 3.14 on windows-latest",
            ],
        )

    def test_expands_include_only_matrix_and_keeps_quoted_commas(self):
        workflow = _matrix_job(
            "        include:\n"
            "          - os: ubuntu-latest  # linux\n"
            '            multiplier: "1.0"\n'
            "          - os: 'windows, latest'\n"
            '            multiplier: "1.5"\n'
        )
        self.assertEqual(
            ruleset_check.job_contexts(workflow),
            ["J ubuntu-latest", "J windows, latest"],
        )

    def test_uses_job_id_without_name_and_strips_name_comment(self):
        workflow = "jobs:\n  lint:\n    runs-on: x\n  build:\n    name: Build  # main build\n    runs-on: x\n"
        self.assertEqual(ruleset_check.job_contexts(workflow), ["lint", "Build"])

    def test_strips_comments_after_quoted_values_and_inline_lists(self):
        workflow = _matrix_job(
            "        include:\n          - os: '3.10'  # note\n",
            name="J ${{ matrix.os }}",
        )
        self.assertEqual(ruleset_check.job_contexts(workflow), ["J 3.10"])
        workflow = _matrix_job("        os: [a, b]  # see [docs]\n")
        self.assertEqual(ruleset_check.job_contexts(workflow), ["J a", "J b"])

    def test_rejects_forms_it_cannot_predict(self):
        cases = {
            "exclude": (
                _matrix_job(
                    "        os: [a, b]\n        exclude:\n          - os: a\n"
                ),
                "not supported",
            ),
            "mixed include": (
                _matrix_job("        os: [a]\n        include:\n          - os: b\n"),
                "mixes",
            ),
            "block list": (
                _matrix_job("        os:\n          - a\n"),
                "not supported",
            ),
            "unquoted number": (_matrix_job("        os: [3.10]\n"), "quote the value"),
            "empty list": (_matrix_job("        os: []\n"), "empty matrix list"),
            "matrix without name": (
                "jobs:\n  j:\n    runs-on: x\n    strategy:\n      matrix:\n        os: [a, b]\n",
                "without name",
            ),
            "undefined key": (_matrix_job("        python: [a]\n"), "matrix.os"),
            "reusable workflow": (
                "jobs:\n  j:\n    uses: org/repo/.github/workflows/x.yml@v1\n",
                "reusable",
            ),
            "job-level if": (
                "jobs:\n  j:\n    name: J\n    if: github.event_name == 'push'\n",
                "job-level if",
            ),
            "other expression": (
                "jobs:\n  j:\n    name: J ${{ github.ref }}\n",
                "non-matrix expression",
            ),
            "no jobs": ("on: push\n", "no top-level jobs"),
            "hex number": (_matrix_job("        os: [0x1F]\n"), "quote the value"),
            "infinity": (_matrix_job("        os: [.inf]\n"), "quote the value"),
            "comment as value": (
                _matrix_job("        include:\n          - os: # missing\n"),
                "empty value",
            ),
            "YAML syntax in plain value": (
                _matrix_job("        os: [&anchor]\n"),
                "YAML syntax",
            ),
            "flow matrix": (
                "jobs:\n  j:\n    name: J ${{ matrix.os }}\n    strategy:\n      matrix: {os: [a, b]}\n",
                "inline matrix",
            ),
            "fromJSON matrix": (
                "jobs:\n  j:\n    name: J ${{ matrix.os }}\n    strategy:\n"
                "      matrix: ${{ fromJSON(needs.x.outputs.m) }}\n",
                "inline matrix",
            ),
            "flow strategy": (
                "jobs:\n  j:\n    name: J\n    strategy: {matrix: {os: [a, b]}}\n",
                "inline matrix",
            ),
            "fixed name over a matrix": (
                _matrix_job("        os: [a, b]\n", name="Same"),
                "same name",
            ),
            "block scalar name": (
                "jobs:\n  j:\n    name: >-\n      Build\n",
                "YAML syntax",
            ),
            "anchored name": ("jobs:\n  j:\n    name: &n Build\n", "YAML syntax"),
            "merge key in job": (
                "jobs:\n  j:\n    <<: *defaults\n    name: J\n",
                "unsupported key",
            ),
            "quoted job key": ('jobs:\n  "j":\n    name: J\n', "not supported"),
        }
        for label, (workflow, reason) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ruleset_check.RulesetCheckError, reason):
                    ruleset_check.job_contexts(workflow)


class ExpectedContextsTest(unittest.TestCase):
    def _workflows(self, directory: str, texts: dict[str, str]) -> Path:
        workflows = Path(directory)
        for name, text in texts.items():
            (workflows / name).write_text(text, encoding="utf-8")
        return workflows

    def test_collects_required_workflows_and_rejects_missing_or_duplicate_names(self):
        required = ("a.yml", "b.yml")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                ruleset_check,
                "REQUIRED_WORKFLOW_PATHS",
                frozenset(f".github/workflows/{n}" for n in required),
            ),
        ):
            workflows = self._workflows(
                directory,
                {
                    "a.yml": "jobs:\n  x:\n    name: Lint\n",
                    "b.yml": "jobs:\n  y:\n    name: Test\n",
                    "not-required.yml": "jobs:\n  z:\n    name: Extra\n",
                },
            )
            self.assertEqual(
                ruleset_check.expected_contexts(workflows), {"Lint", "Test"}
            )
            (workflows / "b.yml").write_text(
                "jobs:\n  y:\n    name: Lint\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ruleset_check.RulesetCheckError, "reported by both"
            ):
                ruleset_check.expected_contexts(workflows)
            (workflows / "b.yml").write_text(
                "jobs:\n  y:\n    name: Test\n  z:\n    name: Test\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ruleset_check.RulesetCheckError, "twice in b.yml"
            ):
                ruleset_check.expected_contexts(workflows)
            (workflows / "b.yml").unlink()
            with self.assertRaisesRegex(
                ruleset_check.RulesetCheckError, "required workflow is missing"
            ):
                ruleset_check.expected_contexts(workflows)


class RulesetContextsTest(unittest.TestCase):
    def _check(self, data) -> set[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ruleset.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return ruleset_check.ruleset_contexts(path)

    def test_accepts_valid_ruleset(self):
        self.assertEqual(self._check(_ruleset(["a", "b"])), {"a", "b"})

    def test_rejects_weakened_or_malformed_rulesets(self):
        two_status_rules = _ruleset(["a"])
        two_status_rules["rules"].append(dict(two_status_rules["rules"][-1]))
        unpinned = _ruleset(["a"])
        unpinned["rules"][-1]["parameters"]["required_status_checks"][0].pop(
            "integration_id"
        )

        def without_rule(rule_type):
            data = _ruleset(["a"])
            data["rules"] = [r for r in data["rules"] if r["type"] != rule_type]
            return data

        wrong_app = _ruleset(["a"])
        wrong_app["rules"][-1]["parameters"]["required_status_checks"][0][
            "integration_id"
        ] = 1
        cases = {
            "missing non_fast_forward rule": (
                without_rule("non_fast_forward"),
                "non_fast_forward",
            ),
            "missing pull_request rule": (without_rule("pull_request"), "pull_request"),
            "other app id": (wrong_app, "integration_id"),
            "tag target": (_ruleset(["a"], target="tag"), "default branch"),
            "include as string": (
                _ruleset(
                    ["a"], conditions={"ref_name": {"include": "~DEFAULT_BRANCH"}}
                ),
                "default branch",
            ),
            "excluded branch": (
                _ruleset(
                    ["a"],
                    conditions={
                        "ref_name": {
                            "include": ["~DEFAULT_BRANCH"],
                            "exclude": ["main"],
                        }
                    },
                ),
                "exclude",
            ),
            "conditions not an object": (
                _ruleset(["a"], conditions=[]),
                "unexpected structure",
            ),
            "disabled": (_ruleset(["a"], enforcement="disabled"), "enforcement"),
            "other branch": (
                _ruleset(
                    ["a"], conditions={"ref_name": {"include": ["refs/heads/dev"]}}
                ),
                "default branch",
            ),
            "bypass": (
                _ruleset(
                    ["a"],
                    bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole"}],
                ),
                "bypass",
            ),
            "missing deletion rule": (without_rule("deletion"), "deletion"),
            "two status rules": (two_status_rules, "exactly one"),
            "no checks": (_ruleset([]), "non-empty"),
            "duplicate context": (_ruleset(["a", "a"]), "unique"),
            "not pinned to Actions": (unpinned, "integration_id"),
            "not an object": ([], "must be an object"),
        }
        for label, (data, reason) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ruleset_check.RulesetCheckError, reason):
                    self._check(data)


class MainTest(unittest.TestCase):
    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ruleset_check.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_repository_ruleset_matches_required_workflow_jobs(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0, out)

    def test_reports_renamed_job_and_stale_ruleset_entry(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                ruleset_check,
                "REQUIRED_WORKFLOW_PATHS",
                frozenset({".github/workflows/ci.yml"}),
            ),
        ):
            workflows = Path(directory)
            (workflows / "ci.yml").write_text(
                "jobs:\n  q:\n    name: Python lint\n", encoding="utf-8"
            )
            ruleset = workflows / "ruleset.json"
            ruleset.write_text(
                json.dumps(_ruleset(["Python quality"])), encoding="utf-8"
            )
            code, out, _ = self._run(
                "--ruleset", str(ruleset), "--workflows", str(workflows)
            )
        self.assertEqual(code, 1)
        self.assertIn("missing from ruleset: Python lint", out)
        self.assertIn(
            "ruleset requires a check no workflow reports: Python quality", out
        )

    def test_returns_2_for_unreadable_inputs(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                ruleset_check,
                "REQUIRED_WORKFLOW_PATHS",
                frozenset({".github/workflows/ci.yml"}),
            ),
        ):
            folder = Path(directory)
            (folder / "ci.yml").write_text(
                "jobs:\n  q:\n    name: Lint\n", encoding="utf-8"
            )
            (folder / "bad.yml").write_text(
                "jobs:\n  q:\n    uses: org/r/w.yml@v1\n", encoding="utf-8"
            )
            broken = folder / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            not_utf8 = folder / "not-utf8.json"
            not_utf8.write_bytes(b"\xff\xfe{")
            unsupported = folder / "unsupported"
            unsupported.mkdir()
            (unsupported / "ci.yml").write_text(
                "jobs:\n  q:\n    uses: org/r/w.yml@v1\n", encoding="utf-8"
            )
            for argv in (
                ["--ruleset", str(broken), "--workflows", str(folder)],
                ["--ruleset", str(folder / "absent.json"), "--workflows", str(folder)],
                ["--ruleset", str(not_utf8), "--workflows", str(folder)],
                ["--workflows", str(unsupported)],
            ):
                with self.subTest(argv=argv):
                    code, _, err = self._run(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn("RULESET CHECK ERROR", err)

    def test_print_expected_matches_the_ruleset_file(self):
        code, out, _ = self._run("--print-expected")
        self.assertEqual(code, 0)
        data = json.loads(
            (REPO_ROOT / ".github" / "rulesets" / "main-protection.json").read_text(
                encoding="utf-8"
            )
        )
        status_rule = next(
            r for r in data["rules"] if r["type"] == "required_status_checks"
        )
        self.assertEqual(
            json.loads(out), status_rule["parameters"]["required_status_checks"]
        )


if __name__ == "__main__":
    unittest.main()
