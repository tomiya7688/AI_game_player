"""Developer fixture must not write the source or leak a failed listener."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import probe_reference_browser_resources as probe


class ResourceProbeBoundaries(unittest.TestCase):
    def test_output_equal_or_inside_source_rejected_before_any_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "pinned"
            source.mkdir()
            for output in (source, source / "new" / "deeper"):
                with (
                    self.subTest(output=output),
                    patch.object(probe, "PinnedGameSource") as verifier,
                ):
                    with self.assertRaisesRegex(ValueError, "outside"):
                        probe.main(
                            [
                                "--source-dir",
                                str(source),
                                "--browser",
                                "unused",
                                "--output",
                                str(output),
                            ]
                        )
                    verifier.assert_not_called()
                    self.assertEqual([], list(source.iterdir()))

    def test_thread_start_failure_closes_bound_server_without_shutdown_or_join(self):
        server = Mock()
        thread = Mock()
        thread.start.side_effect = RuntimeError("fixture start failure")
        # PIL is an optional developer dependency; no image operation is reached.
        with (
            patch.dict("sys.modules", {"PIL": Mock()}),
            patch.object(probe, "ResourceFixtureServer", return_value=server),
            patch.object(probe.threading, "Thread", return_value=thread),
        ):
            with self.assertRaises(RuntimeError):
                probe._one_case(Mock(), Path("unused"), "normal", Path("unused"), {})
        server.server_close.assert_called_once()
        server.shutdown.assert_not_called()
        thread.join.assert_not_called()


if __name__ == "__main__":
    unittest.main()
