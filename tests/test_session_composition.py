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

    def run(self, **_arguments):
        return ActionDecision("start", "fake decision", "fake")

    def run_and_execute(self, **_arguments):
        return "executed"

    def close(self):
        self.events.append("pipeline.close")
        if self.close_error is not None:
            raise self.close_error


class SessionCompositionTests(unittest.TestCase):
    def configuration(self, **changes):
        values = {
            "provider_name": "fake",
            "model": "model-a",
            "endpoint": "http://localhost",
            "game_directory": Path("data/games/test"),
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
        self.assertEqual(build_arguments["arguments"], (source, Path("data/games/test"), provider, controller))
        self.assertEqual(
            {key: build_arguments["keywords"][key] for key in (
                "dry_run", "window_handle", "input_mode", "window_process_id"
            )},
            {
                "dry_run": True,
                "window_handle": 12,
                "input_mode": "window_message",
                "window_process_id": 34,
            },
        )

    def test_runtime_closes_pipeline_before_provider_and_retries_only_failed_cleanup(self):
        events = []
        pipeline = FakePipeline(events, RuntimeError("pipeline close failed"))
        provider = FakeProvider(events)
        runtime = RuntimeComposition(
            provider_factories={"fake": lambda _model, _endpoint: provider},
            pipeline_factory=lambda *_args, **_kwargs: pipeline,
        ).create_session_runtime(self.configuration(), source=object(), controller=object())

        with self.assertRaisesRegex(RuntimeError, "pipeline close failed"):
            runtime.close()
        self.assertEqual(events, ["pipeline.close", "provider.close"])

        pipeline.close_error = None
        runtime.close()
        self.assertEqual(events, ["pipeline.close", "provider.close", "pipeline.close"])

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
