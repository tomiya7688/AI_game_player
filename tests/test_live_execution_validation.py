import ctypes
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player import app as app_module
from ai_game_player.app import Application
from ai_game_player.models import ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.run_control import RunController


# {
#   責務: [FakeVariable: UI変数のget操作をテスト用に代替する]
#   フィールド: [value: getで返す値]
# }
class FakeVariable:
    # {
    #   責務: [__init__: fake変数の値を初期化する]
    #   処理: [valueを保持する]
    #   引数: [value: 返却値]
    #   戻り値: []
    # }
    def __init__(self, value):
        self.value = value

    # {
    #   責務: [get: fake変数の現在値を返す]
    #   処理: [保持値をそのまま返す]
    #   引数: []
    #   戻り値: [object: 保持値]
    # }
    def get(self):
        return self.value


# {
#   責務: [FakeUser32: 対象ウィンドウの有効性と所有PIDを模擬する]
#   フィールド: [valid_handles: 有効HWND一覧, process_id: 現在の所有PID]
# }
class FakeUser32:
    # {
    #   責務: [__init__: 有効HWNDと現在PIDを初期化する]
    #   処理: [fake Win32応答値を保持する]
    #   引数: [valid_handles: 有効HWND一覧, process_id: 返すPID]
    #   戻り値: []
    # }
    def __init__(self, valid_handles, process_id=456):
        self.valid_handles = set(valid_handles)
        self.checked_handles = []
        self.process_id = process_id

    # {
    #   責務: [IsWindow: HWND有効性を模擬する]
    #   処理: [検証したHWNDを記録し有効性を返す]
    #   引数: [handle: 検査HWND]
    #   戻り値: [int: Win32形式の有効フラグ]
    # }
    def IsWindow(self, handle):
        self.checked_handles.append(handle)
        return int(handle in self.valid_handles)

    # {
    #   責務: [GetWindowThreadProcessId: fake HWNDの所有PIDをWin32形式で返す]
    #   処理: [指定ポインターへprocess_idを設定する]
    #   引数: [_handle: 対象HWND, process_id_pointer: PID出力先]
    #   戻り値: [int: 有効なスレッドID]
    # }
    def GetWindowThreadProcessId(self, _handle, process_id_pointer):
        ctypes.cast(process_id_pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.process_id
        return 1


# {
#   責務: [RecordingLog: 実行ログ記録をテスト内に保持する]
#   フィールド: [records: 記録された引数列]
# }
class RecordingLog:
    # {
    #   責務: [__init__: 空のログ記録領域を作成する]
    #   処理: [recordsを空配列にする]
    #   引数: []
    #   戻り値: []
    # }
    def __init__(self):
        self.records = []

    # {
    #   責務: [write: ログ記録の引数を蓄積する]
    #   処理: [recordをrecordsへ追加する]
    #   引数: [record: 1件分のログ値]
    #   戻り値: []
    # }
    def write(self, *record):
        self.records.append(record)


# {
#   責務: [LiveExecutionValidationTests: 実入力対象の識別と連続実行の停止条件を検証する]
#   フィールド: [fake Applicationとfake Win32 APIを各testで構成する]
# }
class LiveExecutionValidationTests(unittest.TestCase):
    # {
    #   責務: [make_application: UI依存なしで検証可能なApplicationを構築する]
    #   処理: [実行フラグ・入力方式・選択対象とHWND/PIDを設定する]
    #   引数: [live: 実入力状態, mode: 入力方式, handles: 表示名とHWNDの対応]
    #   戻り値: [Application: テスト用インスタンス]
    # }
    def make_application(self, *, live=True, mode="window_message", handles=None):
        application = Application.__new__(Application)
        application.live_execution = FakeVariable(live)
        application.input_mode = FakeVariable(mode)
        application.window_choice = FakeVariable("Game")
        application.window_handles = handles or {}
        application.window_process_ids = {name: 456 for name in application.window_handles}
        application._execution_in_progress = False
        application._loop_active = False
        return application

    # {
    #   責務: [test_live_execution_requires_a_target_in_every_input_mode: 実入力では方式によらず対象が必要なことを検証する]
    #   処理: [各入力方式で対象未選択時のValueErrorを検証する]
    #   引数: []
    #   戻り値: []
    # }
    def test_live_execution_requires_a_target_in_every_input_mode(self):
        for mode in ("mouse", "window_message"):
            with self.subTest(mode=mode):
                application = self.make_application(mode=mode)
                with self.assertRaisesRegex(ValueError, "対象ウィンドウの選択"):
                    application._validate_live_execution()

    # {
    #   責務: [test_dry_run_does_not_require_a_live_target: dry-runが実入力対象を要求しないことを検証する]
    #   処理: [dry-runの対象検証が例外なく完了することを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_dry_run_does_not_require_a_live_target(self):
        application = self.make_application(live=False)
        application._validate_live_execution()

    # {
    #   責務: [test_lost_window_handle_is_rejected: 無効HWNDへの実入力を拒否することを検証する]
    #   処理: [IsWindowが無効を返す対象の検証結果を確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_lost_window_handle_is_rejected(self):
        application = self.make_application(handles={"Game": 123})
        user32 = FakeUser32(valid_handles=[])

        with patch.object(app_module.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(ValueError, "失効"):
                application._validate_live_execution()

        self.assertEqual(user32.checked_handles, [123])

    # {
    #   責務: [test_recycled_window_handle_with_different_process_is_rejected: HWND再利用時に別プロセスへの入力を拒否する]
    #   処理: [列挙時PIDと現在PIDが異なる対象を検証する]
    #   引数: []
    #   戻り値: []
    # }
    def test_recycled_window_handle_with_different_process_is_rejected(self):
        application = self.make_application(handles={"Game": 123})
        user32 = FakeUser32(valid_handles=[123], process_id=999)

        with patch.object(app_module.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(ValueError, "別の対象"):
                application._validate_live_execution()

        self.assertEqual(user32.checked_handles, [123])

    # {
    #   責務: [test_start_loop_refuses_invalid_target_before_starting_controller: 不正対象では連続実行を開始しないことを確認する]
    #   処理: [対象未選択の開始要求がcontrollerを起動しないことを検証する]
    #   引数: []
    #   戻り値: []
    # }
    def test_start_loop_refuses_invalid_target_before_starting_controller(self):
        application = self.make_application()
        application.runtime_log = RecordingLog()
        application.controller = SimpleNamespace(start=lambda: self.fail("controller started"))
        application._set_status = lambda _message: None

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application.start_loop()

        show_error.assert_called_once()

    # {
    #   責務: [test_loop_stops_when_step_fails: 評価後にstepを開始できなければ連続実行を止める]
    #   処理: [失敗結果を返す非同期起動後にstopが呼ばれることを検証する]
    #   引数: []
    #   戻り値: []
    # }
    def test_loop_stops_when_step_fails(self):
        application = self.make_application(live=False)
        application.controller = SimpleNamespace(is_running=True, rearm_token=0)
        application._loop_active = True
        application.loop_job = None
        application.runtime_log = RecordingLog()
        application.loop_guard = SimpleNamespace(observe=lambda _observation: False)
        application._run_is_current = lambda _token: True
        application.run_and_execute = lambda **_kwargs: False
        stopped = []
        application.stop = lambda *_args: stopped.append(True)
        application._continue_loop_with_assessment(
            ScreenObservation("play", 1280, 720, []),
            SimpleNamespace(status="ongoing"),
            0,
        )

        self.assertEqual(stopped, [True])

    def test_poll_background_results_discards_superseded_assessment(self):
        application = Application.__new__(Application)
        earlier_observation = ScreenObservation("earlier", 1280, 720, [])
        latest_observation = ScreenObservation("latest", 1280, 720, [])
        previous_observation = ScreenObservation("previous", 1280, 720, [])
        earlier_assessment = OutcomeAssessment("failure", 0.9, "older result")
        latest_assessment = OutcomeAssessment("ongoing", 0.4, "latest result")
        displayed_statuses = []
        application._background_results = queue.Queue()
        application._latest_assessment_task_id = 2
        application._loop_active = False
        application.controller = SimpleNamespace(is_running=False, rearm_token=0, stop_reason=None)
        application.runtime_log = RecordingLog()
        application.provider = FakeVariable("Rule")
        application.previous_observation = previous_observation
        application.current_assessment = None
        application.outcome = SimpleNamespace(config=lambda **values: displayed_statuses.append(values["text"]))
        application.root = SimpleNamespace(after=lambda *_args: None)
        application._background_results.put(("assessment", 2, None, "assessment", latest_assessment, None, False, latest_observation))
        application._background_results.put(("assessment", 1, None, "assessment", earlier_assessment, None, False, earlier_observation))

        application._poll_background_results()

        self.assertIs(application.previous_observation, latest_observation)
        self.assertIs(application.current_assessment, latest_assessment)
        self.assertEqual(displayed_statuses, ["状態: ongoing (40%)"])
        self.assertEqual(application.runtime_log.records[-1][1], "outcome_result_discarded")
        self.assertEqual(application.runtime_log.records[-1][2]["task_id"], 1)
        self.assertEqual(application.runtime_log.records[-1][2]["latest_task_id"], 2)

    def test_start_assessment_worker_records_task_after_successful_start(self):
        application = Application.__new__(Application)
        application._background_task_counter = 5
        application._latest_assessment_task_id = 4
        application.previous_observation = None
        application.provider = FakeVariable("Rule")
        application.model = FakeVariable("")
        application.endpoint = FakeVariable("")
        worker = SimpleNamespace(start=lambda: None)

        with patch.object(app_module.threading, "Thread", return_value=worker):
            application._start_assessment_worker(
                ScreenObservation("current", 1280, 720, []),
                loop_step=False,
                run_token=None,
            )

        self.assertEqual(application._background_task_counter, 6)
        self.assertEqual(application._latest_assessment_task_id, 6)

    def test_failed_assessment_worker_start_preserves_previous_latest_task(self):
        application = Application.__new__(Application)
        application._background_task_counter = 5
        application._latest_assessment_task_id = 4
        application.previous_observation = None
        application.provider = FakeVariable("Rule")
        application.model = FakeVariable("")
        application.endpoint = FakeVariable("")
        application.runtime_log = RecordingLog()
        statuses = []
        application._set_status = statuses.append

        def fail_to_start():
            raise RuntimeError("thread start failed")

        worker = SimpleNamespace(start=fail_to_start)

        with patch.object(app_module.threading, "Thread", return_value=worker):
            application._start_assessment_worker(
                ScreenObservation("current", 1280, 720, []),
                loop_step=False,
                run_token=None,
            )

        self.assertEqual(application._background_task_counter, 6)
        self.assertEqual(application._latest_assessment_task_id, 4)
        self.assertEqual(statuses, ["状態評価を開始できません"])

    def test_poll_background_results_stops_loop_for_superseded_loop_assessment(self):
        application = Application.__new__(Application)
        application._background_results = queue.Queue()
        application._latest_assessment_task_id = 2
        application._loop_active = True
        application.controller = SimpleNamespace(is_running=True, rearm_token=4, stop_reason=None)
        application.runtime_log = RecordingLog()
        application.root = SimpleNamespace(after=lambda *_args: None)
        stopped_reasons = []
        application.stop = stopped_reasons.append
        application._background_results.put(("assessment", 1, 4, "assessment", None, None, True, ScreenObservation("old", 1280, 720, [])))

        application._poll_background_results()

        self.assertEqual(stopped_reasons, ["assessment_superseded"])
        self.assertEqual(application.runtime_log.records[0][1], "outcome_result_discarded")

    # {
    #   責務: [test_poll_global_stop_ignores_automated_cursor_move: 自動クリック先への移動を手動停止と誤認しないことを検証する]
    #   処理: [自動移動先を通知した後に停止監視を呼び、停止せず基準位置を更新することを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_poll_global_stop_ignores_automated_cursor_move(self):
        application = self.make_application(live=False)
        application._loop_active = True
        application._last_cursor_position = (100, 200)
        application._automated_cursor_position_lock = threading.Lock()
        application._automated_cursor_position = None
        application._automated_cursor_move_in_progress = False
        application._cursor_position = lambda: (130, 245)
        application.root = SimpleNamespace(after=lambda *_args: None)
        application.runtime_log = RecordingLog()
        stopped = []
        application.stop = lambda reason: stopped.append(reason)

        application._record_automated_cursor_position((130, 245), False)
        application._poll_global_stop()

        self.assertEqual(stopped, [])
        self.assertEqual(application._last_cursor_position, (130, 245))
        self.assertIsNone(application._automated_cursor_position)

    def test_poll_global_stop_keeps_automated_move_pending_until_api_completes(self):
        application = self.make_application(live=False)
        application._loop_active = True
        application._last_cursor_position = (100, 200)
        application._automated_cursor_position_lock = threading.Lock()
        application._automated_cursor_position = None
        application._automated_cursor_move_in_progress = False
        cursor = {"position": (100, 200)}
        application._cursor_position = lambda: cursor["position"]
        application.root = SimpleNamespace(after=lambda *_args: None)
        application.runtime_log = RecordingLog()
        stopped = []
        application.stop = lambda reason: stopped.append(reason)

        application._record_automated_cursor_position((130, 245), True)
        application._poll_global_stop()

        self.assertEqual(stopped, [])
        self.assertEqual(application._automated_cursor_position, (130, 245))
        self.assertTrue(application._automated_cursor_move_in_progress)

        cursor["position"] = (130, 245)
        application._record_automated_cursor_position((130, 245), False)
        application._poll_global_stop()

        self.assertEqual(stopped, [])
        self.assertEqual(application._last_cursor_position, (130, 245))
        self.assertIsNone(application._automated_cursor_position)

    # {
    #   責務: [test_poll_global_stop_stops_for_manual_cursor_move: 自動移動先と異なるカーソル移動で停止することを検証する]
    #   処理: [未通知座標への移動を停止監視へ渡し、理由付き停止を確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_poll_global_stop_stops_for_manual_cursor_move(self):
        application = self.make_application(live=False)
        application._loop_active = True
        application._last_cursor_position = (100, 200)
        application._automated_cursor_position_lock = threading.Lock()
        application._automated_cursor_position = None
        application._automated_cursor_move_in_progress = False
        application._cursor_position = lambda: (140, 250)
        application.root = SimpleNamespace(after=lambda *_args: None)
        application.runtime_log = RecordingLog()
        stopped = []
        application.stop = lambda reason: stopped.append(reason)

        application._poll_global_stop()

        self.assertEqual(stopped, ["手動マウス移動"])
        self.assertEqual(application.runtime_log.records, [("run_control", "stopped_by_manual_mouse_move")])

    # {
    #   責務: [test_run_and_execute_validates_before_persisting_or_constructing_pipeline: 実行前検証が設定保存より先に行われることを検証する]
    #   処理: [対象未選択時に保存とpipeline構築を行わずエラーを記録する]
    #   引数: []
    #   戻り値: []
    # }
    def test_run_and_execute_validates_before_persisting_or_constructing_pipeline(self):
        application = self.make_application()
        application.controller = RunController()
        application.config_store = SimpleNamespace(save=lambda _config: self.fail("config saved before validation"))
        application.runtime_log = RecordingLog()

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application.run_and_execute()

        show_error.assert_called_once()
        self.assertIn("対象ウィンドウの選択", show_error.call_args.args[1])
        self.assertEqual(application.runtime_log.records[0][0], "error")


if __name__ == "__main__":
    unittest.main()
