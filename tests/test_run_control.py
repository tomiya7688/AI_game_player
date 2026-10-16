import threading
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

    def test_stop_waits_for_generation_guarded_commit(self):
        controller = RunController()
        run_token = controller.rearm_token
        commit_started = threading.Event()
        allow_commit_to_finish = threading.Event()
        stop_started = threading.Event()
        stop_finished = threading.Event()
        commit_finished = threading.Event()

        def commit():
            commit_started.set()
            if not allow_commit_to_finish.wait(timeout=2):
                raise TimeoutError("test did not release guarded commit")
            commit_finished.set()

        def stop():
            stop_started.set()
            controller.stop("Stop during commit")
            stop_finished.set()

        commit_worker = threading.Thread(target=lambda: controller.run_if_current(run_token, commit), daemon=True)
        stop_worker = threading.Thread(target=stop, daemon=True)
        commit_worker.start()
        self.assertTrue(commit_started.wait(timeout=2))
        lock_available = controller._lock.acquire(blocking=False)
        if lock_available:
            controller._lock.release()
        self.assertFalse(lock_available)
        stop_worker.start()
        self.assertTrue(stop_started.wait(timeout=2))
        self.assertFalse(stop_finished.wait(timeout=0.05))

        allow_commit_to_finish.set()
        commit_worker.join(timeout=2)
        stop_worker.join(timeout=2)

        self.assertFalse(commit_worker.is_alive())
        self.assertFalse(stop_worker.is_alive())
        self.assertTrue(commit_finished.is_set())
        self.assertTrue(stop_finished.is_set())
        with self.assertRaisesRegex(ExecutionCancelled, "Stop during commit"):
            controller.ensure_running(run_token)
