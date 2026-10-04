import unittest

from ai_game_player.app import Application


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeSessionController:
    def __init__(self, is_running=True):
        self.is_running = is_running
        self.stop_reasons = []

    def stop(self, reason):
        self.stop_reasons.append(reason)


class FakeRuntime:
    def __init__(self):
        self.execute_calls = 0

    def run_and_execute(self, **_kwargs):
        self.execute_calls += 1


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

    def test_live_step_revalidates_permission_before_execution(self):
        app = self.make_application()
        runtime = FakeRuntime()

        result = app._perform_session_step(runtime, "execute")

        self.assertEqual(result.terminal_reason, "live execution permission revoked")
        self.assertEqual(runtime.execute_calls, 0)


if __name__ == "__main__":
    unittest.main()
