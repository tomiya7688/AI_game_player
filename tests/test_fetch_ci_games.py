"""固定ソース取得が未検証パスや利用者の変更を削除しないことを確認します。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import fetch_ci_games


class ReferenceGameFetchTest(unittest.TestCase):
    def source(self):
        return {
            "id": "test-game",
            "repository": "example/game",
            "commit": "a" * 40,
            "level": 1,
            "license_spdx": "MIT",
            "license_paths": ["LICENSE"],
        }

    def test_invalid_identifiers_are_rejected_before_clean(self):
        for field, value in (
            ("id", "../outside"),
            ("id", "C:/outside"),
            ("repository", "--upload-pack=evil"),
            ("commit", "main"),
        ):
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                game = self.source()
                game[field] = value
                with mock.patch.object(fetch_ci_games.shutil, "rmtree") as remove:
                    with self.assertRaises(ValueError):
                        fetch_ci_games.materialize(game, Path(directory), True)
                    remove.assert_not_called()

    def test_license_paths_cannot_escape_the_checkout(self):
        for value in ("../LICENSE", "/outside", "C:/outside", "file:stream", ""):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                game = self.source()
                game["license_paths"] = [value]
                with self.assertRaises(ValueError):
                    fetch_ci_games.validate_game_target(game, Path(directory))

    def test_existing_foreign_directory_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            saved = target / "user.txt"
            saved.write_text("keep", encoding="utf-8")
            with mock.patch.object(fetch_ci_games, "run", return_value=str(root)):
                with self.assertRaises(ValueError):
                    fetch_ci_games.materialize(self.source(), root, True)
            self.assertEqual(saved.read_text(encoding="utf-8"), "keep")

    def test_dirty_checkout_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            saved = target / "user.txt"
            saved.write_text("keep", encoding="utf-8")
            replies = [
                str(target),
                "https://github.com/example/game.git",
                "?? user.txt",
            ]
            with mock.patch.object(fetch_ci_games, "run", side_effect=replies):
                with self.assertRaisesRegex(ValueError, "local changes"):
                    fetch_ci_games.materialize(self.source(), root, True)
            self.assertEqual(saved.read_text(encoding="utf-8"), "keep")

    def test_real_ignored_file_blocks_clean_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            fetch_ci_games.run("git", "init", cwd=target)
            fetch_ci_games.run(
                "git",
                "remote",
                "add",
                "origin",
                "https://github.com/example/game.git",
                cwd=target,
            )
            (target / ".gitignore").write_text("user.txt\n", encoding="utf-8")
            fetch_ci_games.run("git", "add", ".gitignore", cwd=target)
            fetch_ci_games.run(
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "fixture",
                cwd=target,
            )
            saved = target / "user.txt"
            saved.write_text("keep ignored data", encoding="utf-8")
            with mock.patch.object(
                fetch_ci_games.shutil,
                "rmtree",
                side_effect=AssertionError("unsafe deletion"),
            ):
                with self.assertRaisesRegex(ValueError, "local changes"):
                    fetch_ci_games.materialize(self.source(), root, True)
            self.assertEqual(saved.read_text(encoding="utf-8"), "keep ignored data")

    def test_clean_checkout_returns_fixed_commit_and_external_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            (target / "LICENSE").write_text("license", encoding="utf-8")
            replies = [str(target), "https://github.com/example/game.git", "", "a" * 40]
            with mock.patch.object(fetch_ci_games, "run", side_effect=replies):
                result = fetch_ci_games.materialize(self.source(), root, False)
            self.assertEqual(result["commit"], "a" * 40)
            self.assertEqual(Path(result["path"]), target)
