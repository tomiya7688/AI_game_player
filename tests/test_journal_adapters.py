import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.event_journal import EventEnvelope, EventJournal, EventJournalError
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.runtime_log import RuntimeLog


def read_events(database_path: Path) -> list[EventEnvelope]:
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT envelope_json FROM events ORDER BY sequence"
        ).fetchall()
    return [EventEnvelope.from_dict(json.loads(row[0])) for row in rows]


class LegacyEventAdapterTest(unittest.TestCase):
    def test_session_pipeline_writes_decision_safety_execution_and_runtime_events_to_journal(self):
        class Source:
            def read(self):
                return ScreenObservation("menu", 200, 120), [
                    ActionCandidate("continue", "wait", "Continue", confidence=0.9)
                ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "session.sqlite3"
            journal = EventJournal(journal_path, session_id="session-1")
            runtime_log = RuntimeLog(root / "runtime.jsonl")
            pipeline = DecisionPipeline(
                Source(),
                root,
                dry_run=True,
                event_journal=journal,
                runtime_log=runtime_log,
            )
            pipeline.run_and_execute(purpose="continue")
            pipeline.record_safety_outcome("success", 0.9, "screen changed")
            runtime_log.write("session", "step complete", {"steps": 1})
            pipeline.close()

            events = read_events(journal_path)
            event_types = [event.event_type for event in events]
            self.assertIn("decision.history", event_types)
            self.assertIn("decision.trace", event_types)
            self.assertEqual(event_types.count("execution.result"), 1)
            self.assertEqual(event_types.count("safety.audit"), 3)
            self.assertEqual(event_types.count("runtime.log"), 1)
            self.assertFalse((root / "history.json").exists())
            self.assertFalse((root / "decision_trace.json").exists())
            self.assertFalse((root / "execution_history.json").exists())
            self.assertFalse((root / "action_safety.json").exists())

    def test_migrates_json_array_without_changing_source_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "legacy.json"
            original_text = '[{"action_id":"jump","confidence":0.75}]'
            legacy_path.write_text(original_text, encoding="utf-8")
            journal_path = root / "events.sqlite3"

            with EventJournal(journal_path, session_id="session-1") as journal:
                first_adapter = LegacyEventAdapter(
                    journal, legacy_path, "execution.result"
                )
                second_adapter = LegacyEventAdapter(
                    journal, legacy_path, "execution.result"
                )
                staged = first_adapter.prepare_append({"action_id": "stop"})
                self.assertEqual(1, journal.last_sequence)
                staged.publish()

            events = read_events(journal_path)
            self.assertEqual(2, len(events))
            self.assertEqual("jump", events[0].payload["legacy_record"]["action_id"])
            self.assertEqual("stop", events[1].payload["legacy_record"]["action_id"])
            self.assertEqual(original_text, legacy_path.read_text(encoding="utf-8"))
            self.assertEqual([{"action_id": "jump", "confidence": 0.75}], second_adapter.records)

    def test_rejects_reuse_of_event_id_for_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "events.sqlite3"
            with EventJournal(journal_path, session_id="session-1") as journal:
                saved = journal.append(
                    "migration.record", event_id="legacy-event-1", payload={"value": 1}
                )
                retried = journal.append(
                    "migration.record", event_id="legacy-event-1", payload={"value": 1}
                )
                self.assertEqual(saved, retried)
                with self.assertRaisesRegex(EventJournalError, "different event"):
                    journal.append(
                        "migration.record", event_id="legacy-event-1", payload={"value": 2}
                    )
                self.assertEqual(1, journal.last_sequence)

    def test_execution_history_reads_legacy_records_and_appends_only_to_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "execution_history.json"
            legacy_path.write_text(
                '[{"action_id":"old","executed":true,"mode":"mouse"}]',
                encoding="utf-8",
            )
            original_text = legacy_path.read_text(encoding="utf-8")
            with EventJournal(root / "events.sqlite3", session_id="session-1") as journal:
                adapter = LegacyEventAdapter(journal, legacy_path, "execution.result")
                history = ExecutionHistory(legacy_path, adapter)
                history.append(ExecutionResult("new", False, "dry_run", "preview"))
                self.assertEqual(["old", "new"], [result.action_id for result in history.load()])
            self.assertEqual(original_text, legacy_path.read_text(encoding="utf-8"))

    def test_runtime_log_migrates_old_jsonl_and_keeps_append_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "runtime.jsonl"
            legacy_path.write_text(
                '{"event":"old","message":"kept","context":{}}\n',
                encoding="utf-8",
            )
            with EventJournal(root / "events.sqlite3", session_id="session-1") as journal:
                logger = RuntimeLog(legacy_path)
                logger.attach_event_journal(journal)
                logger.write("new", "recorded", {"step": 2})
                self.assertEqual(2, journal.last_sequence)
                self.assertEqual([], logger._event_adapter.records)
                logger.detach_event_journal()
                events = read_events(root / "events.sqlite3")
            self.assertEqual(
                ["old", "new"],
                [event.payload["legacy_record"]["event"] for event in events],
            )
            self.assertEqual(2, len(legacy_path.read_text(encoding="utf-8").splitlines()))


if __name__ == "__main__":
    unittest.main()
