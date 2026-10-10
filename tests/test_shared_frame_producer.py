import threading
import time
import unittest

from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.shared_frame_producer import (
    FrameDeliveryMode,
    FrameProducerError,
    SharedFrameProducer,
)


class CountingCapture:
    def __init__(self, width=1, height=1):
        self.width = width
        self.height = height
        self.count = 0
        self.lock = threading.Lock()

    def capture(self):
        with self.lock:
            self.count += 1
            count = self.count
        pixel = bytes([count % 255, 0, 0, 255])
        return ScreenFrame(self.width, self.height, pixel * self.width * self.height)


class FailingCapture:
    def capture(self):
        raise ValueError("capture backend rejected the source")


class InvalidFrameCapture:
    def capture(self):
        return ScreenFrame(0, 1, b"")


class DecreasingTimestampCapture:
    def __init__(self):
        self.timestamps = iter((2.0, 1.0))

    def capture(self):
        return ScreenFrame(1, 1, bytes([0, 0, 0, 255]), captured_at=next(self.timestamps))


class CaptureThenFail:
    def __init__(self):
        self.capture_count = 0

    def capture(self):
        self.capture_count += 1
        if self.capture_count > 1:
            raise RuntimeError("capture source disappeared")
        return ScreenFrame(1, 1, bytes([0, 0, 0, 255]))


class ObsoleteCapture:
    def __init__(self, *, fail=False, malformed=False):
        self.fail = fail
        self.malformed = malformed
        self.started = threading.Event()
        self.release = threading.Event()

    def capture(self):
        self.started.set()
        self.release.wait()
        if self.fail:
            raise RuntimeError("obsolete source disappeared")
        if self.malformed:
            return ScreenFrame(0, 1, b"")
        return ScreenFrame(1, 1, bytes([0, 0, 0, 255]))


class BlockingCapture:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def capture(self):
        self.started.set()
        self.release.wait()
        return ScreenFrame(1, 1, bytes([0, 0, 0, 255]))


