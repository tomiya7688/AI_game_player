import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player import app as app_module
from ai_game_player.app import Application


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

    def test_recycled_window_handle_with_different_process_is_rejected(self):
        # {
        #   責務: [test_recycled_window_handle_with_different_process_is_rejected: HWND再利用時に別プロセスへの入力を拒否する]
        #   処理: [列挙時PIDと現在PIDが異なる対象を検証する]
        #   引数: []
        #   戻り値: []
        # }
        application = self.make_application(handles={"Game": 123})
        user32 = FakeUser32(valid_handles=[123], process_id=999)

        with patch.object(app_module.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(ValueError, "別の対象"):
                application._validate_live_execution()

        self.assertEqual(user32.checked_handles, [123])

    def test_start_loop_refuses_invalid_target_before_starting_controller(self):
        # {
        #   責務: [test_start_loop_refuses_invalid_target_before_starting_controller: 不正対象では連続実行を開始しないことを確認する]
        #   処理: [対象未選択の開始要求がcontrollerを起動しないことを検証する]
        #   引数: []
        #   戻り値: []
        # }
        application = self.make_application()
        application.runtime_log = RecordingLog()
        application.controller = SimpleNamespace(start=lambda: self.fail("controller started"))
        application._set_status = lambda _message: None

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application.start_loop()

        show_error.assert_called_once()

    def test_loop_stops_when_step_fails(self):
        # {
        #   責務: [test_loop_stops_when_step_fails: 1ステップ失敗時に連続実行を停止することを確認する]
        #   処理: [失敗結果を返すステップ後にstopが呼ばれることを検証する]
        #   引数: []
        #   戻り値: []
        # }
        application = self.make_application(live=False)
        application.controller = SimpleNamespace(is_running=True)
        application.loop_job = None
        application.runtime_log = RecordingLog()
        application.capture_screen = lambda: None
        application.run_and_execute = lambda: False
        stopped = []
        application.stop = lambda: stopped.append(True)
        application._loop_step()

        self.assertEqual(stopped, [True])

    def test_run_and_execute_validates_before_persisting_or_constructing_pipeline(self):
        # {
        #   責務: [test_run_and_execute_validates_before_persisting_or_constructing_pipeline: 実行前検証が設定保存より先に行われることを検証する]
        #   処理: [対象未選択時に保存とpipeline構築を行わずエラーを記録する]
        #   引数: []
        #   戻り値: []
        # }
        application = self.make_application()
        application.config_store = SimpleNamespace(save=lambda _config: self.fail("config saved before validation"))
        application.runtime_log = RecordingLog()

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application.run_and_execute()

        show_error.assert_called_once()
        self.assertIn("対象ウィンドウの選択", show_error.call_args.args[1])
        self.assertEqual(application.runtime_log.records[0][0], "error")


if __name__ == "__main__":
    unittest.main()
