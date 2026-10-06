import unittest

from ai_game_player.run_control import ExecutionCancelled, RunController


class RunControllerTest(unittest.TestCase):
    def test_stops_and_can_start_again(self):
        controller = RunController()
        self.assertTrue(controller.is_running)
        self.assertEqual(controller.rearm_token, 0)
        controller.stop()
        self.assertFalse(controller.is_running)
        with self.assertRaises(RuntimeError):
            controller.ensure_running()
        controller.start()
        controller.ensure_running()
        self.assertEqual(controller.rearm_token, 1)
        controller.start()
        self.assertEqual(controller.rearm_token, 2)

    # {
    #   責務: [test_stop_invalidates_the_previous_execution_generation: 停止後に再開しても旧世代が有効化されないことを検証する]
    #   処理: [停止理由を保持し、停止時に取得したtokenが再開後に拒否されることを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_stop_invalidates_the_previous_execution_generation(self):
        controller = RunController()
        old_token = controller.rearm_token
        controller.stop("F12")
        self.assertEqual(controller.stop_reason, "F12")
        controller.start()

        with self.assertRaisesRegex(ExecutionCancelled, "停止理由: F12"):
            controller.ensure_running(old_token)
