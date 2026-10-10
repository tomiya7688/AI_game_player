import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_game_player.models import ActionDecision
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRuntimeConfiguration,
)


class FakeProvider:
    def __init__(self, events, close_error=None):
        self.events = events
        self.close_error = close_error

    def assess_outcome(self, observation, previous):
        self.events.append(("provider.assess", observation, previous))
        return OutcomeAssessment("ongoing", 0.7, "fake assessment")

    def close(self):
        self.events.append("provider.close")
        if self.close_error is not None:
            raise self.close_error


class FakePipeline:
    def __init__(self, events, close_error=None):
        self.events = events
        self.close_error = close_error
        self.event_journal = None
        self.journal_closed = False

    def run(self, **_arguments):
        return ActionDecision("start", "fake decision", "fake")

    def run_and_execute(self, **_arguments):
        return "executed"

    def close(self):
        self.events.append("pipeline.close")
        if self.event_journal is not None and not self.journal_closed:
            self.event_journal.close()
            self.journal_closed = True
        if self.close_error is not None:
            raise self.close_error


class SessionCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def configuration(self, **changes):
        values = {
            "provider_name": "fake",
            "model": "model-a",
            "endpoint": "http://localhost",
            "game_directory": Path(self.temporary_directory.name) / "game",
            "dry_run": True,
            "window_handle": 12,
            "input_mode": "window_message",
            "window_process_id": 34,
        }
        values.update(changes)
        return SessionRuntimeConfiguration(**values)

    def test_runtime_uses_injected_factories_and_keeps_session_provider(self):
        events = []
        provider = FakeProvider(events)
        pipeline = FakePipeline(events)
        build_arguments = {}

        def build_pipeline(*arguments, **keywords):
            build_arguments["arguments"] = arguments
            build_arguments["keywords"] = keywords
            pipeline.event_journal = keywords["event_journal"]
            return pipeline

        composition = RuntimeComposition(
            provider_factories={"fake": lambda model, endpoint: (events.append((model, endpoint)) or provider)},
            pipeline_factory=build_pipeline,
        )
        source = object()
        controller = object()
        runtime = composition.create_session_runtime(
            self.configuration(),
            source=source,
            controller=controller,
        )

        self.assertIs(runtime.pipeline, pipeline)
        self.assertEqual(runtime.run().action_id, "start")
        self.assertEqual(runtime.run_and_execute(), "executed")
        assessment = runtime.assess_outcome(object(), None)
        self.assertEqual(assessment.reason, "fake assessment")
        self.assertEqual(events[0], ("model-a", "http://localhost"))
        self.assertEqual(events[1][0], "provider.assess")
        self.assertIsNone(events[1][2])
        runtime.close()
        self.assertEqual(
            build_arguments["arguments"],
            (source, self.configuration().game_directory, provider, controller),
        )
        self.assertEqual(
            {key: build_arguments["keywords"][key] for key in (
                "dry_run", "window_handle", "input_mode", "window_process_id", "migrate_legacy"
            )},
            {
                "dry_run": True,
                "window_handle": 12,
                "input_mode": "window_message",
                "window_process_id": 34,
                "migrate_legacy": True,
            },
        )

    # {
    #   責務: [test_legacy_files_are_migrated_only_in_the_first_runtime_session: 旧保存記録を複数Sessionへ複製しない]
    #   処理: [共有game directoryでruntimeを2回生成し、初回だけ旧記録移行を有効にする]
    #   引数: []
    #   戻り値: []
    # }
    def test_legacy_files_are_migrated_only_in_the_first_runtime_session(self):
        migration_values = []

        class Pipeline:
            event_journal = None

            def close(self):
                self.event_journal.close()

            def load_execution_history(self):
                return []

        def build_pipeline(*_arguments, **keywords):
            migration_values.append(keywords["migrate_legacy"])
            pipeline = Pipeline()
            pipeline.event_journal = keywords["event_journal"]
            return pipeline

        composition = RuntimeComposition(
            provider_factories={"fake": lambda *_arguments: FakeProvider([])},
            pipeline_factory=build_pipeline,
        )
        for _ in range(2):
            runtime = composition.create_session_runtime(
                self.configuration(), source=object(), controller=object()
            )
            runtime.close()

        self.assertEqual([True, False], migration_values)

    def test_runtime_closes_pipeline_before_provider_and_retries_only_failed_cleanup(self):
        events = []
        pipeline = FakePipeline(events, RuntimeError("pipeline close failed"))
        provider = FakeProvider(events)

        def build_pipeline(*_arguments, **keywords):
            pipeline.event_journal = keywords["event_journal"]
            return pipeline

        runtime = RuntimeComposition(
            provider_factories={"fake": lambda _model, _endpoint: provider},
            pipeline_factory=build_pipeline,
        ).create_session_runtime(self.configuration(), source=object(), controller=object())

        with self.assertRaisesRegex(RuntimeError, "pipeline close failed"):
            runtime.close()
        self.assertEqual(events, ["pipeline.close", "provider.close"])

        pipeline.close_error = None
        runtime.close()
        self.assertEqual(events, ["pipeline.close", "provider.close", "pipeline.close"])

    def test_migration_lock_serializes_first_session_creation(self):
        migration_values = []
        first_pipeline_started = threading.Event()
        allow_first_pipeline_to_finish = threading.Event()

        class Pipeline:
            event_journal = None

            def close(self):
                self.event_journal.close()

            def load_execution_history(self):
                return []

        def build_pipeline(*_arguments, **keywords):
            migration_values.append(keywords["migrate_legacy"])
            if len(migration_values) == 1:
                first_pipeline_started.set()
                if not allow_first_pipeline_to_finish.wait(timeout=5):
                    raise TimeoutError("test did not release first pipeline")
            pipeline = Pipeline()
            pipeline.event_journal = keywords["event_journal"]
            return pipeline

        composition = RuntimeComposition(
            provider_factories={"fake": lambda *_arguments: FakeProvider([])},
            pipeline_factory=build_pipeline,
        )
        runtimes = []
        errors = []

        def create_runtime():
            try:
                runtimes.append(
                    composition.create_session_runtime(
                        self.configuration(), source=object(), controller=object()
                    )
                )
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=create_runtime)
        second = threading.Thread(target=create_runtime)
        first.start()
        self.assertTrue(first_pipeline_started.wait(timeout=5))
        second.start()
        time.sleep(0.1)
        self.assertEqual([True], migration_values)
        allow_first_pipeline_to_finish.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([True, False], migration_values)
        for runtime in runtimes:
            runtime.close()

    def test_pipeline_initialization_failure_releases_created_provider(self):
        events = []
        provider = FakeProvider(events)

        def fail_pipeline(*_args, **_kwargs):
            raise ValueError("pipeline construction failed")

        composition = RuntimeComposition(
            provider_factories={"fake": lambda _model, _endpoint: provider},
            pipeline_factory=fail_pipeline,
        )
        with self.assertRaisesRegex(ValueError, "pipeline construction failed"):
            composition.create_session_runtime(self.configuration(), source=object(), controller=object())
        self.assertEqual(events, ["provider.close"])

    def test_initialization_and_provider_cleanup_errors_are_both_reported(self):
        provider = FakeProvider([], RuntimeError("provider close failed"))

        def fail_pipeline(*_args, **_kwargs):
            raise ValueError("pipeline construction failed")

        composition = RuntimeComposition(
            provider_factories={"fake": lambda _model, _endpoint: provider},
            pipeline_factory=fail_pipeline,
        )
        with self.assertRaisesRegex(RuntimeError, "pipeline construction failed.*provider close failed"):
            composition.create_session_runtime(self.configuration(), source=object(), controller=object())

    def test_unknown_provider_fails_before_creating_resources(self):
        composition = RuntimeComposition(provider_factories={})
        with self.assertRaisesRegex(ValueError, "Unsupported decision provider: fake"):
            composition.create_session_runtime(self.configuration(), source=object(), controller=object())

    def test_outcome_provider_is_released_after_assessment(self):
        events = []
        provider = FakeProvider(events)
        composition = RuntimeComposition(provider_factories={"Ollama": lambda *_args: provider})

        result = composition.assess_outcome(
            "Ollama", "model-a", "http://localhost", object(), None
        )

        self.assertEqual(result.reason, "fake assessment")
        self.assertEqual(events[0][0], "provider.assess")
        self.assertEqual(events[1], "provider.close")


if __name__ == "__main__":
    unittest.main()
