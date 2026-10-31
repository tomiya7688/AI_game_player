"""固定ソース取得が未検証パスや利用者の変更を削除しないことを確認します。"""

import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools import fetch_ci_games


class RetrySharingViolationTemporaryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        for attempt in range(121):
            try:
                super().cleanup()
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) != 32 or attempt == 120:
                    raise
                time.sleep(0.25)


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

    def test_existing_checkout_with_foreign_origin_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            replies = [str(target), "https://github.com/other/game.git"]
            with (
                mock.patch.object(fetch_ci_games, "run", side_effect=replies),
                mock.patch.object(fetch_ci_games.shutil, "rmtree") as remove,
            ):
                with self.assertRaisesRegex(
                    ValueError, "not the expected source checkout"
                ):
                    fetch_ci_games.materialize(self.source(), root, True)
                remove.assert_not_called()

    def test_real_index_flags_preserve_hidden_tracked_edits(self):
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with (
                self.subTest(flag=flag),
                RetrySharingViolationTemporaryDirectory() as directory,
            ):
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
                saved = target / "LICENSE"
                saved.write_text("original", encoding="utf-8")
                fetch_ci_games.run("git", "add", "LICENSE", cwd=target)
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
                game = self.source()
                game["commit"] = fetch_ci_games.run(
                    "git", "rev-parse", "HEAD", cwd=target
                )
                fetch_ci_games.run("git", "update-index", flag, "LICENSE", cwd=target)
                saved.write_text("hidden user edit", encoding="utf-8")
                self.assertEqual(
                    fetch_ci_games.run("git", "status", "--porcelain", cwd=target), ""
                )
                with mock.patch.object(fetch_ci_games.shutil, "rmtree") as remove:
                    with self.assertRaisesRegex(
                        ValueError, "skip-worktree or assume-unchanged"
                    ):
                        fetch_ci_games.materialize(game, root, True)
                    remove.assert_not_called()
                self.assertEqual(saved.read_text(encoding="utf-8"), "hidden user edit")

    def test_changed_pin_message_preserves_existing_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            replies = [
                str(target),
                "https://github.com/example/game.git",
                "H LICENSE",
                "",
                "b" * 40,
            ]
            with mock.patch.object(fetch_ci_games, "run", side_effect=replies):
                with self.assertRaisesRegex(RuntimeError, "choose a new --dest"):
                    fetch_ci_games.materialize(self.source(), root, False)

    def test_clean_preserves_committed_local_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            for head, extra in (("b" * 40, ""), ("a" * 40, "b" * 40)):
                with self.subTest(head=head, extra=extra):
                    replies = [
                        str(target),
                        "https://github.com/example/game.git",
                        "H LICENSE",
                        "",
                        head,
                    ]
                    if head == "a" * 40:
                        replies.append(extra)
                    with (
                        mock.patch.object(fetch_ci_games, "run", side_effect=replies),
                        mock.patch.object(fetch_ci_games.shutil, "rmtree") as remove,
                    ):
                        with self.assertRaisesRegex(ValueError, "local history"):
                            fetch_ci_games.materialize(self.source(), root, True)
                        remove.assert_not_called()

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
                "H LICENSE",
                "?? user.txt",
            ]
            with mock.patch.object(fetch_ci_games, "run", side_effect=replies):
                with self.assertRaisesRegex(ValueError, "local changes"):
                    fetch_ci_games.materialize(self.source(), root, True)
            self.assertEqual(saved.read_text(encoding="utf-8"), "keep")

    def test_real_ignored_file_blocks_clean_before_any_deletion(self):
        with RetrySharingViolationTemporaryDirectory() as directory:
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

    def test_existing_lock_is_preserved_before_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "manifest-lock.json"
            lock.write_text("user data", encoding="utf-8")
            with (
                mock.patch.object(sys, "argv", ["fetch_ci_games", "--dest", str(root)]),
                mock.patch.object(fetch_ci_games, "materialize") as materialize,
            ):
                with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                    fetch_ci_games.main()
                materialize.assert_not_called()
            self.assertEqual(lock.read_text(encoding="utf-8"), "user data")

    def test_lock_is_reserved_before_materialize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "manifest-lock.json"

            def fetch(game, dest, clean):
                with self.assertRaises(FileExistsError):
                    lock.open("x", encoding="utf-8")
                return {"id": game["id"]}

            with (
                mock.patch.object(
                    sys, "argv", ["fetch_ci_games", "--level", "1", "--dest", str(root)]
                ),
                mock.patch.object(fetch_ci_games, "materialize", side_effect=fetch),
            ):
                self.assertEqual(fetch_ci_games.main(), 0)
            self.assertIn('"id": "2048"', lock.read_text(encoding="utf-8"))

    def test_clean_checkout_returns_fixed_commit_and_external_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "test-game"
            target.mkdir()
            (target / "LICENSE").write_text("license", encoding="utf-8")
            replies = [
                str(target),
                "https://github.com/example/game.git",
                "H LICENSE",
                "",
                "a" * 40,
            ]
            with mock.patch.object(fetch_ci_games, "run", side_effect=replies):
                result = fetch_ci_games.materialize(self.source(), root, False)
            self.assertEqual(result["commit"], "a" * 40)
            self.assertEqual(Path(result["path"]), target)
