import unittest

from ai_game_player.capture_region import (
    CaptureRegion,
    CaptureRegionMode,
    NormalizedRect,
    PixelRect,
)
from ai_game_player.capture_geometry import crop_screen_frame
from ai_game_player.screen_capture import ScreenFrame


def patterned_frame(width, height, *, screen_bounds=None, window_bounds=None):
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            value = y * width + x
            pixels.extend((value, value, value, 255))
    return ScreenFrame(
        width,
        height,
        bytes(pixels),
        screen_bounds=screen_bounds,
        window_bounds=window_bounds,
    )


class CaptureRegionTest(unittest.TestCase):
    def test_full_screen_keeps_all_pixels_and_records_source_metadata(self):
        raw_frame = patterned_frame(3, 2, screen_bounds=PixelRect(-3, 5, 3, 2))

        frame = crop_screen_frame(
            raw_frame,
            CaptureRegion(CaptureRegionMode.FULL_SCREEN),
        )

        self.assertEqual((frame.width, frame.height), (3, 2))
        self.assertIs(frame.bgra, raw_frame.bgra)
        self.assertEqual(frame.capture_geometry.source, "configured:full_screen")
        self.assertEqual(frame.capture_geometry.confidence, 1.0)
        self.assertEqual(frame.capture_geometry.roi_to_screen(0, 0), (-3.0, 5.0))

    def test_window_mode_keeps_frame_and_requires_a_selected_window(self):
        region = CaptureRegion(CaptureRegionMode.WINDOW)
        frame = crop_screen_frame(
            patterned_frame(2, 2, screen_bounds=PixelRect(10, 20, 2, 2), window_bounds=PixelRect(10, 20, 2, 2)),
            region,
        )

        self.assertEqual(frame.bgra, bytes((0, 0, 0, 255, 1, 1, 1, 255, 2, 2, 2, 255, 3, 3, 3, 255)))
        self.assertEqual(region.capture_handle(123), 123)
        with self.assertRaisesRegex(ValueError, "対象ウィンドウが必要"):
            region.capture_handle(None)

    def test_fixed_region_copies_only_requested_screen_pixels(self):
        region = CaptureRegion(CaptureRegionMode.FIXED_REGION, PixelRect(1, 1, 2, 2))
        raw_frame = patterned_frame(
            4,
            3,
            screen_bounds=PixelRect(0, 0, 4, 3),
            window_bounds=PixelRect(0, 0, 4, 3),
        )

        frame = crop_screen_frame(raw_frame, region)

        self.assertEqual((frame.width, frame.height), (2, 2))
        self.assertEqual(
            [frame.bgra[index] for index in range(0, len(frame.bgra), 4)],
            [5, 6, 9, 10],
        )
        self.assertEqual(frame.capture_geometry.roi_to_screen(0, 0), (1.0, 1.0))
        self.assertEqual(frame.capture_geometry.screen_to_roi(2, 2), (1.0, 1.0))
        self.assertEqual(frame.capture_geometry.roi_to_window(0, 0), (1.0, 1.0))
        self.assertEqual(frame.capture_geometry.window_to_roi(2, 2), (1.0, 1.0))
        self.assertEqual(frame.capture_geometry.roi_to_normalized(1, 1), (0.5, 2 / 3))
        self.assertEqual(frame.capture_geometry.normalized_to_roi(0.5, 2 / 3), (1.0, 1.0))

    def test_fixed_region_fails_instead_of_silently_clipping_screen_edges(self):
        region = CaptureRegion(CaptureRegionMode.FIXED_REGION, PixelRect(3, 0, 2, 1))
        raw_frame = patterned_frame(4, 2, screen_bounds=PixelRect(0, 0, 4, 2))

        with self.assertRaisesRegex(ValueError, "固定ROIの一部が主画面外"):
            crop_screen_frame(raw_frame, region)

    def test_window_relative_region_tracks_window_move_and_resize(self):
        region = CaptureRegion(
            CaptureRegionMode.WINDOW_RELATIVE_REGION,
            NormalizedRect(0.25, 0.25, 0.5, 0.5),
        )
        first_frame = crop_screen_frame(
            patterned_frame(
                4,
                4,
                screen_bounds=PixelRect(100, 200, 4, 4),
                window_bounds=PixelRect(100, 200, 4, 4),
            ),
            region,
        )
        resized_frame = crop_screen_frame(
            patterned_frame(
                8,
                6,
                screen_bounds=PixelRect(200, 300, 8, 6),
                window_bounds=PixelRect(200, 300, 8, 6),
            ),
            region,
        )

        self.assertEqual((first_frame.width, first_frame.height), (2, 2))
        self.assertEqual((resized_frame.width, resized_frame.height), (4, 4))
        self.assertEqual(first_frame.capture_geometry.roi_to_window(0, 0), (1.0, 1.0))
        self.assertEqual(resized_frame.capture_geometry.roi_to_window(0, 0), (2.0, 1.0))
        self.assertEqual(resized_frame.capture_geometry.roi_to_screen(0, 0), (202.0, 301.0))

    def test_relative_region_rejects_missing_window_bounds(self):
        region = CaptureRegion(
            CaptureRegionMode.WINDOW_RELATIVE_REGION,
            NormalizedRect(0, 0, 0.5, 0.5),
        )

        with self.assertRaisesRegex(ValueError, "対象ウィンドウを選び直してください"):
            crop_screen_frame(patterned_frame(4, 4), region)

    def test_capture_region_rejects_invalid_modes_and_normalized_bounds(self):
        with self.assertRaisesRegex(ValueError, "unsupported capture mode"):
            CaptureRegion("automatic")
        with self.assertRaisesRegex(ValueError, "stay within the window"):
            NormalizedRect(0.75, 0, 0.5, 1)


if __name__ == "__main__":
    unittest.main()
