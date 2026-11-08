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
        self.assertIn("[runtime](#group-pkg-runtime)", text)
        self.assertIn("中心の判断を持つ", text)
        self.assertIn("Session wiring.", text)
        self.assertIn("group_misc_top -->|1| group_pkg_runtime", text)
        self.assertIn("責務コメントなし: 0件", text)
        self.assertIn("1: `runtime.session`", text)
        self.assertIn("1: `core`", text)


class ModuleIndexEdgeCaseTest(unittest.TestCase):
    def test_nameless_responsibility_keeps_its_beginning(self):
        self.assertEqual(
            extract_responsibility("# 責務: [名前のない説明: 先頭を落とさない]\n"),
            "名前のない説明: 先頭を落とさない",
        )
        self.assertEqual(
            extract_responsibility("# 責務: [Name：全角コロンの説明]\n"),
            "全角コロンの説明",
        )

    def test_groups_by_shared_prefix_and_keeps_anchors_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "ai_game_player"
            (package / "other").mkdir(parents=True)
            modules = [
                module_entry("ai_game_player", "src/ai_game_player/__init__.py", [], [])
            ]
            for index in range(4):
                (package / f"belief_{index}.py").write_text(
                    '"""Belief."""\n', encoding="utf-8"
                )
                modules.append(
                    module_entry(
                        f"ai_game_player.belief_{index}",
                        f"src/ai_game_player/belief_{index}.py",
                        [],
                        [],
                    )
                )
            (package / "solo.py").write_text('"""Solo."""\n', encoding="utf-8")
            (package / "other" / "x.py").write_text('"""Other."""\n', encoding="utf-8")
            modules.append(
                module_entry(
                    "ai_game_player.solo", "src/ai_game_player/solo.py", [], []
                )
            )
            modules.append(
                module_entry(
                    "ai_game_player.other.x", "src/ai_game_player/other/x.py", [], []
                )
            )
            text = render_module_index(root, json.dumps({"modules": modules}))
        self.assertIn("[belief_*](#group-pfx-belief) | 4", text)
        self.assertIn("[other](#group-pkg-other) | 1", text)
        self.assertIn("[(その他の直下)](#group-misc-top) | 1", text)


class ResponsibilityPriorityTest(unittest.TestCase):
    def test_top_level_multiline_comment_beats_later_method_comment(self):
        text = (
            "# {\n#   責務: [\n#     Thing: 全体の責務\n#   ]\n# }\nclass Thing:\n"
            "    # {\n    #   責務: [run: メソッドの責務]\n    # }\n    def run(self):\n        pass\n"
        )
        self.assertEqual(extract_responsibility(text), "全体の責務")

    def test_multiline_module_docstring_uses_first_content_line(self):
        self.assertEqual(
            extract_responsibility('"""\nFirst line.\n\nMore.\n"""\n'), "First line."
        )


class DocIndexTest(unittest.TestCase):
    def test_summarizes_title_first_sentence_and_front_matter(self):
        text = "Parent: #19\nLast-Checked: 2026-11-08\n\n# 題名 [リンク](a.md)\n\n本文の最初の文です。\n\n| 表 |\n"
        title, summary, front = summarize_document(text)
        self.assertEqual(title, "題名 リンク")
        self.assertEqual(summary, "本文の最初の文です。")
        self.assertEqual(front, {"Parent": "#19", "Last-Checked": "2026-11-08"})

    def test_keeps_underscores_and_cuts_at_first_sentence(self):
        text = "# 題名\n\n`ai_game_player`はrepo_map.jsonを使う。二文目は載せない。\n"
        _, summary, _ = summarize_document(text)
        self.assertEqual(summary, "ai_game_playerはrepo_map.jsonを使う。")

    def test_skips_navigation_links_joins_wrapped_paragraph_and_handles_tilde_fences(
        self,
    ):
        text = "# 題名\n\n[戻る](../README.md)\n\n~~~\n# 例\n~~~\n\n最初の段落は\n折り返されて、ここで終わる。次の文。\n"
        _, summary, _ = summarize_document(text)
        self.assertEqual(summary, "最初の段落は 折り返されて、ここで終わる。")

    def test_encodes_spaces_in_links_and_skips_symlinked_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            (root / "doc").mkdir(parents=True)
            (root / "doc" / "with space.md").write_text(
                "# S\n\nbody.\n", encoding="utf-8"
            )
            outside = Path(directory) / "private.md"
            outside.write_text("# Private\n\nsecret.\n", encoding="utf-8")
            (root / "doc" / "link.md").symlink_to(outside)
            text = render_doc_index(root)
        self.assertIn("(../doc/with%20space.md)", text)
        self.assertNotIn("Private", text)

    def test_excludes_by_relative_path_not_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "generated" / "project"
            (root / "doc").mkdir(parents=True)
            (root / "doc" / "a.md").write_text("# A\n\nfirst.\n", encoding="utf-8")
            text = render_doc_index(root)
        self.assertIn("doc/a.md", text)

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
            "```\n# コード内の見出し\n[隠れ](hidden.md) `HiddenName`\n```\n[他](other.md) と #864。\n#865 の行\n"
        )
        hints = extract_hints(text)
        self.assertIn("## 流れ", hints)
        self.assertIn("| 呼ぶ | `MacroTrigger` |", hints)
        self.assertIn("- other.md", hints)
        self.assertIn("- #864", hints)
        self.assertIn("- MacroTrigger", hints)
        self.assertNotIn("コード内の見出し", hints)
        self.assertNotIn("hidden.md", hints)
        self.assertNotIn("HiddenName", hints)
        self.assertNotIn("#865 の行", hints.split("## 表の行")[0])


if __name__ == "__main__":
    unittest.main()
