from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.run_control import ExecutionCancelled, RunController
from tools.browser_probe_support import export_game_assets, publish_validation_report
from tools import browser_probe_support
from tools.check_2048_screen_transition import BrowserKeyExecutor, BrowserScreenSource
from tools.probe_browser_game import BrowserClickExecutor


class BrowserProbeSupportTests(unittest.TestCase):
    def test_export_uses_committed_allowlisted_assets_only(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            files = {
                "index.html": "<script src='game.js'></script>",
                "game.js": "window.running = true;",
                "style.css": "body {}",
                ".env": "hidden tracked secret",
                "private.pem": "not an asset",
                ".gitignore": "ignored.js\nlocal.env\n",
            }
            for name, content in files.items():
                (source / name).write_text(content)

            def git(*arguments):
                return (
                    subprocess.check_output(
                        ["git", "-C", str(source), *arguments],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )

            git("init")
            git("add", ".")
            git(
                "-c",
                "user.name=Probe Test",
                "-c",
                "user.email=probe@example.invalid",
                "commit",
                "-m",
                "fixture",
            )
            (source / "ignored.js").write_text("ignored local secret")
            (source / "local.env").write_text("ignored local secret")
            destination = Path(directory) / "public"
            export_game_assets(source, git("rev-parse", "HEAD"), destination)
            self.assertEqual(
                {p.name for p in destination.iterdir()},
                {"index.html", "game.js", "style.css"},
            )
            self.assertEqual((destination / "game.js").read_text(), files["game.js"])

    def test_concurrent_reports_retain_both_complete_results(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            with ThreadPoolExecutor(max_workers=2) as pool:
                archives = list(
                    pool.map(
                        lambda value: publish_validation_report(target, {"run": value}),
                        [1, 2],
                    )
                )
            self.assertEqual(
                {json.loads(p.read_text())["run"] for p in archives}, {1, 2}
            )
            self.assertIn(json.loads(target.read_text())["run"], {1, 2})
            self.assertEqual(len(list(Path(directory).glob("report-*.json"))), 2)

    def test_interrupted_latest_publication_preserves_previous_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            publish_validation_report(target, {"run": 1})
            original_stage = browser_probe_support.stage_json_write

            def fail_latest(path, report):
                if path == target:
                    raise OSError("injected latest-alias failure")
                return original_stage(path, report)

            with patch.object(browser_probe_support, "stage_json_write", fail_latest):
                with self.assertRaises(OSError):
                    publish_validation_report(target, {"run": 2})
            self.assertEqual(json.loads(target.read_text()), {"run": 1})
            self.assertEqual(
                {
                    json.loads(p.read_text())["run"]
                    for p in Path(directory).glob("report-*.json")
                },
                {1, 2},
            )

    def test_injected_executors_check_stop_at_dispatch_boundary(self):
        for executor_type, candidate in (
            (BrowserKeyExecutor, ActionCandidate("up", "key", "UP")),
            (
                BrowserClickExecutor,
                ActionCandidate("score", "click", "Score", x=2, y=2),
            ),
        ):
            with self.subTest(executor=executor_type.__name__):
                controller = RunController()
                sent = []

                class Page:
                    viewport_size = {"width": 100, "height": 100}
                    keyboard = SimpleNamespace(press=lambda key: sent.append(key))
                    mouse = SimpleNamespace(click=lambda x, y: sent.append((x, y)))

                    def is_closed(self):
                        return False

                    @property
                    def url(self):
                        # Stop arrives after Pipeline's checks, during target validation.
                        controller.stop()
                        return "http://127.0.0.1/game"

                executor = executor_type(
                    Page(),
                    "http://127.0.0.1/game",
                    lambda: controller.ensure_running(0),
                )
                with self.assertRaises(ExecutionCancelled):
                    executor.execute(candidate)
                self.assertEqual(sent, [])

    def test_victory_ocr_stops_without_inventing_obscured_board(self):
        image = SimpleNamespace(width=2, height=2, tobytes=lambda *args: bytes(16))
        image.convert = lambda mode: image
        image_class = SimpleNamespace(open=lambda data: image)
        source = BrowserScreenSource(
            SimpleNamespace(screenshot=lambda: b"fake image"),
            image_class,
            ocr=SimpleNamespace(
                recognize=lambda frame: [{"text": "You"}, {"text": "win!"}]
            ),
        )
        board = {
            "board": [[None] * 4 for _ in range(4)],
            "confidence": 0.0,
            "game_over": False,
        }
        source.adapter = SimpleNamespace(
            adapt=lambda frame, observation: (
                ScreenObservation("victory", 2, 2, features={"game_2048": board}),
                [ActionCandidate("up", "key", "UP")],
            )
        )
        self.assertIsNone(source.capture())
        self.assertTrue(source.victory_observed)
        self.assertEqual(source.read()[1], [])
        board.update(board=[[2048, 0, 0, 0], *([[0] * 4] * 3)], confidence=1.0)
        state = source.capture()
        self.assertTrue(state.won)
        self.assertEqual(source.read()[1], [])


if __name__ == "__main__":
    unittest.main()
