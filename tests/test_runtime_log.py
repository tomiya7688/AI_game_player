import json
import threading
import tempfile
import time
import unittest
from pathlib import Path

from ai_game_player.runtime_log import RuntimeLog


class RuntimeLogTest(unittest.TestCase):
    def test_appends_json_lines_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "runtime.jsonl"
            log = RuntimeLog(path)
            log.write("decision", "done", {"action_id": "start"})
            log.write("error", "failed")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["event"], "decision")
            self.assertEqual(first["context"]["action_id"], "start")
            self.assertTrue(first["timestamp"].endswith("+00:00"))

    def test_detach_waits_until_in_progress_journal_append_finishes(self):
        class BlockingAdapter:
            def __init__(self):
                self.append_started = threading.Event()
                self.allow_append_to_finish = threading.Event()

            def prepare_append(self, _record):
                adapter = self

                class StagedAppend:
                    def publish(self):
                        adapter.append_started.set()
                        if not adapter.allow_append_to_finish.wait(timeout=5):
                            raise TimeoutError("test did not release journal append")

                return StagedAppend()

        with tempfile.TemporaryDirectory() as directory:
            logger = RuntimeLog(Path(directory) / "runtime.jsonl")
            adapter = BlockingAdapter()
            logger._event_adapter = adapter
            attachment_token = object()
            logger._attachment_token = attachment_token
            writer = threading.Thread(target=lambda: logger.write("assessment"))
            detached = threading.Event()

            def detach():
                logger.detach_event_journal(attachment_token)
                detached.set()

            writer.start()
            self.assertTrue(adapter.append_started.wait(timeout=5))
            detacher = threading.Thread(target=detach)
            detacher.start()
            time.sleep(0.1)
            self.assertFalse(detached.is_set())
            adapter.allow_append_to_finish.set()
            writer.join(timeout=5)
            detacher.join(timeout=5)

            self.assertFalse(writer.is_alive())
            self.assertFalse(detacher.is_alive())
            self.assertTrue(detached.is_set())
