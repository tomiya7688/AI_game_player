import os
import unittest
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture
class ScreenCaptureTest(unittest.TestCase):
    def test_frame_contract(self):
        frame=ScreenFrame(2,3,b"x"*24); self.assertEqual(len(frame.bgra),frame.width*frame.height*4)
    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError): WindowsScreenCapture().capture()