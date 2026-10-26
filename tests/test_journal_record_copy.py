import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ai_game_player.event_journal import EventJournal
from ai_game_player.journal_adapters import LegacyEventAdapter


class JournalRecordCopyTest(unittest.TestCase):
    def test_string_subclass_keys_do_not_share_mutable_attributes_with_cache(self):
        class AnnotatedKey(str):
            pass

        key = AnnotatedKey("action")
        key.labels = ["original"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with EventJournal(
                root / "events.sqlite3", session_id="key-copy-test"
            ) as journal:
                adapter = LegacyEventAdapter(journal, root / "legacy.json", "test.copy")
                adapter.prepare_append({key: "wait"}).publish()
                copied_key = next(iter(adapter.records[0]))
                copied_key.labels.append("changed")
                self.assertEqual(next(iter(adapter.records[0])).labels, ["original"])

    def test_json_records_are_copied_and_converted_in_one_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with EventJournal(
                root / "events.sqlite3", session_id="copy-test"
            ) as journal:
                adapter = LegacyEventAdapter(journal, root / "legacy.json", "test.copy")
                record = {
                    "nested": [{"confidence": Decimal("0.125"), "labels": ["wait"]}],
                    "optional": None,
                    "allowed": True,
                }
                adapter.prepare_append(record).publish()
                # JSON dictionaries/lists are already recursively copied by the
                # Decimal conversion; a second full deepcopy must not be needed.
                with patch(
                    "ai_game_player.journal_adapters.deepcopy"
                ) as duplicate_copy:
                    result = adapter.records
                    duplicate_copy.assert_not_called()
                self.assertEqual(result[0]["nested"][0]["confidence"], 0.125)
                self.assertIsInstance(result[0]["nested"][0]["confidence"], float)
                result[0]["nested"][0]["labels"].append("changed")
                record["nested"][0]["labels"].append("input changed")
                self.assertEqual(adapter.records[0]["nested"][0]["labels"], ["wait"])

    def test_tuple_containing_mutable_values_still_returns_an_independent_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with EventJournal(
                root / "events.sqlite3", session_id="tuple-copy-test"
            ) as journal:
                adapter = LegacyEventAdapter(journal, root / "legacy.json", "test.copy")
                adapter.prepare_append({"values": (["original"],)}).publish()
                result = adapter.records
                self.assertIsInstance(result[0]["values"], tuple)
                result[0]["values"][0].append("changed")
                self.assertEqual(adapter.records[0]["values"], (["original"],))


if __name__ == "__main__":
    unittest.main()
