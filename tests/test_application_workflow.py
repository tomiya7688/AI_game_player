import json
from dataclasses import replace
from pathlib import Path
import unittest

from ai_game_player.capture_region import CaptureRegion, CaptureRegionMode, PixelRect
from ai_game_player.application_workflow import (
    ApplicationSettings,
    ApplicationWorkflowService,
    MemorySource,
)
from ai_game_player.models import ScreenObservation


class FakeConfigStore:
    def __init__(self):
        self.saved = []

    def save(self, config):
        self.saved.append(config)


class FakeRuntimeComposition:
    def __init__(self):
        self.evaluations = []

    def explain_actions(self, observation, candidates):
        result = [{"action_id": candidate.action_id, "accepted": True} for candidate in candidates]
        self.evaluations.append((observation, candidates, result))
        return result


class ApplicationWorkflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.config_store = FakeConfigStore()
        self.composition = FakeRuntimeComposition()
        self.memory_source = MemorySource()
        self.service = ApplicationWorkflowService(
            self.config_store,
            self.composition,
            self.memory_source,
        )
        self.settings = ApplicationSettings(
            provider="Ollama",
            model="small-model",
            endpoint="http://127.0.0.1:11434",
            personality="curious",
            purpose="find the next screen",
            live_execution=True,
            input_mode="window_message",
        )
        self.observation_json = json.dumps(
            {"screen_id": "title", "width": 640, "height": 480, "ocr_text": ["START"]}
        )
        self.candidates_json = json.dumps(
            [{"action_id": "start", "kind": "click", "label": "START", "x": 100, "y": 200}]
        )

    def test_execute_saves_settings_evaluates_candidates_and_updates_pipeline_source(self):
        result = self.service.prepare_step(
            self.settings,
            self.observation_json,
            self.candidates_json,
            "execute",
        )

        observation, candidates = self.memory_source.read()
        self.assertEqual(observation.screen_id, "title")
        self.assertEqual([candidate.action_id for candidate in candidates], ["start"])
        self.assertEqual(self.config_store.saved[0].provider, "Ollama")
        self.assertEqual(result.observation, observation)
        self.assertEqual(result.candidates, candidates)
        self.assertEqual(result.evaluation, [{"action_id": "start", "accepted": True}])
        self.assertFalse(result.dry_run)

    def test_prepare_step_persists_the_capture_region_selected_by_the_user(self):
        capture_region = CaptureRegion(
            CaptureRegionMode.FIXED_REGION,
            PixelRect(12, 34, 320, 240),
        )

        self.service.prepare_step(
            replace(self.settings, capture_region=capture_region),
            self.observation_json,
            self.candidates_json,
            "decision",
        )

        self.assertEqual(self.config_store.saved[0].capture_region, capture_region)

    def test_session_runtime_uses_the_validated_source_and_explicit_target_snapshot(self):
        created = []
        self.composition.create_session_runtime = lambda configuration, **dependencies: created.append(
            (configuration, dependencies)
        ) or "session-runtime"
        controller = object()

        def cursor_callback(*_arguments):
            return None

        runtime_log = object()

        runtime = self.service.create_session_runtime(
            self.settings,
            game_directory=Path("data/games/test"),
            dry_run=False,
            window_handle=123,
            window_process_id=456,
            controller=controller,
            cursor_callback=cursor_callback,
            runtime_log=runtime_log,
        )

        configuration, dependencies = created[0]
        self.assertEqual(runtime, "session-runtime")
        self.assertEqual(configuration.provider_name, "Ollama")
        self.assertEqual(configuration.window_handle, 123)
        self.assertEqual(configuration.window_process_id, 456)
        self.assertIs(dependencies["source"], self.memory_source)
        self.assertIs(dependencies["controller"], controller)
        self.assertIs(dependencies["automated_cursor_position_callback"], cursor_callback)
        self.assertIs(dependencies["runtime_log"], runtime_log)

    def test_decision_and_disallowed_live_input_both_remain_dry_run(self):
        for command, live_execution in (("decision", True), ("execute", False)):
            with self.subTest(command=command, live_execution=live_execution):
                result = self.service.prepare_step(
                    replace(self.settings, live_execution=live_execution),
                    self.observation_json,
                    self.candidates_json,
                    command,
                )
                self.assertTrue(result.dry_run)

    def test_invalid_candidate_document_does_not_replace_the_previous_source(self):
        original = ScreenObservation("previous", 320, 240)
        self.memory_source.update(original, [])

        with self.assertRaisesRegex(ValueError, "候補の配列"):
            self.service.prepare_step(
                self.settings,
                self.observation_json,
                json.dumps({"action_id": "start"}),
                "execute",
            )

        observation, candidates = self.memory_source.read()
        self.assertIs(observation, original)
        self.assertEqual(candidates, [])
        self.assertEqual(self.composition.evaluations, [])

    def test_unknown_command_is_rejected_before_settings_are_saved(self):
        with self.assertRaisesRegex(ValueError, "Unsupported session command"):
            self.service.prepare_step(
                self.settings,
                self.observation_json,
                self.candidates_json,
                "restart",
            )

        self.assertEqual(self.config_store.saved, [])


if __name__ == "__main__":
    unittest.main()
