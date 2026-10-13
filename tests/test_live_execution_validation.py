import ctypes
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player import app as app_module
from ai_game_player.app import Application
from ai_game_player.capture_region import CaptureRegion, CaptureRegionMode
from ai_game_player.game_session import SessionStatus
from ai_game_player.models import ScreenObservation
from ai_game_player.observation_service import LiveObservationService
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.run_control import RunController
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.window_selector import WindowInfo


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
#   責務: [FakeSessionController: ApplicationからSession controllerへのcommand委譲を記録する]
#   フィールド: [run_control: worker世代のtest制御, status: session状態, is_looping: 連続実行状態, command_calls: sessionへ届いた操作]
# }
class FakeSessionController:
    # {
    #   責務: [__init__: 初期状態のSession controller spyを作成する]
    #   処理: [idle状態と空のcommand記録領域を初期化する]
    #   引数: []
    #   戻り値: []
    # }
    def __init__(self):
        self.run_control = RunController()
        self.status = SessionStatus.IDLE
        self.is_looping = False
        self.has_pending_step = False
        self.start_count = 0
        self.stop_reasons = []
        self.loop_commands = []
        self.step_commands = []
        self.completed_steps = []
        self.completed_observations = []

    def start(self):
        self.start_count += 1
        self.run_control.start()
        self.status = SessionStatus.RUNNING

    def stop(self, reason="stopped"):
        self.stop_reasons.append(reason)
        self.is_looping = False
        self.run_control.stop(reason)
        self.status = SessionStatus.STOPPED

    def start_loop(self, command="execute", interval_ms=1000):
        self.loop_commands.append((command, interval_ms))
        self.start()
        self.is_looping = True

    def step(self, command="execute"):
        self.step_commands.append(command)
        if self.status is not SessionStatus.RUNNING:
            self.start()

    def complete_step(self, result=None, error=None):
        self.completed_steps.append((result, error))

    def complete_loop_observation(self, observation):
        self.completed_observations.append(observation)


