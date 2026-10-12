import unittest

from ai_game_player.app import Application


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self):
        self.options = {}

    def config(self, **options):
        self.options.update(options)


class FakeSessionController:
    def __init__(self, is_running=True, is_looping=False, has_pending_step=False):
        self.is_running = is_running
        self.is_looping = is_looping
        self.stop_reasons = []
        self.has_pending_step = has_pending_step

    def stop(self, reason):
        self.stop_reasons.append(reason)


class FakeRuntime:
    def __init__(self):
        self.execute_calls = 0

    def run_and_execute(self, **_kwargs):
        self.execute_calls += 1


class FakeObservationService:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        return True


class FakeRoot:
    def __init__(self):
        self.destroy_calls = 0

    def destroy(self):
        self.destroy_calls += 1


class FakeRuntimeLog:
    def __init__(self):
        self.entries = []

    def write(self, *entry):
        self.entries.append(entry)


class ApplicationSessionSafetyTests(unittest.TestCase):
    def make_application(self):
        app = object.__new__(Application)
        app.live_execution = FakeVariable(False)
        app._session_dry_run = False
        app.session_controller = FakeSessionController()
        app._refresh_execution_controls = lambda: None
        return app

    def test_unchecking_live_permission_stops_an_active_live_session(self):
        app = self.make_application()

        app._on_live_execution_changed()

        self.assertEqual(
            app.session_controller.stop_reasons,
            ["live execution permission revoked"],
        )

    def test_continuous_mode_display_matches_the_mode_fixed_at_loop_start(self):
        cases = (
            (False, True, "連続実行中の動作: dry-run（実入力なし）"),
            (True, False, "連続実行中の動作: 実入力"),
        )
        for live_permission, session_dry_run, expected_status in cases:
            with self.subTest(live_permission=live_permission):
                app = object.__new__(Application)
                app.live_execution = FakeVariable(live_permission)
                app._session_dry_run = session_dry_run
                app.session_controller = FakeSessionController(is_looping=True)
                app.execute_button = FakeWidget()
                app.loop_button = FakeWidget()
                app.live_status = FakeWidget()
                app.live_execution_toggle = FakeWidget()

                app._refresh_execution_controls()

                self.assertEqual(app.live_status.options["text"], expected_status)
                self.assertEqual(app.live_execution_toggle.options["state"], "disabled")

    def test_live_permission_cannot_change_between_continuous_steps(self):
        app = self.make_application()
        app.live_execution.set(True)
        app.session_controller.is_looping = True
        app.status_message = None
        app._set_status = lambda message: setattr(app, "status_message", message)

        app._on_live_execution_changed()

        self.assertFalse(app.live_execution.get())
        self.assertEqual(app.session_controller.stop_reasons, [])
        self.assertEqual(app.status_message, "連続実行中は実入力設定を変更できません")

    def test_live_step_revalidates_permission_before_execution(self):
        app = self.make_application()
        runtime = FakeRuntime()

        result = app._perform_session_step(runtime, "execute")

        self.assertEqual(result.terminal_reason, "live execution permission revoked")
        self.assertEqual(runtime.execute_calls, 0)

    def test_application_close_stops_observation_stream_before_destroying_ui(self):
        app = object.__new__(Application)
        app.session_controller = FakeSessionController()
        app.observation_service = FakeObservationService()
        app.runtime_log = FakeRuntimeLog()
        app.root = FakeRoot()

        app.close()

        self.assertEqual(app.session_controller.stop_reasons, ["application_closed"])
        self.assertEqual(app.observation_service.close_calls, 1)
        self.assertEqual(app.root.destroy_calls, 1)


if __name__ == "__main__":
    unittest.main()
