import json
import tempfile
import unittest
from pathlib import Path

from tools.diagram_hints import extract_hints
from tools.generate_doc_index import render_doc_index, summarize_document
from tools.generate_module_index import extract_responsibility, render_module_index


def module_entry(
    name: str, path: str, dependencies: list[str], classes: list[str]
) -> dict:
    symbols = [{"kind": "class", "name": c, "qualified_name": c} for c in classes]
    return {
        "module": name,
        "path": path,
        "dependencies": dependencies,
        "symbols": symbols,
    }


class ModuleIndexTest(unittest.TestCase):
    def test_extracts_responsibility_from_structured_comment_then_docstring(self):
        commented = "# {\n#   責務: [Pipeline: 画面から実行までを接続する]\n# }\nclass Pipeline:\n    pass\n"
        self.assertEqual(
            extract_responsibility(commented), "画面から実行までを接続する"
        )
        self.assertEqual(
            extract_responsibility('"""Evaluates a thing."""\n'), "Evaluates a thing."
        )
        self.assertEqual(extract_responsibility("x = 1\n"), "(責務コメントなし)")

    def test_renders_toc_responsibility_table_and_dependency_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "ai_game_player" / "runtime"
            source.mkdir(parents=True)
            (root / "src" / "ai_game_player" / "core.py").write_text(
                "# {\n#   責務: [Core: 中心の判断を持つ]\n# }\nclass Core:\n    pass\n",
                encoding="utf-8",
            )
            (source / "session.py").write_text(
                '"""Session wiring."""\n', encoding="utf-8"
            )
            repo_map = {
                "modules": [
                    module_entry(
                        "ai_game_player", "src/ai_game_player/__init__.py", [], []
                    ),
                    module_entry(
                        "ai_game_player.core",
                        "src/ai_game_player/core.py",
                        ["ai_game_player.runtime.session"],
                        ["Core"],
                    ),
                    module_entry(
                        "ai_game_player.runtime.session",
                        "src/ai_game_player/runtime/session.py",
                        [],
                        [],
                    ),
                ]
            }
            text = render_module_index(root, json.dumps(repo_map))
        self.assertIn("[runtime](#group-runtime)", text)
        self.assertIn("中心の判断を持つ", text)
        self.assertIn("Session wiring.", text)
        self.assertIn("group_other -->|1| group_runtime", text)
        self.assertIn("責務コメントなし: 0件", text)


class DocIndexTest(unittest.TestCase):
    def test_summarizes_title_first_sentence_and_front_matter(self):
        text = "Parent: #19\nLast-Checked: 2026-11-08\n\n# 題名 [リンク](a.md)\n\n本文の最初の文です。\n\n| 表 |\n"
        title, summary, front = summarize_document(text)
        self.assertEqual(title, "題名 リンク")
        self.assertEqual(summary, "本文の最初の文です。")
        self.assertEqual(front, {"Parent": "#19", "Last-Checked": "2026-11-08"})

    def test_renders_one_row_per_document_and_skips_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "doc" / "generated").mkdir(parents=True)
            (root / "doc" / "a.md").write_text("# A\n\nfirst.\n", encoding="utf-8")
            (root / "doc" / "generated" / "x.md").write_text("# X\n", encoding="utf-8")
            text = render_doc_index(root)
        self.assertIn("doc/a.md", text)
        self.assertNotIn("doc/generated/x.md", text)


class DiagramHintsTest(unittest.TestCase):
    def test_extracts_headings_table_rows_links_issues_and_names(self):
        text = (
            "# 設計\n## 流れ\n| 責務 | 担当 |\n|---|---|\n| 呼ぶ | `MacroTrigger` |\n"
            "```\n# コード内の見出し\n```\n[他](other.md) と #864。\n"
        )
        hints = extract_hints(text)
        self.assertIn("## 流れ", hints)
        self.assertIn("| 呼ぶ | `MacroTrigger` |", hints)
        self.assertIn("- other.md", hints)
        self.assertIn("- #864", hints)
        self.assertIn("- MacroTrigger", hints)
        self.assertNotIn("コード内の見出し", hints)


if __name__ == "__main__":
    unittest.main()
