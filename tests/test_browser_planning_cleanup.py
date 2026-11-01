from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.event_journal import EventJournalClosedError
from tools import compare_2048_browser_planning as comparison


class BrowserPlanningCleanupTests(unittest.TestCase):
    def test_constructor_failure_remains_primary_and_journal_closes_before_tempdir_removal(
        self,
    ):
        journals = []
        context = SimpleNamespace(
            add_init_script=lambda _: None,
            new_page=lambda: SimpleNamespace(
                goto=lambda _: None, wait_for_timeout=lambda _: None
            ),
            close=lambda: (_ for _ in ()).throw(RuntimeError("context cleanup failed")),
        )
        source = SimpleNamespace(
            victory_observed=False, last_state=None, completed=[], last_png=None
        )

        def fail_pipeline(*args, **kwargs):
            journals.append(kwargs["event_journal"])
            raise RuntimeError("pipeline construction failed")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"PIL": SimpleNamespace(Image=object())}),
            (
                patch.object(
                    comparison, "FreshCompletedBrowserSource", return_value=source
                )
            ),
            patch.object(comparison, "DecisionPipeline", side_effect=fail_pipeline),
        ):
            result = comparison.episode(
                SimpleNamespace(new_context=lambda **_: context),
                "fixture",
                479,
                "baseline",
                3,
                Path(directory),
            )
        self.assertEqual(result["error"]["message"], "pipeline construction failed")
        self.assertEqual(
            result["context_cleanup_error"]["message"], "context cleanup failed"
        )
        self.assertEqual(len(journals), 1)
        self.assertFalse(journals[0].path.parent.exists())
        with self.assertRaises(EventJournalClosedError):
            journals[0].append("test")

    def test_context_creation_failure_is_reported_without_an_input(self):
        def fail_context(**kwargs):
            raise RuntimeError("context construction failed")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"PIL": SimpleNamespace(Image=object())}),
        ):
            result = comparison.episode(
                SimpleNamespace(new_context=fail_context),
                "fixture",
                479,
                "planner",
                3,
                Path(directory),
            )
        self.assertEqual(result["error"]["message"], "context construction failed")
        self.assertEqual(result["actions"], 0)

    def test_cli_reports_and_forwards_the_same_requested_depth_and_budget(self):
        from contextlib import nullcontext
        import json

        browser = SimpleNamespace(close=lambda: None)
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **_: browser)
        )
        module = SimpleNamespace(sync_playwright=lambda: nullcontext(playwright))
        server = SimpleNamespace(
            server_port=123,
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"playwright.sync_api": module}),
            (
                patch.object(
                    comparison,
                    "PinnedGameSource",
                    return_value=SimpleNamespace(
                        materialize=lambda: Path(directory), lock={"commit": "pinned"}
                    ),
                )
            ),
            patch.object(comparison, "ThreadingHTTPServer", return_value=server),
            (
                patch.object(
                    comparison,
                    "episode",
                    return_value={
                        "actions": 1,
                        "stopped_input_rejected": True,
                        "visible_score": "0",
                    },
                )
            ) as run,
            (
                patch(
                    "sys.argv",
                    [
                        "comparison",
                        "--allow-live-input",
                        "--providers",
                        "planner",
                        "--seeds",
                        "479",
                        "--max-steps",
                        "1",
                        "--planner-depth",
                        "2",
                        "--planner-wall-ms",
                        "600",
                        "--output",
                        directory,
                    ],
                )
            ),
        ):
            self.assertEqual(comparison.main(), 0)
            self.assertEqual(run.call_args.kwargs["planner_depth"], 2)
            self.assertEqual(run.call_args.kwargs["planner_wall_ms"], 600)
            report = json.loads(
                next(Path(directory).glob("run-*/report.json")).read_text()
            )
        self.assertEqual(report["profiles"]["planner"]["max_depth"], 2)
        self.assertEqual(report["profiles"]["planner"]["wall_time_ms"], 600)

    def test_post_input_capture_failure_keeps_score_and_marks_missing_completion(self):
        from ai_game_player.game_2048_state import Game2048State
        from ai_game_player.models import ActionCandidate, ScreenObservation
        from tools.check_2048_screen_transition import BrowserScreenSource

        pressed = []
        page = SimpleNamespace(
            goto=lambda _: None,
            wait_for_timeout=lambda _: None,
            is_closed=lambda: False,
            url="fixture",
            keyboard=SimpleNamespace(press=pressed.append),
            locator=lambda _: SimpleNamespace(evaluate=lambda *args, **kwargs: "1824"),
        )
        context = SimpleNamespace(
            add_init_script=lambda _: None, new_page=lambda: page, close=lambda: None
        )
        captures = [0]

        def capture(source):
            captures[0] += 1
            if captures[0] > 1:
                raise ValueError("unrecognized glow cell")
            source.cached = (
                ScreenObservation(
                    "fixture", 100, 100, features={"signature": "pixels"}
                ),
                [ActionCandidate("2048-move-left", "key", "LEFT", confidence=1)],
            )
            return Game2048State(
                ((0, 2, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0))
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"PIL": SimpleNamespace(Image=object())}),
            patch.object(BrowserScreenSource, "capture", capture),
        ):
            result = comparison.episode(
                SimpleNamespace(new_context=lambda **_: context),
                "fixture",
                479,
                "baseline",
                3,
                Path(directory),
            )
        self.assertEqual(pressed, ["ArrowLeft"])
        self.assertEqual(result["error"]["message"], "unrecognized glow cell")
        self.assertEqual(result["visible_score"], "1824")
        self.assertTrue(result["terminal_board_unverified"])
        self.assertEqual(result["confirmed_backend_key_sends"], 1)
        self.assertEqual(result["missing_completion_count"], 1)
        self.assertEqual(result["pending_execution_observation_count"], 1)
        self.assertEqual(result["unverified_action_count"], 1)
        self.assertEqual(
            result["last_execution_attempt"]["action_id"], "2048-move-left"
        )
        self.assertEqual(
            result["last_execution_attempt"]["post_observation_status"], "pending"
        )
        self.assertEqual(
            result["last_execution_attempt"]["recognized_before_board"][0], [0, 2, 0, 0]
        )
        self.assertEqual(len(result["failure_journal"]["reference"]["sha256"]), 64)
