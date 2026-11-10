import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "babysit" / "SKILL.md"
DIGEST = ROOT / "tools" / "pr_digest.py"
FLAG = re.compile(r"(--[a-z][a-z-]*)")
CODE_SPAN = re.compile(r"`([^`\n]+)`")


@unittest.skipUnless(
    DIGEST.is_file(),
    "tools/pr_digest.py (#898) is not in this checkout yet; the contract is checked once the PR is based on main",
)
class BabysitSkillContractTest(unittest.TestCase):
    """babysitスキルが案内するpr_digestの呼び方が、#898の実際のCLIと食い違わないこと。"""

    def skill_text(self):
        return SKILL.read_text(encoding="utf-8")

    def digest_flags(self):
        result = subprocess.run(
            [sys.executable, str(DIGEST), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        # usage行(先頭の数行)にある、-hを除くオプション名。
        return set(FLAG.findall(result.stdout.split("PRの状態", 1)[0]))

    def test_every_flag_the_skill_passes_to_pr_digest_exists(self):
        used = set()
        # pr_digest.pyを呼ぶコードスパン(`...`)の中のオプションだけを見る(同じ行の別コマンドは対象外)。
        for span in CODE_SPAN.findall(self.skill_text()):
            if "pr_digest.py" in span:
                used.update(FLAG.findall(span))
        self.assertTrue(used, "the skill should show at least one pr_digest flag")
        self.assertLessEqual(used, self.digest_flags())

    def test_the_skill_states_the_limits_of_the_digest(self):
        text = self.skill_text()
        for needle in ("get_reviews", "get_comments", "GraphQL", "--rerun-failed"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
