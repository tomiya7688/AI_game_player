import unittest
from threading import Event
from time import monotonic
from types import SimpleNamespace

from ai_game_player.models import ScreenObservation
from ai_game_player.observation_service import (
    LiveObservationService,
    NANOSECONDS_PER_SECOND,
)
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.shared_frame_producer import FrameDeliveryMode, FrameProducerError


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
            capture_interval_seconds=10.0,
            frame_wait_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)

        observation = service.capture_observation(77)

        self.assertEqual(observation.screen_id, expected_observation.screen_id)
        self.assertEqual(observation.width, expected_observation.width)
        self.assertEqual(observation.height, expected_observation.height)
        self.assertEqual(observation.ocr_text, expected_observation.ocr_text)
        capture_metadata = observation.features["capture_metadata"]
        self.assertRegex(capture_metadata["frame_id"], r"^[0-9a-f]+:0:1$")
        self.assertEqual(capture_metadata["source_generation"], 0)
        self.assertEqual(
            capture_metadata["monotonic_ns"],
            round(expected_frame.captured_at * NANOSECONDS_PER_SECOND),
        )
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
            capture_interval_seconds=10.0,
            frame_wait_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)

        with self.assertRaisesRegex(RuntimeError, "OCR failed"):
            service.capture_observation(None)

    def test_window_change_replaces_source_and_advances_the_frame_generation(self):
        captured_handles = []
        service = LiveObservationService(
            capture_frame=lambda handle: captured_handles.append(handle)
            or ScreenFrame(1, 1, bytes(4)),
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=0.001,
            frame_wait_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)

        first_observation = service.capture_observation(77)
        second_observation = service.capture_observation(88)

        first_metadata = first_observation.features["capture_metadata"]
        second_metadata = second_observation.features["capture_metadata"]
        self.assertIn(77, captured_handles)
        self.assertIn(88, captured_handles)
        self.assertEqual(first_metadata["source_generation"], 0)
        self.assertEqual(second_metadata["source_generation"], 1)
        self.assertNotEqual(first_metadata["frame_id"], second_metadata["frame_id"])

    def test_stale_frame_is_discarded_until_a_fresh_frame_arrives(self):
        captured_frames = []

        def capture_frame(_handle):
            captured_frames.append(None)
            captured_at = monotonic() - 10 if len(captured_frames) == 1 else monotonic()
            return ScreenFrame(1, 1, bytes(4), captured_at=captured_at)

        service = LiveObservationService(
            capture_frame=capture_frame,
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=0.001,
            frame_wait_timeout_seconds=1.0,
            maximum_frame_age_seconds=0.1,
        )
        self.addCleanup(service.close)

        observation = service.capture_observation(77)

        self.assertGreaterEqual(len(captured_frames), 2)
        capture_time_seconds = (
            observation.features["capture_metadata"]["monotonic_ns"]
            / NANOSECONDS_PER_SECOND
        )
        self.assertLess(monotonic() - capture_time_seconds, 0.1)

    def test_stale_frames_time_out_instead_of_becoming_observations(self):
        service = LiveObservationService(
            capture_frame=lambda _handle: ScreenFrame(
                1,
                1,
                bytes(4),
                captured_at=monotonic() - 10,
            ),
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=0.001,
            frame_wait_timeout_seconds=0.03,
            maximum_frame_age_seconds=0.005,
        )
        self.addCleanup(service.close)

        with self.assertRaisesRegex(TimeoutError, "対象ウィンドウを選び直して"):
            service.capture_observation(77)

    def test_capture_failure_is_returned_to_the_caller(self):
        service = LiveObservationService(
            capture_frame=lambda _handle: (_ for _ in ()).throw(RuntimeError("window capture failed")),
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=10.0,
            frame_wait_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)

        with self.assertRaisesRegex(FrameProducerError, "RuntimeError: window capture failed"):
            service.capture_observation(77)

    def test_additional_consumer_receives_the_same_frame_as_observation(self):
        expected_frame = ScreenFrame(1, 1, bytes(4))
        capture_started = Event()
        release_capture = Event()

        def capture_frame(_handle):
            capture_started.set()
            if not release_capture.wait(1.0):
                raise TimeoutError("Test capture was not released")
            return expected_frame

        service = LiveObservationService(
            capture_frame=capture_frame,
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=10.0,
            frame_wait_timeout_seconds=1.0,
        )
        self.addCleanup(service.close)
        recorder_subscription = service.subscribe_frames(77, FrameDeliveryMode.ORDERED, max_pending=2)
        self.assertTrue(capture_started.wait(1.0))
        release_capture.set()

        observation = service.capture_observation(77)
        recorded_packet = recorder_subscription.get(timeout=1.0)

        self.assertIsNotNone(recorded_packet)
        self.assertIs(recorded_packet.frame, expected_frame)
        capture_metadata = observation.features["capture_metadata"]
        self.assertEqual(capture_metadata["frame_id"].split(":")[-2:], ["0", str(recorded_packet.frame_id)])
        recorder_subscription.close()

    def test_close_stops_the_stream_and_rejects_later_capture(self):
        service = LiveObservationService(
            capture_frame=lambda _handle: ScreenFrame(1, 1, bytes(4)),
            analyzer_factory=lambda _recognizer: SimpleNamespace(
                analyze=lambda frame, screen_id: ScreenObservation(screen_id, frame.width, frame.height)
            ),
            ocr_factory=lambda: None,
            capture_interval_seconds=10.0,
            frame_wait_timeout_seconds=1.0,
        )

        service.capture_observation(77)

        self.assertTrue(service.close())
        self.assertTrue(service.close())
        with self.assertRaisesRegex(RuntimeError, "is closed"):
            service.capture_observation(77)


if __name__ == "__main__":
    unittest.main()
