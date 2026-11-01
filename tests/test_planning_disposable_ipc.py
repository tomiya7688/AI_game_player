import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.planning_file_publisher import (
    publish_planning_bytes,
    replace_planning_file,
)
from ai_game_player.planning_wire import (
    decode_planning_json,
    publish_planning_json,
    read_planning_json,
)
from ai_game_player.planning_worker_diagnostic import new_parent_timing


class PlanningDisposableIpcTests(unittest.TestCase):
    def test_single_encode_same_utf8_bytes_without_durability_change_to_other_store(
        self,
    ):
        value = {"nonce": "abc", "state": "試験😀", "values": [0.25, -1, 1]}
        expected = json.dumps(
            value, ensure_ascii=False, indent=2, allow_nan=False
        ).encode("utf-8")
        real_dumps = json.dumps
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            with (
                patch(
                    "ai_game_player.planning_wire.json.dumps", wraps=real_dumps
                ) as encode,
                patch("os.fsync") as sync,
            ):
                publish_planning_json(path, value, len(expected))
                self.assertEqual(encode.call_count, 1)
                sync.assert_not_called()
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(read_planning_json(path, len(expected)), value)
                pending = stage_json_write(Path(directory) / "durable.json", value)
                try:
                    sync.assert_called_once()
                finally:
                    pending.discard()

    def test_flush_and_close_precede_one_atomic_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            publish_planning_json(path, {"depth": 1}, 100)
            old = path.read_bytes()
            stages = []
            streams = []
            real_temporary = tempfile.NamedTemporaryFile

            def track_stream(**kwargs):
                stream = real_temporary(**kwargs)
                streams.append(stream)
                return stream

            def inspect_before_publish(source, target):
                self.assertTrue(streams[-1].closed)
                self.assertEqual(source.parent, target.parent)
                self.assertEqual(source.read_bytes(), b'{"depth":2}')
                self.assertEqual(target.read_bytes(), old)
                stages.append(source)
                replace_planning_file(source, target)

            with (
                patch(
                    "ai_game_player.planning_file_publisher.tempfile.NamedTemporaryFile",
                    side_effect=track_stream,
                ),
                patch(
                    "ai_game_player.planning_file_publisher.replace_planning_file",
                    side_effect=inspect_before_publish,
                ) as publish,
            ):
                publish_planning_bytes(path, b'{"depth":2}', 100)
            publish.assert_called_once()
            self.assertEqual(read_planning_json(path, 100), {"depth": 2})
            self.assertFalse(stages[0].exists())
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_short_write_never_replaces_existing_or_exposes_partial_first_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            real_temporary = tempfile.NamedTemporaryFile

            def short_stream(**kwargs):
                stream = real_temporary(**kwargs)
                real_write = stream.write
                stream.write = lambda raw: real_write(raw[:1])
                return stream

            for initial in (None, {"depth": 1}):
                if initial is not None:
                    publish_planning_json(path, initial, 100)
                with (
                    patch(
                        "ai_game_player.planning_file_publisher.tempfile.NamedTemporaryFile",
                        side_effect=short_stream,
                    ),
                    patch(
                        "ai_game_player.planning_file_publisher.replace_planning_file"
                    ) as publish,
                    self.assertRaisesRegex(OSError, "incomplete"),
                ):
                    publish_planning_bytes(path, b'{"depth":2}', 100)
                publish.assert_not_called()
                if initial is None:
                    self.assertEqual(list(path.parent.iterdir()), [])
                else:
                    self.assertEqual(read_planning_json(path, 100), initial)
                    self.assertEqual(list(path.parent.iterdir()), [path])

    def test_primary_write_or_replace_failure_survives_a_second_temp_cleanup_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            publish_planning_json(path, {"depth": 1}, 100)
            original = path.read_bytes()
            real_temporary = tempfile.NamedTemporaryFile
            for phase in ("write", "replace"):
                primary = OSError(28, "primary fixed failure")
                cleanup = PermissionError(13, "secondary fixed failure")

                def failed_stream(**kwargs):
                    stream = real_temporary(**kwargs)
                    if phase == "write":

                        def failed_write(_raw):
                            raise primary

                        stream.write = failed_write
                    return stream

                with (
                    patch(
                        "ai_game_player.planning_file_publisher.tempfile.NamedTemporaryFile",
                        side_effect=failed_stream,
                    ),
                    patch(
                        "ai_game_player.planning_file_publisher.replace_planning_file",
                        side_effect=primary if phase == "replace" else None,
                    ) as replace,
                    patch.object(Path, "unlink", side_effect=cleanup),
                    self.assertRaises(OSError) as caught,
                ):
                    publish_planning_bytes(path, b'{"depth":2}', 100)
                self.assertIs(caught.exception, primary)
                self.assertEqual(caught.exception.errno, 28)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(replace.call_count, int(phase == "replace"))
                # The enclosing owned-directory cleanup is responsible after the OS refuses unlink.
                for temporary in path.parent.glob(".checkpoint.json.*.tmp"):
                    temporary.unlink()

    def test_cleanup_only_failure_remains_visible_after_successful_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            error = PermissionError(13, "cleanup failure")
            with (
                patch.object(Path, "unlink", side_effect=error),
                self.assertRaises(OSError) as caught,
            ):
                publish_planning_bytes(path, b'{"depth":2}', 100)
            self.assertIs(caught.exception, error)
            self.assertEqual(read_planning_json(path, 100), {"depth": 2})

    def test_invalid_json_and_byte_budget_cannot_modify_the_last_complete_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finished.json"
            publish_planning_json(path, {"reason": "complete"}, 100)
            expected = path.read_bytes()
            nested = []
            for _ in range(32):
                nested = [nested]
            for value, maximum in (
                ({"value": float("nan")}, 100),
                ({"value": float("inf")}, 100),
                ({"value": "試"}, 1),
                ({"value": nested}, 10000),
            ):
                with self.subTest(value_type=type(value["value"])):
                    with self.assertRaises(ValueError):
                        publish_planning_json(path, value, maximum)
                    self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_decode_one_immutable_read_retains_all_strict_json_guards(self):
        for raw, maximum in (
            (b'{"a":1,"a":2}', 100),
            (b'{"a":NaN}', 100),
            (b'{"a":Infinity}', 100),
            (b'{"a":"\xff"}', 100),
            (b'{"a":1', 100),
            (b"[]", 100),
            (b"{}", 1),
            (b'{"a":' + b"[" * 32 + b"0" + b"]" * 32 + b"}", 1000),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_planning_json(raw, maximum)
        self.assertEqual(decode_planning_json(b'{"a":"[\\"{]"}', 100), {"a": '["{]'})

    def test_optional_parent_phases_are_monotonic_do_not_extend_the_cutoff(self):
        timing = new_parent_timing(100, 500)
        phases = (
            "request_encode_started_at_ns",
            "request_encode_finished_at_ns",
            "request_write_started_at_ns",
            "request_write_closed_at_ns",
            "request_publish_started_at_ns",
            "request_publish_finished_at_ns",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            with patch(
                "ai_game_player.planning_worker_diagnostic._parent_clock_ns",
                side_effect=range(101, 107),
            ) as clock:
                publish_planning_json(path, {"nonce": "abc"}, 100, parent_timing=timing)
            self.assertEqual(clock.call_count, 6)
            self.assertEqual([timing[phase] for phase in phases], list(range(101, 107)))
            self.assertEqual(timing["compute_cutoff_at_ns"], 500)
            self.assertLess(len(json.dumps(timing)), 4096)
            for error in (MemoryError(), ValueError()):
                with patch(
                    "ai_game_player.planning_worker_diagnostic._parent_clock_ns",
                    side_effect=error,
                ):
                    publish_planning_json(
                        path,
                        {"nonce": "new"},
                        100,
                        parent_timing=new_parent_timing(100, 500),
                    )
                self.assertEqual(read_planning_json(path, 100), {"nonce": "new"})
            with patch(
                "ai_game_player.planning_worker_diagnostic._parent_clock_ns"
            ) as clock:
                publish_planning_json(path, {"nonce": "off"}, 100)
            clock.assert_not_called()
