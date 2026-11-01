"""Lightweight local-link and Mermaid-envelope checks for public guide pages.

This extends the established README/source navigation test; it does not replace
code-derived documentation generation or claim to validate Mermaid semantics.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.documentation_check import (
    FENCE,
    mermaid_errors as _mermaid_errors,
    pages_to_check as _pages_to_check,
    relative_link_errors as _relative_link_errors,
)


class SourceNavigationTests(unittest.TestCase):
    def test_valid_markdown_link_destinations_and_code_examples(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            page = root / "guide.md"
            for name in ("target.md", "with space.md", "with(parens).md", "with&.md"):
                (root / name).write_text("content", encoding="utf-8")
            valid = """[title](target.md "説明")
[single](target.md '説明')
[title-example](target.md "Ignore [example](missing-title.md)")
[angle](<with space.md> "説明")
[balanced](with(parens).md)
[entity](with&amp;.md)
`[example](missing-inline.md)`
``[example](missing-two-backticks.md)``
~~~~markdown
[example](missing-fenced.md)
~~~
~~~~
````markdown
```mermaid
[example](missing-nested.md)
```
````
[target][ref]
[ref]: target.md "参照先"
[another]: target.md "Ignore [example](missing-reference-title.md)"
"""
            self.assertEqual(_relative_link_errors(page, root, valid), [])

    def test_reference_links_and_escaped_destinations_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            page = root / "guide.md"
            (root / "with(parens).md").write_text("content", encoding="utf-8")
            self.assertEqual(
                _relative_link_errors(page, root, r"[ok](with\(parens\).md)"), []
            )
            text = '[item][reference]\n[reference]: missing.md "Title"\n'
            self.assertTrue(
                any(
                    "missing path" in error
                    for error in _relative_link_errors(page, root, text)
                )
            )

    def test_footnote_text_is_not_a_link_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                _relative_link_errors(
                    root / "page.md",
                    root,
                    "Text[^note].\n\n[^note]: Ordinary explanatory prose.",
                ),
                [],
            )

    def test_valid_fence_lengths_and_mermaid_diagram_types(self):
        self.assertEqual(
            _mermaid_errors("````markdown\n```mermaid\nwrongDiagram\n```\n````"), []
        )
        self.assertEqual(
            _mermaid_errors("~~~mermaid\nclassDiagram\nclass Example\n~~~~"), []
        )
        self.assertEqual(
            _mermaid_errors("```mermaid\nstateDiagram-v2\n[*] --> Ready\n```"), []
        )

    def test_mermaid_comments_quotes_and_shapes_do_not_create_fake_nodes(self):
        text = """```mermaid
%% 処理の概要
flowchart TD
%% A[旧表示]
A["X[状態]"] --> B["X[結果]"]
C[ラベルの X(説明)] --> A
A -->|X[補足]| B
B -->|X[補足]| C
```
"""
        self.assertEqual(_mermaid_errors(text), [])
        self.assertEqual(
            _mermaid_errors(
                "```mermaid\n---\ntitle: Example\n---\nflowchart LR\nA[Player's choice] --> B[Result]\n```"
            ),
            [],
        )
        errors = _mermaid_errors(
            "```mermaid\nflowchart LR\nA[Player's choice]\nA[Result]\n```"
        )
        self.assertTrue(any("repeated Mermaid node" in error for error in errors))

    def test_unclosed_tilde_fence_is_the_reported_error(self):
        errors = _mermaid_errors("~~~mermaid\nflowchart TD\nA[入力]")
        self.assertEqual(len(errors), 1)
        self.assertIn("unclosed code fence", errors[0])

    def test_tilde_mermaid_and_parenthesis_duplicates_are_checked(self):
        errors = _mermaid_errors("~~~mermaid\nwrongDiagram\n~~~")
        self.assertTrue(any("unknown Mermaid" in error for error in errors))
        for first, second in (("A(一つ)", "A(二つ)"), ("A((一つ))", "A[二つ]")):
            with self.subTest(first=first, second=second):
                errors = _mermaid_errors(
                    f"```mermaid\nflowchart TD\n{first}\n{second}\n```"
                )
                self.assertTrue(
                    any("repeated Mermaid node" in error for error in errors)
                )

    def test_existing_runtime_guide_is_required(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn(
            root / "doc/architecture/runtime_layers.md", _pages_to_check(root)
        )

    def test_relative_markdown_links_point_to_existing_paths(self):
        root = Path(__file__).resolve().parents[1]
        pages = _pages_to_check(root)
        self.assertIn(root / "README.md", pages)
        self.assertIn(root / "src/ai_game_player/README.md", pages)
        for page in pages:
            text = page.read_text(encoding="utf-8")
            with self.subTest(page=page.relative_to(root)):
                errors = _relative_link_errors(page, root, text)
                self.assertEqual(
                    errors, [], "Invalid local Markdown links: " + "; ".join(errors)
                )

    def test_public_guide_diagram_fences(self):
        root = Path(__file__).resolve().parents[1]
        for page in _pages_to_check(root):
            if (
                page == root / "README.md"
                or page == root / "src/ai_game_player/README.md"
            ):
                continue
            with self.subTest(page=page.relative_to(root)):
                errors = _mermaid_errors(page.read_text(encoding="utf-8"))
                self.assertEqual(
                    errors, [], "Invalid Mermaid block: " + "; ".join(errors)
                )

    def test_invalid_local_links_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            page = root / "guide.md"
            page.write_text(
                "[missing](missing.md)\n[escape](../secret.md)", encoding="utf-8"
            )
            errors = _relative_link_errors(page, root, page.read_text(encoding="utf-8"))
            self.assertEqual(len(errors), 2)
            self.assertTrue(any("missing path" in error for error in errors))
            self.assertTrue(any("escapes repository" in error for error in errors))

    def test_invalid_mermaid_fences_and_nodes_fail(self):
        self.assertTrue(
            _mermaid_errors("~~~markdown\n" + FENCE + "mermaid\nflowchart TD")
        )
        self.assertTrue(_mermaid_errors(FENCE + "mermaid\nwrongDiagram\n" + FENCE))
        duplicated = FENCE + "mermaid\nflowchart TD\nA[一つ]\nA[二つ]\n" + FENCE
        self.assertTrue(
            any("repeated" in error for error in _mermaid_errors(duplicated))
        )
        valid = FENCE + "mermaid\nflowchart LR\nA[最初] --> B[次]\nB --> A\n" + FENCE
        self.assertEqual(_mermaid_errors(valid), [])


class NavigationParserTests(unittest.TestCase):
    def test_titles_spaces_parentheses_and_reference_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target (one).md").touch()
            page = root / "guide.md"
            valid = "\n".join(
                [
                    '[title](target%20(one).md "説明")',
                    "[title](<target (one).md> '説明')",
                    "[reference][r]",
                    '[r]: <target (one).md> "説明"',
                ]
            )
            self.assertEqual(_relative_link_errors(page, root, valid), [])
            broken = '[missing](missing.md "title")\n[x][r]\n[r]: absent.md\n[out](../outside.md)'
            errors = _relative_link_errors(page, root, broken)
            self.assertEqual(len(errors), 3)
            self.assertTrue(any("escapes repository" in error for error in errors))

    def test_code_examples_are_not_links(self):
        text = "`[x](missing.md)`\n``[x](missing.md)``\n~~~md\n[x](missing.md)\n~~~"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_relative_link_errors(root / "guide.md", root, text), [])

    def test_long_fences_protect_inner_examples(self):
        text = "````markdown\n```mermaid\nnotADiagram\n```\n[x](missing.md)\n````"
        self.assertEqual(_mermaid_errors(text), [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(_relative_link_errors(root / "guide.md", root, text), [])

    def test_unclosed_tilde_fence_has_specific_error(self):
        self.assertEqual(
            _mermaid_errors("~~~mermaid\nflowchart TD"),
            ["line 1: unclosed code fence: mermaid"],
        )

    def test_wrong_or_short_closer_does_not_close(self):
        for closer in ("~~~", "``"):
            with self.subTest(closer=closer):
                self.assertTrue(
                    any(
                        "unclosed" in error
                        for error in _mermaid_errors(
                            "```mermaid\nflowchart TD\n" + closer
                        )
                    )
                )
        self.assertEqual(_mermaid_errors("~~~mermaid\nflowchart TD\n~~~~"), [])

    def test_invalid_diagram_inside_tilde_fence(self):
        self.assertEqual(
            _mermaid_errors("~~~mermaid\nwrongDiagram\n~~~"),
            ["line 3: unknown Mermaid opening directive"],
        )

    def test_class_state_and_sequence_diagrams(self):
        for directive in (
            "classDiagram",
            "stateDiagram-v2",
            "sequenceDiagram",
            "erDiagram",
            "pie",
        ):
            with self.subTest(directive=directive):
                self.assertEqual(
                    _mermaid_errors("```mermaid\n" + directive + "\n```"), []
                )

    def test_parenthesis_and_other_duplicate_nodes(self):
        for shape in ("[一つ]", "{条件}", "(処理)", "((丸))"):
            with self.subTest(shape=shape):
                errors = _mermaid_errors(
                    "```mermaid\nflowchart TD\nA" + shape + "\nA" + shape + "\n```"
                )
                self.assertTrue(any("repeated" in error for error in errors))

    def test_quoted_labels_comments_and_reused_ids_are_not_duplicates(self):
        diagram = 'flowchart TD\nA["B[説明]"] --> B(処理)\nB --> A\n%% A[例]'
        self.assertEqual(_mermaid_errors("```mermaid\n" + diagram + "\n```"), [])

    def test_comments_and_front_matter_before_diagram(self):
        text = "~~~mermaid\n---\ntitle: 説明\n---\n%% comment\nflowchart TD\nA[最初] --> B[次]\n~~~"
        self.assertEqual(_mermaid_errors(text), [])

    def test_required_runtime_page_does_not_silently_disappear(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIn(
                root / "doc/architecture/runtime_layers.md", _pages_to_check(root)
            )

    def test_optional_update_guide_is_scanned_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "doc/contributor/documentation_update_guide.md"
            page.parent.mkdir(parents=True)
            page.touch()
            self.assertIn(page, _pages_to_check(root))


if __name__ == "__main__":
    unittest.main()