# {
#   責務: [LiveExecutionValidationTests: 実入力対象の識別と連続実行の停止条件を検証する]
#   フィールド: [fake Applicationとfake Win32 APIを各testで構成する]
# }
class LiveExecutionValidationTests(unittest.TestCase):
    def make_uninitialized_application(self):
        application = Application.__new__(Application)
        application.session_controller = FakeSessionController()
        application.controller = application.session_controller.run_control
        return application

    # {
    #   責務: [make_application: UI依存なしで検証可能なApplicationを構築する]
    #   処理: [実行フラグ・入力方式・選択対象とHWND/PIDを設定する]
    #   引数: [live: 実入力状態, mode: 入力方式, handles: 表示名とHWNDの対応]
    #   戻り値: [Application: テスト用インスタンス]
    # }
    def make_application(self, *, live=True, mode="window_message", handles=None):
        application = self.make_uninitialized_application()
        application.live_execution = FakeVariable(live)
        application.input_mode = FakeVariable(mode)
        application.window_choice = FakeVariable("Game")
        application.window_handles = handles or {}
        application.window_process_ids = {name: 456 for name in application.window_handles}
        application._execution_in_progress = False
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
    #   責務: [test_pipeline_failure_completes_session_step_when_logging_fails: ログ障害がpipeline stepの完了を妨げないことを検証する]
    #   処理: [RuntimeLog.writeが例外を送出するworker失敗を処理し、step完了・利用者通知・次回poll登録を確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_pipeline_failure_completes_session_step_when_logging_fails(self):
        class FailingLog:
            def write(self, *_record):
                raise RuntimeError("Session Journal is unhealthy")

        application = self.make_uninitialized_application()
        application.controller = RunController()
        application.controller.start()
        run_token = application.controller.rearm_token
        application._execution_in_progress = True
        application._execution_task_id = 7
        application._background_results = queue.Queue()
        error = RuntimeError("pipeline failed")
        application._background_results.put(
            ("pipeline", 7, run_token, "execute", None, error, False, None, None)
        )
        application.runtime_log = FailingLog()
        statuses = []
        scheduled = []
        application._set_status = statuses.append
        application.root = SimpleNamespace(after=lambda *_args: scheduled.append(True))
        application._closing = False
        completions = []
        application._complete_session_step = lambda result=None: completions.append(result)

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application._poll_background_results()

        self.assertEqual([error], completions)
        self.assertEqual([True], scheduled)
        self.assertFalse(application._execution_in_progress)
        self.assertIsNone(application._execution_task_id)
        self.assertTrue(any("記録に失敗しました" in status for status in statuses))
        show_error.assert_called_once_with("実行エラー", "pipeline failed")

    # {
    #   責務: [test_manual_capture_assessment_does_not_use_execution_generation: 手動画面取得の評価を実行停止状態から分離する]
    #   処理: [停止中の手動取得では世代tokenを渡さず、連続実行stepでは現在tokenを渡す]
    #   引数: []
    #   戻り値: []
    # }
    def test_manual_capture_assessment_does_not_use_execution_generation(self):
        observation = ScreenObservation("live", 1, 1, [])

        for loop_step in (False, True):
            with self.subTest(loop_step=loop_step):
                application = self.make_uninitialized_application()
                application.window_handles = {}
                application.window_choice = FakeVariable("Game")
                application.capture_mode = FakeVariable(CaptureRegionMode.FULL_SCREEN.value)
                application._active_capture_region = CaptureRegion(CaptureRegionMode.FULL_SCREEN)
                application.obs = SimpleNamespace(delete=lambda *_args: None, insert=lambda *_args: None)
                application.actions = SimpleNamespace(get=lambda *_args: "[]")
                application.workflow_service = SimpleNamespace(
                    preview_observation=lambda captured, _candidates: SimpleNamespace(
                        evaluation=[]
                    )
                )
                application._display_step_evaluation = lambda _evaluation: None
                application.runtime_log = RecordingLog()
                application.controller = RunController()
                if loop_step:
                    application.controller.start()
                    expected_token = application.controller.rearm_token
                else:
                    application.controller.start()
                    application.controller.stop("manual")
                    expected_token = None
                started_assessments = []
                application._start_assessment_worker = lambda captured_observation, **options: started_assessments.append(
                    (captured_observation, options)
                )
                captured_frames = []
                application.observation_service = LiveObservationService(
                    capture_frame=lambda handle: captured_frames.append(handle)
                    or ScreenFrame(1, 1, bytes(4)),
                    analyzer_factory=lambda _ocr: SimpleNamespace(
                        analyze=lambda _frame, _screen_id: observation
                    ),
                    ocr_factory=lambda: None,
                    capture_interval_seconds=10.0,
                    frame_wait_timeout_seconds=1.0,
                )
                self.addCleanup(application.observation_service.close)
                captured_observation = application.capture_screen(loop_step=loop_step)

                self.assertEqual(captured_observation.screen_id, observation.screen_id)
                self.assertEqual(captured_observation.width, observation.width)
                self.assertEqual(captured_observation.height, observation.height)
                self.assertIn("capture_metadata", captured_observation.features)
                self.assertEqual(captured_frames, [None])
                self.assertEqual(len(started_assessments), 1)
                assessment_observation, assessment_options = started_assessments[0]
                self.assertIs(assessment_observation, captured_observation)
                self.assertEqual(
                    assessment_options,
                    {"loop_step": loop_step, "run_token": expected_token},
                )

    # {
    #   責務: [test_capture_screen_previews_observation_candidates_without_manual_candidates: GUIの画面取得がAutomation候補なしで画面由来候補を評価欄へ渡すことを検証する]
    #   処理: [画面取得後に空のAutomation候補JSONとOCR/画像特徴をworkflowへ渡し、由来付き評価を表示することを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_capture_screen_previews_observation_candidates_without_manual_candidates(self):
        observation = ScreenObservation(
            "captured-title",
            640,
            480,
            features={
                "ocr_candidates": [{"text": "START", "x": 20, "y": 30, "width": 80, "height": 32}],
                "image_candidates": [{"action_id": "bright", "kind": "click", "label": "bright_region", "x": 40, "y": 46, "confidence": 0.0}],
            },
        )
        received_inputs = []
        displayed_evaluations = []
        application = self.make_uninitialized_application()
        application.window_handles = {}
        application.window_choice = FakeVariable("Game")
        application.capture_mode = FakeVariable(CaptureRegionMode.FULL_SCREEN.value)
        application._active_capture_region = CaptureRegion(CaptureRegionMode.FULL_SCREEN)
        application._apply_capture_region = lambda: None
        application.observation_service = SimpleNamespace(
            capture_observation=lambda _handle: observation
        )
        application.obs = SimpleNamespace(delete=lambda *_args: None, insert=lambda *_args: None)
        application.actions = SimpleNamespace(get=lambda *_args: "[]")
        application.workflow_service = SimpleNamespace(
            preview_observation=lambda captured, candidates_json: received_inputs.append(
                (captured, candidates_json)
            )
            or SimpleNamespace(
                evaluation=[
                    {
                        "action_id": "ocr-0",
                        "accepted": True,
                        "sources": ["ocr", "image"],
                    }
                ]
            )
        )
        application._display_step_evaluation = displayed_evaluations.append
        application.runtime_log = RecordingLog()
        application._start_assessment_worker = lambda *_args, **_kwargs: None

        self.assertIs(application.capture_screen(), observation)
        self.assertEqual(received_inputs, [(observation, "[]")])
        self.assertEqual(
            displayed_evaluations,
            [[{"action_id": "ocr-0", "accepted": True, "sources": ["ocr", "image"]}]],
        )

    def test_duplicate_title_selection_keeps_the_selected_window_for_capture_and_input(self):
        first_window = WindowInfo(0x101, "Game", 456)
        second_window = WindowInfo(0x202, "Game", 456)
        application = self.make_uninitialized_application()
        application.runtime_log = RecordingLog()
        application.window_combo = {}
        application.window_choice = FakeVariable(first_window.display_name)

        with (
            patch.object(
                app_module,
                "WindowsWindowSelector",
                return_value=SimpleNamespace(list_windows=lambda: [first_window, second_window]),
            ),
            patch.object(app_module.messagebox, "showerror"),
        ):
            application.refresh_windows()

        self.assertEqual(application.runtime_log.records, [])

        self.assertEqual(
            application.window_combo["values"],
            [first_window.display_name, second_window.display_name],
        )
        self.assertEqual(application.window_handles[first_window.display_name], first_window.handle)
        self.assertEqual(application.window_handles[second_window.display_name], second_window.handle)
        self.assertEqual(application.window_process_ids[second_window.display_name], second_window.process_id)

        application.window_choice.value = second_window.display_name
        captured_handles = []
        observation = ScreenObservation("selected-window", 100, 100, [])
        application._apply_capture_region = lambda: None
        application.observation_service = SimpleNamespace(
            capture_observation=lambda handle: captured_handles.append(handle) or observation
        )
        application.obs = SimpleNamespace(delete=lambda *_args: None, insert=lambda *_args: None)
        application.actions = SimpleNamespace(get=lambda *_args: "[]")
        application.workflow_service = SimpleNamespace(
            preview_observation=lambda _captured, _candidates: SimpleNamespace(evaluation=[])
        )
        application._display_step_evaluation = lambda _evaluation: None
        application.controller = SimpleNamespace(rearm_token=None)
        application._start_assessment_worker = lambda *_args, **_kwargs: None

        self.assertIs(application.capture_screen(), observation)
        self.assertEqual(captured_handles, [second_window.handle])

        runtime_arguments = []
        application.live_execution = FakeVariable(True)
        application._session_dry_run = False
        application._application_settings = lambda: object()
        application.workflow_service = SimpleNamespace(
            create_session_runtime=lambda _settings, **arguments: runtime_arguments.append(arguments)
            or "session-runtime"
        )
        application.controller = object()

        fake_user32 = FakeUser32({first_window.handle, second_window.handle}, process_id=second_window.process_id)
        with patch.object(app_module.ctypes, "windll", SimpleNamespace(user32=fake_user32), create=True):
            runtime = application._create_session_runtime()

        self.assertEqual(runtime, "session-runtime")
        self.assertEqual(runtime_arguments[0]["window_handle"], second_window.handle)
        self.assertEqual(runtime_arguments[0]["window_process_id"], second_window.process_id)

    # {
    #   責務: [test_standalone_assessment_updates_display_after_stop: 停止中に完了した手動評価を画面へ反映する]
    #   処理: [tokenを持たないassessment結果が停止状態でも評価・前回観測を更新することを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_standalone_assessment_updates_display_after_stop(self):
        application = self.make_uninitialized_application()
        previous_observation = ScreenObservation("previous", 1280, 720, [])
        observation = ScreenObservation("captured", 1280, 720, [])
        assessment = OutcomeAssessment("ongoing", 0.6, "ゲームは継続中です")
        displayed_statuses = []
        application._background_results = queue.Queue()
        application._background_results.put(("assessment", 1, None, "assessment", assessment, None, False, observation, None))
        application._latest_assessment_task_id = 1
        application.controller = RunController()
        application.controller.start()
        application.controller.stop("manual")
        application.runtime_log = RecordingLog()
        application.provider = FakeVariable("Rule")
        application.previous_observation = previous_observation
        application.current_assessment = None
        application.outcome = SimpleNamespace(config=lambda **values: displayed_statuses.append(values["text"]))
        application.root = SimpleNamespace(after=lambda *_args: None)

        application._poll_background_results()

        self.assertIs(application.previous_observation, observation)
        self.assertIs(application.current_assessment, assessment)
        self.assertEqual(displayed_statuses, ["状態: ongoing (60%)"])
        self.assertEqual(application.runtime_log.records[-1][0:2], ("outcome", "ゲームは継続中です"))

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
    #   責務: [test_start_loop_delegates_scheduling_to_session_controller: 連続実行のtimer所有権をSession controllerへ委譲する]
    #   処理: [validなdry-run設定でApplicationが独自callbackを登録せずcontroller.start_loopを呼ぶことを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_start_loop_delegates_scheduling_to_session_controller(self):
        application = self.make_application(live=False)
        application.workflow_service = SimpleNamespace(
            prepare_step=lambda *_arguments: SimpleNamespace(evaluation=[], dry_run=True)
        )
        application._application_settings = lambda: SimpleNamespace()
        application.obs = SimpleNamespace(get=lambda *_arguments: "{}")
        application.actions = SimpleNamespace(get=lambda *_arguments: "[]")
        application._display_step_evaluation = lambda _evaluation: None
        application._record_automated_cursor_position = lambda *_arguments: None
        application._cursor_position = lambda: (100, 200)

        application.start_loop()

        self.assertEqual(
            application.session_controller.loop_commands,
            [("execute", app_module.LOOP_STEP_DELAY_MS)],
        )
        self.assertTrue(application.session_controller.is_looping)

    # {
    #   責務: [test_single_step_delegates_to_session_controller: 1回分の判断操作をSession controllerへ委譲する]
    #   処理: [validなdry-run設定でApplication.run_and_executeがSession controller.stepを呼ぶことを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_single_step_delegates_to_session_controller(self):
        application = self.make_application(live=False)
        application.workflow_service = SimpleNamespace(
            prepare_step=lambda *_arguments: SimpleNamespace(evaluation=[], dry_run=True)
        )
        application._application_settings = lambda: SimpleNamespace()
        application.obs = SimpleNamespace(get=lambda *_arguments: "{}")
        application.actions = SimpleNamespace(get=lambda *_arguments: "[]")
        application._display_step_evaluation = lambda _evaluation: None

        self.assertTrue(application.run_and_execute())
        self.assertEqual(application.session_controller.step_commands, ["execute"])

    # {
    #   責務: [test_rearm_restarts_the_session_controller: Applicationの再開commandをSession controllerへ委譲する]
    #   処理: [running Sessionを停止理由rearmedで閉じ、Application.startがSessionを再開することを確認する]
    #   引数: []
    #   戻り値: []
    # }
    def test_rearm_restarts_the_session_controller(self):
        application = self.make_uninitialized_application()
        statuses = []
        application.session_controller.start()
        application.runtime_log = RecordingLog()
        application._set_status = statuses.append

        application.start()

        self.assertEqual(application.session_controller.stop_reasons, ["rearmed"])
        self.assertEqual(application.session_controller.start_count, 2)
        self.assertEqual(statuses, ["実行可能"])

    def test_poll_background_results_discards_superseded_assessment(self):
        application = self.make_uninitialized_application()
        earlier_observation = ScreenObservation("earlier", 1280, 720, [])
        latest_observation = ScreenObservation("latest", 1280, 720, [])
        previous_observation = ScreenObservation("previous", 1280, 720, [])
        earlier_assessment = OutcomeAssessment("failure", 0.9, "older result")
        latest_assessment = OutcomeAssessment("ongoing", 0.4, "latest result")
        displayed_statuses = []
        application._background_results = queue.Queue()
        application._latest_assessment_task_id = 2
        application.controller = SimpleNamespace(is_running=False, rearm_token=0, stop_reason=None)
        application.runtime_log = RecordingLog()
        application.provider = FakeVariable("Rule")
        application.previous_observation = previous_observation
        application.current_assessment = None
        application.outcome = SimpleNamespace(config=lambda **values: displayed_statuses.append(values["text"]))
        application.root = SimpleNamespace(after=lambda *_args: None)
        application._background_results.put(("assessment", 2, None, "assessment", latest_assessment, None, False, latest_observation, None))
        application._background_results.put(("assessment", 1, None, "assessment", earlier_assessment, None, False, earlier_observation, None))

        application._poll_background_results()

        self.assertIs(application.previous_observation, latest_observation)
        self.assertIs(application.current_assessment, latest_assessment)
        self.assertEqual(displayed_statuses, ["状態: ongoing (40%)"])
        self.assertEqual(application.runtime_log.records[-1][1], "outcome_result_discarded")
        self.assertEqual(application.runtime_log.records[-1][2]["task_id"], 1)
        self.assertEqual(application.runtime_log.records[-1][2]["latest_task_id"], 2)

    def test_start_assessment_worker_records_task_after_successful_start(self):
        application = self.make_uninitialized_application()
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
        application = self.make_uninitialized_application()
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
        application = self.make_uninitialized_application()
        application._background_results = queue.Queue()
        application._latest_assessment_task_id = 2
        application.session_controller.is_looping = True
        application.controller = SimpleNamespace(is_running=True, rearm_token=4, stop_reason=None)
        application.runtime_log = RecordingLog()
        application.root = SimpleNamespace(after=lambda *_args: None)
        stopped_reasons = []
        application.stop = stopped_reasons.append
        application._background_results.put(("assessment", 1, 4, "assessment", None, None, True, ScreenObservation("old", 1280, 720, []), None))

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
        application.session_controller.is_looping = True
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
        application.session_controller.is_looping = True
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
        application.session_controller.is_looping = True
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
