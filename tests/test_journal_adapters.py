import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.event_journal import EventEnvelope, EventJournal, EventJournalError
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.runtime.session_composition import (
    ManagedSessionRuntime,
    RuntimeComposition,
    SessionRuntimeConfiguration,
    _recover_interrupted_migration,
)
from ai_game_player.run_control import RunController


def read_events(database_path: Path) -> list[EventEnvelope]:
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT envelope_json FROM events ORDER BY sequence"
        ).fetchall()
    return [EventEnvelope.from_dict(json.loads(row[0], parse_float=Decimal)) for row in rows]


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

    # {
    #   責務: [test_migration_preserves_decimal_tokens_in_json_and_jsonl: 旧recordのDecimal精度を保つ]
    #   処理: [JSON arrayとJSONLの小数tokenをDecimalとして読み、Journal payloadの数値を比較する]
    #   引数: []
    #   戻り値: []
    # }
    def test_migration_preserves_decimal_tokens_in_json_and_jsonl(self):
        precise_decimal = "0.1000000000000000000000000001"
        for json_lines, file_name, content in (
            (False, "legacy.json", "[{\"value\":" + precise_decimal + "}]"),
            (True, "legacy.jsonl", '{"value": ' + precise_decimal + "}\n"),
        ):
            with self.subTest(json_lines=json_lines), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_path = root / file_name
                source_path.write_text(content, encoding="utf-8")
                with EventJournal(root / "events.sqlite3", session_id="session-decimal") as journal:
                    LegacyEventAdapter(
                        journal,
                        source_path,
                        "legacy.numeric",
                        json_lines=json_lines,
                    )
                event = read_events(root / "events.sqlite3")[0]
                self.assertEqual(
                    Decimal(precise_decimal),
                    event.payload["legacy_record"]["value"],
                )

    def test_rejects_reuse_of_event_id_for_different_content(self):
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "events.sqlite3"
            with EventJournal(journal_path, session_id="session-1") as journal:
                saved = journal.append(
                    "migration.record",
                    event_id="legacy-event-1",
                    payload={"alpha": 1, "value": 1},
                )
                retried = journal.append(
                    "migration.record",
                    event_id="legacy-event-1",
                    payload={"alpha": 1, "value": 1},
                )
                self.assertEqual(saved, retried)
                with self.assertRaisesRegex(EventJournalError, "different event"):
                    journal.append(
                        "migration.record", event_id="legacy-event-1", payload={"value": 2}
                    )
                reordered_retry = journal.append(
                    "migration.record",
                    event_id="legacy-event-1",
                    payload={"value": 1, "alpha": 1},
                )
                self.assertEqual(saved, reordered_retry)
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

    def test_later_session_reads_legacy_cache_without_replaying_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "execution_history.json"
            legacy_path.write_text(
                '[{"action_id":"old","executed":true,"mode":"mouse"}]',
                encoding="utf-8",
            )
            journal_path = root / "later-session.sqlite3"
            with EventJournal(journal_path, session_id="session-2") as journal:
                adapter = LegacyEventAdapter(
                    journal,
                    legacy_path,
                    "execution.result",
                    migrate_legacy=False,
                )
                history = ExecutionHistory(legacy_path, adapter)
                self.assertEqual(["old"], [entry.action_id for entry in history.load()])
                self.assertEqual(0, journal.last_sequence)

    def test_runtime_log_skips_legacy_read_when_cache_and_migration_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "runtime.jsonl"
            legacy_path.write_text('{"event": "truncated"', encoding="utf-8")
            with EventJournal(root / "events.sqlite3", session_id="session-later") as journal:
                adapter = LegacyEventAdapter(
                    journal,
                    legacy_path,
                    "runtime.log",
                    json_lines=True,
                    retain_records=False,
                    migrate_legacy=False,
                )
                self.assertEqual([], adapter.records)
                self.assertEqual(0, journal.last_sequence)

    def test_event_id_lookup_sqlite_error_uses_event_journal_error_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            with EventJournal(Path(directory) / "events.sqlite3", session_id="session-error") as journal:
                with patch.object(
                    journal,
                    "_read_event_by_id",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    with self.assertRaisesRegex(EventJournalError, "retry lookup failed"):
                        journal.append("legacy.record", event_id="legacy-1")

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
                attachment_token = logger.attach_event_journal(journal)
                logger.write("new", "recorded", {"step": 2})
                self.assertEqual(2, journal.last_sequence)
                self.assertEqual([], logger._event_adapter.records)
                logger.detach_event_journal(attachment_token)
                events = read_events(root / "events.sqlite3")
            self.assertEqual(
                ["old", "new"],
                [event.payload["legacy_record"]["event"] for event in events],
            )
            self.assertEqual(2, len(legacy_path.read_text(encoding="utf-8").splitlines()))

    def test_runtime_log_rejects_overlapping_sessions_and_stale_detach(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logger = RuntimeLog(root / "runtime.jsonl")
            with EventJournal(root / "first.sqlite3", session_id="first") as first_journal:
                with EventJournal(root / "second.sqlite3", session_id="second") as second_journal:
                    first_token = logger.attach_event_journal(first_journal)
                    with self.assertRaisesRegex(RuntimeError, "already attached"):
                        logger.attach_event_journal(second_journal)
                    with self.assertRaisesRegex(RuntimeError, "does not own"):
                        logger.detach_event_journal(object())
                    logger.write("first-session", "still attached")
                    self.assertEqual(1, first_journal.last_sequence)
                    self.assertEqual(0, second_journal.last_sequence)
                    logger.detach_event_journal(first_token)
                    self.assertIsNone(logger._event_adapter)

    def test_journal_factory_without_runtime_log_parameter_does_not_mark_log_migrated(self):
        class Provider:
            def close(self):
                return None

        def journal_pipeline_factory(
            source,
            game_directory,
            provider,
            controller,
            *,
            event_journal,
            migrate_legacy,
            migrate_runtime_log,
        ):
            return DecisionPipeline(
                source,
                game_directory,
                provider,
                controller,
                event_journal=event_journal,
                migrate_legacy=migrate_legacy,
                migrate_runtime_log=migrate_runtime_log,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            logger = RuntimeLog(root / "runtime.jsonl")
            runtime = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()},
                pipeline_factory=journal_pipeline_factory,
            ).create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
                runtime_log=logger,
            )

            runtime.close()

            self.assertFalse(
                logger.path.with_name(
                    logger.path.name + ".session-event-migration-v1.complete"
                ).exists()
            )
            logger.write("after-session", "legacy factory left logger detached")

    def test_invalid_execution_history_fails_before_executor_is_created(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            game_directory.mkdir()
            (game_directory / "execution_history.json").write_text("not valid JSON", encoding="utf-8")
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            )

            with patch("ai_game_player.pipeline.ActionExecutor") as executor_factory:
                with self.assertRaises(ValueError):
                    composition.create_session_runtime(
                        configuration,
                        source=object(),
                        controller=RunController(),
                    )

            executor_factory.assert_not_called()

    def test_interrupted_migration_discards_partial_journal_and_retries_sources(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            session_events_directory = game_directory / "session_events"
            session_events_directory.mkdir(parents=True)
            history_path = game_directory / "history.json"
            history_path.write_text('[{"legacy":"history"}]', encoding="utf-8")
            interrupted_journal_path = session_events_directory / "interrupted.sqlite3"
            with EventJournal(interrupted_journal_path, session_id="interrupted") as journal:
                journal.append(
                    "decision.history",
                    event_id="interrupted-history-import",
                    payload={"legacy_record": {"legacy": "history"}},
                )
            pending_path = session_events_directory / "legacy_migration_v1.pending"
            pending_path.write_text(
                json.dumps({
                    "journal_name": interrupted_journal_path.name,
                    "game_history": True,
                    "runtime_log": False,
                    "runtime_log_source": None,
                }),
                encoding="utf-8",
            )
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )

            runtime = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            ).create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
            )
            recovered_journal_path = runtime.pipeline.event_journal.path
            runtime.close()

            self.assertNotEqual(interrupted_journal_path, recovered_journal_path)
            self.assertFalse(interrupted_journal_path.exists())
            self.assertFalse(pending_path.exists())
            history_events = [
                event for event in read_events(recovered_journal_path)
                if event.event_type == "decision.history"
            ]
            self.assertEqual(1, len(history_events))

    def test_shared_log_marker_does_not_complete_unrelated_pending_migration(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            session_events_directory = game_directory / "session_events"
            session_events_directory.mkdir(parents=True)
            runtime_log_path = root / "runtime.jsonl"
            runtime_log_path.write_text("", encoding="utf-8")
            runtime_marker = runtime_log_path.with_name(
                runtime_log_path.name + ".session-event-migration-v1.complete"
            )
            runtime_marker.touch()
            interrupted_journal_path = session_events_directory / "interrupted.sqlite3"
            with EventJournal(interrupted_journal_path, session_id="interrupted") as journal:
                journal.append("runtime.log", event_id="runtime-import", payload={"event": "old"})
            pending_path = session_events_directory / "legacy_migration_v1.pending"
            pending_path.write_text(
                json.dumps({
                    "journal_name": interrupted_journal_path.name,
                    "game_history": True,
                    "runtime_log": True,
                    "runtime_log_source": str(runtime_log_path.resolve()),
                    "factory_completed": False,
                }),
                encoding="utf-8",
            )
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )

            runtime = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            ).create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
                runtime_log=RuntimeLog(runtime_log_path),
            )
            runtime.close()

            self.assertFalse(interrupted_journal_path.exists())
            self.assertTrue(runtime_marker.exists())
            self.assertTrue((session_events_directory / "legacy_migration_v1.complete").exists())
            self.assertFalse(pending_path.exists())

    def test_factory_completed_pending_record_finishes_its_own_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_events_directory = root / "game" / "session_events"
            session_events_directory.mkdir(parents=True)
            runtime_log_path = root / "runtime.jsonl"
            runtime_marker = runtime_log_path.with_name(
                runtime_log_path.name + ".session-event-migration-v1.complete"
            )
            runtime_marker.touch()
            journal_path = session_events_directory / "completed.sqlite3"
            with EventJournal(journal_path, session_id="completed") as journal:
                journal.append(
                    "runtime.log",
                    event_id="completed-log-import",
                    payload={"event": "old"},
                )
            pending_path = session_events_directory / "legacy_migration_v1.pending"
            pending_path.write_text(
                json.dumps({
                    "journal_name": journal_path.name,
                    "game_history": True,
                    "runtime_log": True,
                    "runtime_log_source": str(runtime_log_path.resolve()),
                    "factory_completed": True,
                }),
                encoding="utf-8",
            )

            _recover_interrupted_migration(pending_path, session_events_directory)

            self.assertTrue(journal_path.exists())
            self.assertTrue((session_events_directory / "legacy_migration_v1.complete").exists())
            self.assertTrue(runtime_marker.exists())
            self.assertFalse(pending_path.exists())
            self.assertEqual(1, len(read_events(journal_path)))

    def test_pending_recovery_record_remains_when_partial_cleanup_fails(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            game_directory.mkdir()
            (game_directory / "execution_history.json").write_text("invalid JSON", encoding="utf-8")
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            )

            with patch(
                "ai_game_player.runtime.session_composition._remove_partial_journal_files",
                side_effect=OSError("database is locked by another process"),
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup also failed"):
                    composition.create_session_runtime(
                        configuration,
                        source=object(),
                        controller=RunController(),
                    )

            self.assertTrue(
                (game_directory / "session_events" / "legacy_migration_v1.pending").exists()
            )

    def test_runtime_log_migration_marker_is_scoped_to_source_across_games(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_log_path = root / "runtime.jsonl"
            runtime_log_path.write_text(
                '{"event":"original-source","message":"kept","context":{}}\n',
                encoding="utf-8",
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            )

            def configuration_for(game_name):
                return SessionRuntimeConfiguration(
                    provider_name="fake",
                    model="model",
                    endpoint="http://localhost",
                    game_directory=root / game_name,
                    dry_run=True,
                    window_handle=None,
                    input_mode="window_message",
                    window_process_id=None,
                )

            first = composition.create_session_runtime(
                configuration_for("game-one"),
                source=object(),
                controller=RunController(),
                runtime_log=RuntimeLog(runtime_log_path),
            )
            first.close()
            shared_source = composition.create_session_runtime(
                configuration_for("game-two"),
                source=object(),
                controller=RunController(),
                runtime_log=RuntimeLog(runtime_log_path),
            )
            shared_source_path = shared_source.pipeline.event_journal.path
            self.assertEqual(0, shared_source.pipeline.event_journal.last_sequence)
            shared_source.close()

            second_source_path = root / "runtime-second.jsonl"
            second_source_path.write_text(
                '{"event":"second-source","message":"new","context":{}}\n',
                encoding="utf-8",
            )
            changed_source = composition.create_session_runtime(
                configuration_for("game-two"),
                source=object(),
                controller=RunController(),
                runtime_log=RuntimeLog(second_source_path),
            )
            changed_source_journal_path = changed_source.pipeline.event_journal.path
            changed_source.close()

            self.assertEqual(0, len(read_events(shared_source_path)))
            self.assertEqual(
                "second-source",
                read_events(changed_source_journal_path)[0].payload["legacy_record"]["event"],
            )

    def test_provider_is_closed_when_migration_lock_cannot_be_acquired(self):
        class Provider:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = Provider()
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=root / "game",
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: provider}
            )

            with patch(
                "ai_game_player.runtime.session_composition._legacy_migration_lock",
                side_effect=OSError("migration directory is unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "migration directory"):
                    composition.create_session_runtime(
                        configuration,
                        source=object(),
                        controller=RunController(),
                    )

            self.assertTrue(provider.closed)

    def test_pipeline_and_journal_are_closed_when_migration_lock_release_fails(self):
        class Provider:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Pipeline:
            def __init__(self, event_journal):
                self.event_journal = event_journal
                self.closed = False

            def close(self):
                self.closed = True
                self.event_journal.close()

        provider = Provider()
        pipeline_holder = {}

        def pipeline_factory(
            _source,
            _game_directory,
            _provider,
            _controller,
            *,
            event_journal,
            migrate_legacy,
            migrate_runtime_log,
        ):
            pipeline = Pipeline(event_journal)
            pipeline_holder["pipeline"] = pipeline
            return pipeline

        @contextmanager
        def lock_that_fails_to_release(_lock_path):
            yield
            raise OSError("migration lock release failed")

        with tempfile.TemporaryDirectory() as directory:
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=Path(directory) / "game",
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: provider},
                pipeline_factory=pipeline_factory,
            )
            with patch(
                "ai_game_player.runtime.session_composition._legacy_migration_lock",
                side_effect=lock_that_fails_to_release,
            ):
                with self.assertRaisesRegex(OSError, "migration lock release failed"):
                    composition.create_session_runtime(
                        configuration,
                        source=object(),
                        controller=RunController(),
                    )

        self.assertTrue(pipeline_holder["pipeline"].closed)
        self.assertIsNone(pipeline_holder["pipeline"].event_journal._connection)
        self.assertTrue(provider.closed)

    def test_shutdown_failure_records_failed_status_before_journal_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "session.sqlite3"
            journal = EventJournal(journal_path, session_id="session-failed")
            runtime_log = RuntimeLog(root / "runtime.jsonl")
            pipeline = DecisionPipeline(
                object(),
                root,
                event_journal=journal,
                runtime_log=runtime_log,
            )

            pipeline.record_shutdown_failure("provider", RuntimeError("close failed"))
            pipeline.close()

            failure_events = [
                event for event in read_events(journal_path)
                if event.event_type == "runtime.log"
                and event.payload["legacy_record"]["event"] == "session.shutdown_failed"
            ]
            self.assertEqual(1, len(failure_events))
            self.assertEqual(
                "failed",
                failure_events[0].payload["legacy_record"]["context"]["status"],
            )

    def test_shutdown_failure_records_failed_status_without_runtime_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "session.sqlite3"
            pipeline = DecisionPipeline(
                object(),
                root,
                event_journal=EventJournal(journal_path, session_id="session-no-runtime-log"),
            )

            pipeline.record_shutdown_failure("provider", RuntimeError("close failed"))
            pipeline.close()

            failure_events = [
                event for event in read_events(journal_path)
                if event.event_type == "runtime.log"
                and event.payload["legacy_record"]["event"] == "session.shutdown_failed"
            ]
            self.assertEqual(1, len(failure_events))
            self.assertEqual("failed", failure_events[0].status)
            self.assertEqual("provider", failure_events[0].payload["legacy_record"]["context"]["resource"])

    def test_positional_only_journal_factory_uses_legacy_pipeline_path(self):
        class Provider:
            def close(self):
                return None

        class Pipeline:
            def __init__(self, event_journal):
                self.event_journal = event_journal

            def close(self):
                return None

        received_journal = []

        def positional_only_factory(
            _source,
            _game_directory,
            _provider,
            _controller,
            event_journal=None,
            migrate_legacy=True,
            migrate_runtime_log=True,
            /,
        ):
            received_journal.append(event_journal)
            return Pipeline(event_journal)

        with tempfile.TemporaryDirectory() as directory:
            game_directory = Path(directory) / "game"
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            runtime = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()},
                pipeline_factory=positional_only_factory,
            ).create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
            )
            runtime.close()

            self.assertEqual([None], received_journal)
            self.assertFalse((game_directory / "session_events").exists())

    def test_journal_checkpoint_failure_is_logged_before_runtime_log_detaches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "session.sqlite3"
            journal = EventJournal(journal_path, session_id="session-checkpoint-failed")
            pipeline = DecisionPipeline(
                object(),
                root,
                event_journal=journal,
                runtime_log=RuntimeLog(root / "runtime.jsonl"),
            )
            with patch.object(
                journal,
                "flush",
                side_effect=EventJournalError("injected checkpoint failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected checkpoint failure"):
                    pipeline.close()

            failure_events = [
                event for event in read_events(journal_path)
                if event.event_type == "runtime.log"
                and event.payload["legacy_record"]["event"] == "session.shutdown_failed"
            ]
            self.assertEqual(1, len(failure_events))
            self.assertEqual(
                "event_journal_checkpoint",
                failure_events[0].payload["legacy_record"]["context"]["resource"],
            )

    def test_managed_runtime_does_not_retry_closed_journal_after_close_error(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = EventJournal(root / "session.sqlite3", session_id="close-error")
            pipeline = DecisionPipeline(object(), root, event_journal=journal)
            runtime = ManagedSessionRuntime(pipeline, Provider())
            close_journal = journal.close

            def close_then_report_checkpoint_failure():
                close_journal()
                raise EventJournalError("final checkpoint failed after connection close")

            with patch.object(journal, "close", side_effect=close_then_report_checkpoint_failure):
                with self.assertRaisesRegex(RuntimeError, "final checkpoint failed"):
                    runtime.close()
                self.assertTrue(pipeline._event_journal_closed)
                runtime.close()

            self.assertTrue(runtime._pipeline_closed)

    def test_managed_runtime_records_provider_shutdown_failure_in_journal(self):
        class Provider:
            def close(self):
                raise RuntimeError("provider close failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=root / "game",
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            runtime = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            ).create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
                runtime_log=RuntimeLog(root / "runtime.jsonl"),
            )
            journal_path = runtime.pipeline.event_journal.path

            with self.assertRaisesRegex(RuntimeError, "provider close failed"):
                runtime.close()

            failure_events = [
                event for event in read_events(journal_path)
                if event.event_type == "runtime.log"
                and event.payload["legacy_record"]["event"] == "session.shutdown_failed"
            ]
            self.assertEqual(1, len(failure_events))
            self.assertEqual(
                "provider",
                failure_events[0].payload["legacy_record"]["context"]["resource"],
            )
            self.assertEqual(
                "failed",
                failure_events[0].payload["legacy_record"]["context"]["status"],
            )

    def test_failed_partial_migration_removes_session_database_before_retry(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            game_directory.mkdir()
            (game_directory / "history.json").write_text('[{"legacy":"history"}]', encoding="utf-8")
            execution_history_path = game_directory / "execution_history.json"
            execution_history_path.write_text("not valid JSON", encoding="utf-8")
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            )

            with self.assertRaises(ValueError):
                composition.create_session_runtime(
                    configuration,
                    source=object(),
                    controller=RunController(),
                )

            session_events = game_directory / "session_events"
            self.assertEqual([], list(session_events.glob("*.sqlite3")))
            self.assertFalse(session_events.joinpath("legacy_migration_v1.complete").exists())

            execution_history_path.write_text("[]", encoding="utf-8")
            runtime = composition.create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
            )
            journal_path = runtime.pipeline.event_journal.path
            runtime.close()

            self.assertEqual(1, len(list(session_events.glob("*.sqlite3"))))
            self.assertEqual(
                1,
                sum(event.event_type == "decision.history" for event in read_events(journal_path)),
            )

    def test_runtime_log_migration_has_completion_marker_independent_of_history(self):
        class Provider:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_directory = root / "game"
            runtime_log_path = root / "runtime.jsonl"
            runtime_log_path.write_text(
                '{"event":"before-first-logger","message":"kept","context":{}}\n',
                encoding="utf-8",
            )
            configuration = SessionRuntimeConfiguration(
                provider_name="fake",
                model="model",
                endpoint="http://localhost",
                game_directory=game_directory,
                dry_run=True,
                window_handle=None,
                input_mode="window_message",
                window_process_id=None,
            )
            composition = RuntimeComposition(
                provider_factories={"fake": lambda *_arguments: Provider()}
            )
            history_only_runtime = composition.create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
            )
            history_only_runtime.close()

            logger = RuntimeLog(runtime_log_path)
            runtime_with_logger = composition.create_session_runtime(
                configuration,
                source=object(),
                controller=RunController(),
                runtime_log=logger,
            )
            journal_path = runtime_with_logger.pipeline.event_journal.path
            self.assertEqual(1, runtime_with_logger.pipeline.event_journal.last_sequence)
            runtime_with_logger.close()

            migrated_events = read_events(journal_path)
            self.assertEqual("runtime.log", migrated_events[0].event_type)
            self.assertEqual(
                "before-first-logger",
                migrated_events[0].payload["legacy_record"]["event"],
            )


if __name__ == "__main__":
    unittest.main()
