import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pr_digest", Path(__file__).resolve().parents[1] / "tools" / "pr_digest.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BOT_BODY = (
    '<!-- x --> **<picture><img src="a.svg"></picture> Major** · **Stability**\n'
    "<details><summary>Supported by static analysis</summary>\n"
    "Script executed:\n\n```bash\ncat finish_task.sh\n```\n\nLength of output: 4050\n\n---\n\n"
    "</details>\n\n**公開するJSONのサイズを検査してください。**\n\n詳しい説明…"
)


def comment(identifier, login, body="x", reply_to=None):
    return {
        "id": identifier, "user": {"login": login}, "body": body, "path": "a.py", "line": 3,
        "in_reply_to_id": reply_to,
    }


class PrDigestTest(unittest.TestCase):
    def test_check_counts_and_failed_names(self):
        runs = [
            {"name": "a", "conclusion": "success"},
            {"name": "b", "conclusion": "failure"},
            {"name": "c", "conclusion": None, "status": "queued"},
        ]
        counts, failed = MODULE.count_checks(runs)
        self.assertEqual(counts, {"success": 1, "failure": 1, "queued": 1})
        self.assertEqual(failed, ["b"])

    def test_headline_skips_analysis_logs(self):
        self.assertEqual(MODULE.headline(BOT_BODY), "公開するJSONのサイズを検査してください。")
        self.assertEqual(MODULE.severity(BOT_BODY), "Major")

    def test_bold_conclusion_wins_over_noisy_lines(self):
        body = "Web query:\n\n`x`\n\n**結論の見出しです**\n\n本文 **強調** あり"
        self.assertEqual(MODULE.headline(body), "結論の見出しです")

    def test_headline_is_bounded_and_has_a_fallback(self):
        self.assertEqual(len(MODULE.headline("x" * 500)), MODULE.HEADLINE_LIMIT)
        self.assertEqual(MODULE.headline(""), "(no headline)")

    def test_findings_track_human_replies_only(self):
        comments = [
            comment(1, "coderabbitai[bot]", BOT_BODY),
            comment(2, "coderabbitai[bot]", BOT_BODY),
            comment(3, "owner", "対応しません", reply_to=1),
            comment(4, "coderabbitai[bot]", "withdrawn", reply_to=2),
        ]
        findings = MODULE.inline_findings(comments)
        self.assertEqual([(f["id"], f["answered"]) for f in findings], [(1, True), (2, False)])

    def test_pagination_stops_on_a_short_page_and_fails_when_incomplete(self):
        pages = [[{}] * 100, [{}] * 3]
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: pages.pop(0)
        try:
            self.assertEqual(len(MODULE.fetch_pages("o/r", "x")), 103)
        finally:
            MODULE.gh_json = original
        MODULE.gh_json = lambda args: [{}] * 100
        try:
            with self.assertRaises(ValueError):
                MODULE.fetch_pages("o/r", "x")
        finally:
            MODULE.gh_json = original


if __name__ == "__main__":
    unittest.main()