class SharedFrameProducerTest(unittest.TestCase):
    def wait_for_capture_count(self, producer, count, timeout=1.0):
        deadline = time.monotonic() + timeout
        while producer.metrics.captured_frames < count and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertGreaterEqual(producer.metrics.captured_frames, count)

    def test_two_subscribers_receive_the_same_capture_packet(self):
        producer = SharedFrameProducer(CountingCapture(), interval_seconds=1.0)
        ordered = producer.subscribe(FrameDeliveryMode.ORDERED, max_pending=2)
        latest = producer.subscribe()

        producer.start()
        ordered_packet = ordered.get(timeout=1.0)
        latest_packet = latest.get(timeout=1.0)
        self.assertIsNotNone(ordered_packet)
        self.assertIs(ordered_packet, latest_packet)
        self.assertEqual(ordered_packet.frame_id, 1)
        self.assertEqual(ordered_packet.frame.captured_at > 0, True)
        self.assertTrue(producer.stop(timeout=1.0))
        self.assertIsNone(ordered.get(timeout=0.1))
        self.assertIsNone(latest.get(timeout=0.1))

    def test_timeout_and_normal_end_are_distinguished_by_subscription_state(self):
        producer = SharedFrameProducer(CountingCapture(), interval_seconds=1.0)
        subscriber = producer.subscribe()
        self.assertIsNone(subscriber.get(timeout=0))
        self.assertFalse(subscriber.is_closed)

        self.assertTrue(producer.stop(timeout=0.1))
        self.assertIsNone(subscriber.get(timeout=0))
        self.assertTrue(subscriber.is_closed)

    def test_latest_subscriber_replaces_unread_frames_and_reports_drops(self):
        producer = SharedFrameProducer(CountingCapture(), interval_seconds=0.001)
        subscriber = producer.subscribe(FrameDeliveryMode.LATEST, max_pending=20)
        producer.start()
        self.wait_for_capture_count(producer, 5)
        self.assertTrue(producer.stop(timeout=1.0))

        packet = subscriber.get(timeout=0.1)
        self.assertIsNotNone(packet)
        self.assertEqual(subscriber.pending_frames, 0)
        self.assertLessEqual(subscriber.pending_frames, 1)
        self.assertGreater(subscriber.dropped_frames, 0)
        self.assertGreater(producer.metrics.dropped_frames, 0)

    def test_ordered_subscriber_keeps_old_frames_and_drops_new_frames_when_full(self):
        producer = SharedFrameProducer(CountingCapture(), interval_seconds=0.001)
        subscriber = producer.subscribe(FrameDeliveryMode.ORDERED, max_pending=1)
        producer.start()
        self.wait_for_capture_count(producer, 5)
        self.assertTrue(producer.stop(timeout=1.0))

        first_packet = subscriber.get(timeout=0.1)
        self.assertEqual(first_packet.frame_id, 1)
        self.assertGreaterEqual(subscriber.dropped_frames, 4)

    def test_source_replacement_changes_generation_and_frame_dimensions(self):
        producer = SharedFrameProducer(CountingCapture(2, 2), interval_seconds=0.01)
        subscriber = producer.subscribe()
        producer.start()
        first_packet = subscriber.get(timeout=1.0)
        self.assertEqual(first_packet.source_generation, 0)
        self.assertEqual((first_packet.frame.width, first_packet.frame.height), (2, 2))

        generation = producer.replace_source(CountingCapture(4, 1))
        second_packet = subscriber.get(timeout=1.0)
        self.assertEqual(generation, 1)
        self.assertEqual(second_packet.source_generation, 1)
        self.assertGreater(second_packet.frame_id, first_packet.frame_id)
        self.assertEqual((second_packet.frame.width, second_packet.frame.height), (4, 1))
        self.assertTrue(producer.stop(timeout=1.0))

    def test_source_replacement_waits_for_inflight_publication_then_flushes_old_frame(self):
        producer = SharedFrameProducer(CountingCapture(), interval_seconds=0.005)
        subscriber = producer.subscribe()
        publishing = threading.Event()
        release_publication = threading.Event()
        replacement_complete = threading.Event()
        publish_packet = subscriber._publish

        def pause_publication(packet):
            publishing.set()
            release_publication.wait(timeout=1.0)
            return publish_packet(packet)

        subscriber._publish = pause_publication
        producer.start()
        self.assertTrue(publishing.wait(timeout=1.0))
        replacement_thread = threading.Thread(
            target=lambda: (
                producer.replace_source(CountingCapture(3, 2)),
                replacement_complete.set(),
            )
        )
        replacement_thread.start()
        try:
            self.assertFalse(replacement_complete.wait(timeout=0.02))
        finally:
            release_publication.set()
        self.assertTrue(replacement_complete.wait(timeout=1.0))

        packet = subscriber.get(timeout=1.0)
        self.assertEqual(packet.source_generation, 1)
        self.assertEqual((packet.frame.width, packet.frame.height), (3, 2))
        self.assertTrue(producer.stop(timeout=1.0))

    def test_source_replacement_discards_capture_already_running_on_old_source(self):
        old_source = BlockingCapture()
        producer = SharedFrameProducer(old_source, interval_seconds=0.001)
        subscriber = producer.subscribe()
        producer.start()
        self.assertTrue(old_source.started.wait(timeout=1.0))

        generation = producer.replace_source(CountingCapture(3, 1))
        old_source.release.set()
        packet = subscriber.get(timeout=1.0)

        self.assertEqual(packet.source_generation, generation)
        self.assertEqual((packet.frame.width, packet.frame.height), (3, 1))
        self.assertGreaterEqual(producer.metrics.dropped_frames, 1)
        self.assertTrue(producer.stop(timeout=1.0))

    def test_source_replacement_ignores_obsolete_capture_failures_and_invalid_frames(self):
        for old_source in (
            ObsoleteCapture(fail=True),
            ObsoleteCapture(malformed=True),
        ):
            with self.subTest(failure=old_source.fail, malformed=old_source.malformed):
                producer = SharedFrameProducer(old_source, interval_seconds=0.001)
                subscriber = producer.subscribe()
                producer.start()
                self.assertTrue(old_source.started.wait(timeout=1.0))

                generation = producer.replace_source(CountingCapture(2, 3))
                old_source.release.set()
                packet = subscriber.get(timeout=1.0)

                self.assertEqual(packet.source_generation, generation)
                self.assertEqual((packet.frame.width, packet.frame.height), (2, 3))
                self.assertIsNone(producer.metrics.error_message)
                self.assertGreaterEqual(producer.metrics.dropped_frames, 1)
                self.assertTrue(producer.stop(timeout=1.0))

    def test_capture_failure_reaches_subscriber_and_stops_producer(self):
        producer = SharedFrameProducer(FailingCapture())
        subscriber = producer.subscribe()
        producer.start()

        with self.assertRaisesRegex(FrameProducerError, "capture backend rejected the source"):
            subscriber.get(timeout=1.0)

        self.assertTrue(producer.stop(timeout=1.0))
        self.assertFalse(producer.metrics.is_running)
        self.assertIn("ValueError", producer.metrics.error_message)
        with self.assertRaisesRegex(RuntimeError, "started once"):
            producer.start()

    def test_capture_failure_counts_queued_packets_discarded_from_metrics(self):
        producer = SharedFrameProducer(CaptureThenFail(), interval_seconds=0.01)
        subscriber = producer.subscribe()
        producer.start()
        deadline = time.monotonic() + 1.0
        while producer.metrics.error_message is None and time.monotonic() < deadline:
            time.sleep(0.001)

        with self.assertRaisesRegex(FrameProducerError, "capture source disappeared"):
            subscriber.get(timeout=0.1)
        self.assertEqual(producer.metrics.published_frames, 1)
        self.assertEqual(producer.metrics.dropped_frames, 1)
        self.assertEqual(subscriber.dropped_frames, 1)

    def test_malformed_frame_is_rejected_and_reported(self):
        producer = SharedFrameProducer(InvalidFrameCapture())
        subscriber = producer.subscribe()
        producer.start()

        with self.assertRaisesRegex(FrameProducerError, "invalid BGRA frame or timestamp"):
            subscriber.get(timeout=1.0)

        self.assertEqual(producer.metrics.captured_frames, 0)

    def test_capture_timestamps_must_not_move_backwards(self):
        producer = SharedFrameProducer(DecreasingTimestampCapture(), interval_seconds=0.001)
        subscriber = producer.subscribe()
        producer.start()
        first_packet = subscriber.get(timeout=1.0)
        self.assertEqual(first_packet.frame.captured_at, 2.0)

        with self.assertRaisesRegex(FrameProducerError, "non-decreasing monotonic clock"):
            subscriber.get(timeout=1.0)

        self.assertEqual(producer.metrics.captured_frames, 2)

    def test_stop_timeout_does_not_wait_forever_for_blocked_capture(self):
        capture = BlockingCapture()
        producer = SharedFrameProducer(capture)
        producer.subscribe()
        producer.start()
        self.assertTrue(capture.started.wait(timeout=1.0))

        self.assertFalse(producer.stop(timeout=0.001))
        with self.assertRaisesRegex(RuntimeError, "stopped or is stopping"):
            producer.subscribe()
        with self.assertRaisesRegex(RuntimeError, "stopped or is stopping"):
            producer.replace_source(CountingCapture())
        capture.release.set()
        self.assertTrue(producer.stop(timeout=1.0))

    def test_invalid_cadence_and_queue_capacity_are_rejected(self):
        for interval in (0, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaisesRegex(ValueError, "interval_seconds"):
                SharedFrameProducer(CountingCapture(), interval_seconds=interval)
        for capacity in (0, 1.5, True):
            with self.subTest(capacity=capacity), self.assertRaisesRegex(ValueError, "max_pending"):
                SharedFrameProducer(CountingCapture()).subscribe(
                    FrameDeliveryMode.ORDERED, max_pending=capacity
                )


if __name__ == "__main__":
    unittest.main()
