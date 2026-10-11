import unittest
from types import SimpleNamespace

from ai_game_player.models import ScreenObservation
from ai_game_player.observation_service import LiveObservationService
from ai_game_player.screen_capture import ScreenFrame


class LiveObservationServiceTests(unittest.TestCase):
    def test_capture_and_analyzer_dependencies_receive_window_and_live_screen_id(self):
        expected_frame = ScreenFrame(2, 1, bytes(8))
        expected_observation = ScreenObservation("live", 2, 1, ["PLAY"])
        calls = []
        analyzer = SimpleNamespace(
            analyze=lambda frame, screen_id: calls.append(("analyze", frame, screen_id))
            or expected_observation
        )
        service = LiveObservationService(
            capture_frame=lambda window_handle: calls.append(("capture", window_handle))
            or expected_frame,
            analyzer_factory=lambda recognizer: calls.append(("factory", recognizer)) or analyzer,
            ocr_factory=lambda: "fake OCR",
        )

        observation = service.capture_observation(77)

        self.assertIs(observation, expected_observation)
        self.assertEqual(
            calls,
            [
                ("capture", 77),
                ("factory", "fake OCR"),
                ("analyze", expected_frame, "live"),
            ],
        )

    def test_analyzer_failure_is_returned_to_the_caller(self):
        service = LiveObservationService(
            capture_frame=lambda _handle: ScreenFrame(1, 1, bytes(4)),
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda *_args: (_ for _ in ()).throw(RuntimeError("OCR failed"))
            ),
            ocr_factory=lambda: None,
        )

        with self.assertRaisesRegex(RuntimeError, "OCR failed"):
            service.capture_observation(None)


if __name__ == "__main__":
    unittest.main()
