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
