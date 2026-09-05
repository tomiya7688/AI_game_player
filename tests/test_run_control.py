import unittest

from ai_game_player.run_control import RunController


class RunControllerTest(unittest.TestCase):
    def test_stops_and_can_start_again(self):
        controller = RunController()
        self.assertTrue(controller.is_running)
        controller.stop()
        self.assertFalse(controller.is_running)
        with self.assertRaises(RuntimeError):
            controller.ensure_running()
        controller.start()
        controller.ensure_running()