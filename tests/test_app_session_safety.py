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
        self.has_pending_step = False

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
