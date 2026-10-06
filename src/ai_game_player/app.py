import os
import ctypes
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.execution_mode import execution_labels
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.outcome import OutcomeAssessment, OutcomeEvaluator
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.window_selector import WindowsWindowSelector
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.ui.shell import ApplicationShell, ShellState, ShellStateStore


# {
#   責務: [Application scheduling constants: Tkイベントポーリングと連続実行間隔を明示する]
#   フィールド: [background_result_poll_interval_ms: worker結果確認間隔, global_stop_poll_interval_ms: 緊急停止確認間隔, loop_step_delay_ms: 連続実行step間隔, windows_f12_virtual_key_code: F12キー識別値, windows_key_pressed_flag: キー状態フラグ]
# }
BACKGROUND_RESULT_POLL_INTERVAL_MS = 50
GLOBAL_STOP_POLL_INTERVAL_MS = 100
LOOP_STEP_DELAY_MS = 1000
WINDOWS_F12_VIRTUAL_KEY_CODE = 0x7B
WINDOWS_KEY_PRESSED_FLAG = 1


class MemorySource:
    def __init__(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> None:
        self.observation = observation
        self.candidates = candidates

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        return self.observation, self.candidates


# {
#   責務: [
#     Application: UI入力を受け取り、ゲーム実行状態と対象ウィンドウを管理する
#   ]
#   フィールド: [
#     window_handles: 表示名ごとのHWND
#     window_process_ids: 表示名ごとの列挙時PID
#     live_execution: 実入力の許可状態
#   ]
#   処理: [
#     1: UI commandの入力を検証する
#     2: 対象識別情報を実行パイプラインへ渡す
#     3: 実行結果と状態を画面へ表示する
#   ]
# }
class Application:
    # {
    #   責務: [
    #     __init__: UIと実行制御の初期状態を構築する
    #   ]
    #   処理: [
    #     1: 設定と実行制御を初期化する
    #     2: 対象ウィンドウの識別情報を保持する
    #     3: UI commandと表示を接続する
    #   ]
    #   引数: [
    #     root: Tkinterのルートウィンドウ
    #   ]
    #   戻り値: []
    # }
    def __init__(self, root: tk.Tk) -> None:
        config_store = ConfigStore(Path("data/config.json"))
        config = config_store.load()
        self.root = root
        self.runtime_log = RuntimeLog()
        self.controller = RunController()
        self.windows: list = []
        self.window_handles: dict[str, int] = {}
        self.window_process_ids: dict[str, int] = {}
        self.loop_job: str | None = None
        self._loop_active = False
        self._execution_in_progress = False
        self._background_task_counter = 0
        self._execution_task_id: int | None = None
        self._background_results: queue.Queue[tuple[str, int, int | None, str, object | None, Exception | None, bool, ScreenObservation | None]] = queue.Queue()
        self._last_cursor_position: tuple[int, int] | None = None
        self.outcome_evaluator = OutcomeEvaluator()
        self.previous_observation: ScreenObservation | None = None
        self.current_assessment = None
        self.loop_guard = LoopGuard()
        root.title("AI Game Player - Decision Sandbox")
        root.geometry("1120x760")
        root.minsize(800, 600)
        root.bind("<Escape>", lambda _event: self.stop())
        self.shell_state_store = ShellStateStore(Path("data/shell_state.json"))
        self.shell = ApplicationShell(root, self.shell_state_store.load(), self.stop, self._save_shell_state)
        self.shell.pack(fill=tk.BOTH, expand=True)
        frame = ttk.Frame(self.shell.play_host, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        self.provider = tk.StringVar(value=config.provider)
        self.model = tk.StringVar(value=config.model)
        self.endpoint = tk.StringVar(value=config.endpoint)
        self.personality = tk.StringVar(value=config.personality)
        self.purpose = tk.StringVar(value=config.purpose)
        self.live_execution = tk.BooleanVar(value=config.live_execution)
        self.input_mode = tk.StringVar(value=config.input_mode)
        self.config_store = config_store
        settings = ttk.Frame(frame)
        settings.pack(fill=tk.X)
        ttk.Label(settings, text="Provider").pack(side=tk.LEFT)
        provider_combo = ttk.Combobox(settings, textvariable=self.provider, values=("ローカル規則", "Ollama"), state="readonly", width=12)
        provider_combo.pack(side=tk.LEFT, padx=5)
        provider_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_models() if self.provider.get() == "Ollama" else None)
        ttk.Label(settings, text="モデル").pack(side=tk.LEFT)
        self.model_combo = ttk.Combobox(settings, textvariable=self.model, width=16)
        self.model_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(settings, text="モデル取得", command=self.refresh_models).pack(side=tk.LEFT)
        ttk.Label(settings, text="Endpoint").pack(side=tk.LEFT)
        ttk.Entry(settings, textvariable=self.endpoint, width=28).pack(side=tk.LEFT, padx=5)
        ttk.Button(settings, text="接続確認", command=self.check_ollama).pack(side=tk.LEFT)
        prompt_settings = ttk.Frame(frame)
        prompt_settings.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(prompt_settings, text="人格").pack(side=tk.LEFT)
        ttk.Entry(prompt_settings, textvariable=self.personality, width=20).pack(side=tk.LEFT, padx=5)
        ttk.Label(prompt_settings, text="目的").pack(side=tk.LEFT)
        ttk.Entry(prompt_settings, textvariable=self.purpose, width=34).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(prompt_settings, text="実入力を許可", variable=self.live_execution, command=self._refresh_execution_controls).pack(side=tk.LEFT, padx=5)
        ttk.Label(prompt_settings, text="入力方式").pack(side=tk.LEFT)
        ttk.Combobox(prompt_settings, textvariable=self.input_mode, values=("window_message", "mouse"), state="readonly", width=16).pack(side=tk.LEFT, padx=5)
        window_settings = ttk.Frame(frame)
        window_settings.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(window_settings, text="対象ウィンドウ").pack(side=tk.LEFT)
        self.window_choice = tk.StringVar()
        self.window_combo = ttk.Combobox(window_settings, textvariable=self.window_choice, state="readonly", width=42)
        self.window_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(window_settings, text="一覧更新", command=self.refresh_windows).pack(side=tk.LEFT)
        ttk.Label(frame, text="画面観測JSON").pack(anchor=tk.W)
        ttk.Button(frame, text="画面取得（Windows）", command=self.capture_screen).pack(anchor=tk.W)
        self.obs = tk.Text(frame, height=10)
        self.obs.pack(fill=tk.BOTH, expand=True)
        self.obs.insert("1.0", json.dumps({"screen_id": "title", "width": 1280, "height": 720, "ocr_text": ["NEW GAME", "OPTION"]}, ensure_ascii=False, indent=2))
        ttk.Label(frame, text="Automation候補JSON").pack(anchor=tk.W, pady=(8, 0))
        self.actions = tk.Text(frame, height=10)
        self.actions.pack(fill=tk.BOTH, expand=True)
        self.actions.insert("1.0", json.dumps([{"action_id": "new-game", "kind": "click", "label": "NEW GAME", "x": 640, "y": 360, "confidence": .95}, {"action_id": "option", "kind": "click", "label": "OPTION", "x": 640, "y": 500, "confidence": .8}], ensure_ascii=False, indent=2))
        controls = ttk.Frame(frame)
        controls.pack(anchor=tk.W, pady=8)
        ttk.Label(frame, text="停止方法: 停止ボタン / Esc / F12 / 手動マウス移動").pack(anchor=tk.W)
        ttk.Button(controls, text="1ステップ判断（操作は実行しない）", command=self.run).pack(side=tk.LEFT)
        self.execute_button = ttk.Button(controls, command=self.run_and_execute)
        self.execute_button.pack(side=tk.LEFT, padx=6)
        self.loop_button = ttk.Button(controls, command=self.start_loop)
        self.loop_button.pack(side=tk.LEFT)
        ttk.Button(controls, text="再開", command=self.start).pack(side=tk.LEFT)
        self.live_status = ttk.Label(frame)
        self.live_status.pack(anchor=tk.W)
        self._refresh_execution_controls()
        self.result = ttk.Label(frame, text="待機中")
        self.result.pack(anchor=tk.W)
        self.metrics = ttk.Label(frame, text="指標: 0件")
        self.metrics.pack(anchor=tk.W)
        self.outcome = ttk.Label(frame, text="状態: 未評価")
        self.outcome.pack(anchor=tk.W)
        ttk.Label(frame, text="評価結果JSON").pack(anchor=tk.W)
        self.evaluation = tk.Text(frame, height=5)
        self.evaluation.pack(fill=tk.X)
        if config.provider == "Ollama":
            self.root.after(0, self.refresh_models)
        self.root.after(BACKGROUND_RESULT_POLL_INTERVAL_MS, self._poll_background_results)
        if os.name == "nt":
            self.refresh_windows()
            self._poll_global_stop()

    def _save_shell_state(self, state: ShellState) -> None:
        try:
            self.shell_state_store.save(state)
        except OSError as exc:
            self.runtime_log.write("error", str(exc), {"operation": "save_shell_state"})

    def _set_status(self, message: str) -> None:
        self.result.config(text=message)
        self.shell.set_status(message)

    def _refresh_execution_controls(self) -> None:
        execute_label, loop_label, status = execution_labels(self.live_execution.get())
        self.execute_button.config(text=execute_label)
        self.loop_button.config(text=loop_label)
        self.live_status.config(text=status)

    # {
    #   責務: [
    #     _validate_live_execution: 実入力に使う選択対象のHWNDとPIDを再検証する
    #   ]
    #   処理: [
    #     1: dry-runなら検証を終える
    #     2: 選択されたHWNDと列挙時PIDを取得する
    #     3: Windows上で現在のHWND所有PIDと保存済みPIDを比較する
    #   ]
    #   引数: []
    #   戻り値: []
    #   エラー: [
    #     対象未選択またはHWND/PID不一致でValueError
    #   ]
    # }
    def _validate_live_execution(self) -> None:
        if not self.live_execution.get():
            return

        selected_window = self.window_choice.get()
        target_handle = self.window_handles.get(selected_window)
        expected_process_id = self.window_process_ids.get(selected_window)
        if target_handle is None or expected_process_id is None:
            raise ValueError("実入力には対象ウィンドウの選択が必要です")
        if os.name == "nt":
            user32 = ctypes.windll.user32
            process_id = ctypes.c_ulong()
            if (
                not user32.IsWindow(target_handle)
                or not user32.GetWindowThreadProcessId(target_handle, ctypes.byref(process_id))
                or process_id.value != expected_process_id
            ):
                raise ValueError("選択中の対象ウィンドウが別の対象へ変化または失効しています。一覧を更新してください")

    def _cursor_position(self) -> tuple[int, int] | None:
        if os.name != "nt":
            return None
        point = (ctypes.c_long * 2)()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point[0]), int(point[1])

    # {
    #   責務: [_poll_global_stop: UIを塞がず緊急キーと手動マウス移動による停止を検出する]
    #   処理: [F12または連続実行中のカーソル移動を検知して理由付き停止を要求する]
    #   引数: []
    #   戻り値: []
    # }
    def _poll_global_stop(self) -> None:
        if os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(WINDOWS_F12_VIRTUAL_KEY_CODE) & WINDOWS_KEY_PRESSED_FLAG:
            self.stop("F12")
        current = self._cursor_position()
        if self._loop_active and self._last_cursor_position is not None and current != self._last_cursor_position:
            self.runtime_log.write("run_control", "stopped_by_manual_mouse_move")
            self.stop("手動マウス移動")
        self.root.after(GLOBAL_STOP_POLL_INTERVAL_MS, self._poll_global_stop)

    def check_ollama(self) -> None:
        try:
            models = OllamaProvider.list_models(self.endpoint.get())
            self.model_combo["values"] = models
            self._set_status(f"Ollama接続OK: {len(models)}モデル")
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "ollama_connection"})
            self._set_status("Ollama接続エラー")

    def refresh_models(self) -> None:
        try:
            models = OllamaProvider.list_models(self.endpoint.get())
            self.model_combo["values"] = models
            if models and self.model.get() not in models:
                self.model.set(models[0])
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "model_list"})
            self._set_status("Ollamaモデル一覧を取得できません")

    # {
    #   責務: [
    #     refresh_windows: 操作対象ウィンドウの一覧とPID対応を更新する
    #   ]
    #   処理: [
    #     1: Windowsの対象一覧を取得する
    #     2: 表示名からHWNDと列挙時PIDへの対応を保存する
    #     3: 選択UIの候補を更新する
    #   ]
    #   引数: []
    #   戻り値: []
    #   副作用: [
    #     window_handles、window_process_ids、選択UIを更新する
    #   ]
    # }
    def refresh_windows(self) -> None:
        try:
            self.windows = WindowsWindowSelector().list_windows()
            self.window_handles = {window.display_name: window.handle for window in self.windows}
            self.window_process_ids = {window.display_name: window.process_id for window in self.windows}
            self.window_combo["values"] = list(self.window_handles)
            if self.window_handles and not self.window_choice.get():
                self.window_choice.set(next(iter(self.window_handles)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "window_list"})
            messagebox.showerror("ウィンドウ一覧エラー", str(exc))

    # {
    #   責務: [_assess_observation: UI状態へ触れず画面状態の評価を計算する]
    #   処理: [指定されたproviderで評価し、失敗時はローカル評価へフォールバックする]
    #   引数: [observation: 評価対象, previous: 直前の観測, provider_name: provider名, model: モデル名, endpoint: Ollama endpoint]
    #   戻り値: [OutcomeAssessment: 評価結果]
    # }
    def _assess_observation(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
        provider_name: str,
        model: str,
        endpoint: str,
    ) -> OutcomeAssessment:
        try:
            if provider_name == "Ollama":
                return OllamaProvider(model, endpoint).assess_outcome(observation, previous)
            else:
                return self.outcome_evaluator.assess(observation)
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "outcome_assessment"})
            return self.outcome_evaluator.assess(observation)

    # {
    #   責務: [capture_screen: Windows画面を観測へ変換し評価をバックグラウンドへ渡す]
    #   処理: [画面とOCRを取得しUIへ表示した後、評価workerを開始する]
    #   引数: [loop_step: 連続実行の評価結果として扱うか]
    #   戻り値: [ScreenObservation | None: 取得した観測または取得失敗]
    # }
    def capture_screen(self, *, loop_step: bool = False) -> ScreenObservation | None:
        try:
            from ai_game_player.frame_analyzer import FrameAnalyzer
            selected_handle = self.window_handles.get(self.window_choice.get())
            observation = FrameAnalyzer(TesseractOcrRecognizer.optional()).analyze(WindowsScreenCapture().capture(selected_handle), "live")
            self.obs.delete("1.0", tk.END)
            self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            self.runtime_log.write("screen_capture", "observation updated", {"screen_id": observation.screen_id})
            run_token = self.controller.rearm_token
            self._start_assessment_worker(observation, loop_step=loop_step, run_token=run_token)
            return observation
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "screen_capture"})
            messagebox.showerror("画面取得エラー", str(exc))
            return None

    # {
    #   責務: [_start_assessment_worker: Ollamaを含む画面状態評価をUIスレッド外で開始する]
    #   処理: [UI設定を値として退避し、結果をスレッドセーフなqueueへ送る]
    #   引数: [observation: 評価対象, loop_step: 連続実行の継続判定か, run_token: 呼び出し時の実行世代]
    #   戻り値: []
    # }
    def _start_assessment_worker(
        self,
        observation: ScreenObservation,
        *,
        loop_step: bool,
        run_token: int | None,
    ) -> None:
        self._background_task_counter += 1
        task_id = self._background_task_counter
        worker = threading.Thread(
            target=self._assess_observation_in_background,
            args=(
                task_id,
                run_token,
                loop_step,
                observation,
                self.previous_observation,
                self.provider.get(),
                self.model.get(),
                self.endpoint.get(),
            ),
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_outcome_assessment_worker"})
            if loop_step:
                self.stop("outcome_worker_start_failed")
            else:
                self._set_status("状態評価を開始できません")

    # {
    #   責務: [_assess_observation_in_background: 評価を実行してGUIへ処理結果を引き渡す]
    #   処理: [例外を捕捉し、Tk操作をせず結果queueへ登録する]
    #   引数: [task_id: worker識別子, run_token: 実行世代, loop_step: 連続実行由来, observation: 評価対象]
    #   戻り値: []
    # }
    def _assess_observation_in_background(
        self,
        task_id: int,
        run_token: int | None,
        loop_step: bool,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
        provider_name: str,
        model: str,
        endpoint: str,
    ) -> None:
        assessment: OutcomeAssessment | None = None
        error: Exception | None = None
        try:
            assessment = self._assess_observation(observation, previous, provider_name, model, endpoint)
        except Exception as exc:
            error = exc
        self._background_results.put(("assessment", task_id, run_token, "assessment", assessment, error, loop_step, observation))

    # {
    #   責務: [_start_pipeline_worker: 候補判断・安全検証・実行をUIスレッド外で開始する]
    #   処理: [重複実行を防止し、開始時の実行世代をworkerへ固定する]
    #   引数: [pipeline: 実行パイプライン, operation: 判断または実行, purpose: ゲームの目的, personality: 判断人格, run_token: 実行開始世代, loop_step: 連続実行由来]
    #   戻り値: [bool: workerを開始したか]
    # }
    def _start_pipeline_worker(
        self,
        pipeline: DecisionPipeline,
        operation: str,
        purpose: str,
        personality: str,
        run_token: int,
        loop_step: bool,
    ) -> bool:
        if self._execution_in_progress:
            pipeline.close()
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return False

        self._background_task_counter += 1
        task_id = self._background_task_counter
        self._execution_task_id = task_id
        self._execution_in_progress = True
        worker = threading.Thread(
            target=self._run_pipeline_in_background,
            args=(task_id, pipeline, operation, purpose, personality, run_token, loop_step),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self._execution_in_progress = False
            self._execution_task_id = None
            pipeline.close()
            raise
        self._set_status("Ollama推論中" if self.provider.get() == "Ollama" else "判断・実行中")
        return True

    # {
    #   責務: [_run_pipeline_in_background: pipelineを実行し結果・例外・終了処理をqueueへ送る]
    #   処理: [Tkを操作せず判断または実行を行い、pipelineを必ずcloseする]
    #   引数: [task_id: worker識別子, pipeline: 対象pipeline, operation: 判断または実行, run_token: 実行世代]
    #   戻り値: []
    # }
    def _run_pipeline_in_background(
        self,
        task_id: int,
        pipeline: DecisionPipeline,
        operation: str,
        purpose: str,
        personality: str,
        run_token: int,
        loop_step: bool,
    ) -> None:
        result: object | None = None
        error: Exception | None = None
        try:
            if operation == "execute":
                result = pipeline.run_and_execute(
                    purpose=purpose,
                    personality=personality,
                    expected_rearm_token=run_token,
                )
            else:
                result = pipeline.run(
                    purpose=purpose,
                    personality=personality,
                    expected_rearm_token=run_token,
                )
        except Exception as exc:
            error = exc
        finally:
            try:
                pipeline.close()
            except Exception as exc:
                if error is None:
                    error = exc
        self._background_results.put(("pipeline", task_id, run_token, operation, result, error, loop_step, None))

    # {
    #   責務: [_run_is_current: worker結果が現在も有効な実行世代か判定する]
    #   処理: [実行中状態と再開世代の一致を確認する]
    #   引数: [run_token: worker開始時の世代]
    #   戻り値: [bool: 結果の有効性]
    # }
    def _run_is_current(self, run_token: int | None) -> bool:
        return run_token is not None and self.controller.is_running and self.controller.rearm_token == run_token

    # {
    #   責務: [_poll_background_results: worker結果をGUIスレッドで適用する]
    #   処理: [停止済みworkerの結果を破棄し、有効な評価・判断・実行結果だけを表示する]
    #   引数: []
    #   戻り値: []
    # }
    def _poll_background_results(self) -> None:
        while True:
            try:
                task_type, task_id, run_token, operation, result, error, loop_step, observation = self._background_results.get_nowait()
            except queue.Empty:
                break

            if task_type == "assessment":
                if run_token is not None and not self._run_is_current(run_token):
                    self.runtime_log.write("run_control", "outcome_result_discarded", {"reason": self.controller.stop_reason or "execution generation changed"})
                    continue
                if error is not None:
                    self.runtime_log.write("error", str(error), {"operation": "outcome_assessment"})
                    if loop_step and self._loop_active:
                        self.stop("outcome_assessment_failed")
                    continue
                if not isinstance(result, OutcomeAssessment) or observation is None:
                    self.runtime_log.write("error", "状態評価workerの結果形式が不正です", {"operation": "outcome_assessment"})
                    if loop_step and self._loop_active:
                        self.stop("invalid_outcome_assessment_result")
                    continue
                self.previous_observation = observation
                self.current_assessment = result
                self.outcome.config(text=f"状態: {result.status} ({result.confidence:.0%})")
                self.runtime_log.write("outcome", result.reason, {"status": result.status, "confidence": result.confidence, "provider": self.provider.get()})
                if loop_step:
                    self._continue_loop_with_assessment(observation, result, run_token)
                continue

            if task_type != "pipeline" or task_id != self._execution_task_id:
                continue
            self._execution_in_progress = False
            self._execution_task_id = None
            if error is not None:
                if isinstance(error, ExecutionCancelled) or not self._run_is_current(run_token):
                    reason = error.reason if isinstance(error, ExecutionCancelled) else self.controller.stop_reason or "実行世代が変更されました"
                    self.runtime_log.write("run_control", "inference_result_discarded", {"reason": reason, "operation": operation})
                    if self.controller.is_running:
                        self._set_status("停止要求後の推論応答を破棄しました")
                    if loop_step and self._loop_active and self._run_is_current(run_token):
                        self.stop(reason)
                    continue
                self.runtime_log.write("error", str(error), {"operation": operation})
                messagebox.showerror("実行エラー" if operation == "execute" else "判断エラー", str(error))
                if loop_step and self._loop_active:
                    self.stop("pipeline_step_failed")
                continue
            if not self._run_is_current(run_token):
                self.runtime_log.write("run_control", "inference_result_discarded", {"reason": self.controller.stop_reason or "実行世代が変更されました", "operation": operation})
                continue

            if operation == "execute" and isinstance(result, ExecutionResult):
                self._set_status(f"実行: {result.action_id} / {result.mode} / {result.detail}")
                metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
                self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
                self.runtime_log.write("execution", result.detail, {"action_id": result.action_id, "mode": result.mode})
            elif operation == "decision" and isinstance(result, ActionDecision):
                self._set_status(f"選択: {result.action_id} / {result.reason}")
                metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
                self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
                self.runtime_log.write("decision", result.reason, {"action_id": result.action_id, "provider": self.provider.get()})

            if loop_step and self._loop_active and self._run_is_current(run_token):
                self._last_cursor_position = self._cursor_position()
                self.loop_job = self.root.after(LOOP_STEP_DELAY_MS, self._loop_step)
        self.root.after(BACKGROUND_RESULT_POLL_INTERVAL_MS, self._poll_background_results)

    # {
    #   責務: [
    #     start_loop: 対象を検証してから連続実行を開始する
    #   ]
    #   処理: [
    #     1: 実入力対象のHWNDとPIDを検証する
    #     2: 不正なら記録と通知を行い開始を拒否する
    #     3: 正常ならcontrollerを開始し次stepを予約する
    #   ]
    #   引数: []
    #   戻り値: []
    # }
    def start_loop(self) -> None:
        if self._loop_active:
            return
        if self._execution_in_progress:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        try:
            self._validate_live_execution()
        except ValueError as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            messagebox.showerror("連続実行エラー", str(exc))
            self._set_status("連続実行を開始できません")
            return

        self.controller.start()
        if self.loop_job is None:
            self._loop_active = True
            self._last_cursor_position = self._cursor_position()
            self.loop_guard.reset()
            self.runtime_log.write("run_control", "loop_started")
            self.loop_job = self.root.after(LOOP_STEP_DELAY_MS, self._loop_step)
            self._set_status("連続実行中")

    # {
    #   責務: [_loop_step: 画面観測を用意し、非同期状態評価を開始する]
    #   処理: [開始世代を記録して観測し、結果処理を評価workerへ委譲する]
    #   引数: []
    #   戻り値: []
    # }
    def _loop_step(self) -> None:
        self.loop_job = None
        if not self._loop_active or not self.controller.is_running:
            return
        if self._execution_in_progress:
            self.runtime_log.write("run_control", "loop_stopped_by_overlapping_step")
            self.stop("overlapping_step")
            return

        run_token = self.controller.rearm_token
        if os.name == "nt":
            if self.capture_screen(loop_step=True) is None:
                self.runtime_log.write("run_control", "loop_stopped_by_capture_failure")
                self.stop("capture_failure")
            return
        try:
            current_observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "loop_observation"})
            self.stop("invalid_observation")
            return
        self._start_assessment_worker(current_observation, loop_step=True, run_token=run_token)

    # {
    #   責務: [_continue_loop_with_assessment: 評価済み状態を検査して次の非同期実行を開始する]
    #   処理: [世代・反復・成功失敗を確認し、安全な場合だけ1stepをworkerへ渡す]
    #   引数: [observation: 現在観測, assessment: 状態評価, run_token: 連続実行の開始世代]
    #   戻り値: []
    # }
    def _continue_loop_with_assessment(
        self,
        observation: ScreenObservation,
        assessment: OutcomeAssessment,
        run_token: int | None,
    ) -> None:
        if not self._loop_active or not self._run_is_current(run_token):
            return
        if self.loop_guard.observe(observation):
            self.runtime_log.write("run_control", "loop_stopped_by_repeat")
            self.stop("repeated_observation")
            return
        if assessment.status in {"success", "failure"}:
            self.runtime_log.write("run_control", "loop_stopped_by_outcome", {"status": assessment.status})
            self.stop(f"terminal_outcome:{assessment.status}")
            return
        if not self.run_and_execute(loop_step=True, expected_rearm_token=run_token):
            self.stop("pipeline_step_not_started")

    # {
    #   責務: [start: 実行世代を進めて停止状態から再開する]
    #   処理: [RunControllerを再開し状態とログを更新する]
    #   引数: []
    #   戻り値: []
    # }
    def start(self) -> None:
        self.controller.start()
        self.runtime_log.write("run_control", "started")
        self._set_status("実行可能")

    # {
    #   責務: [stop: 停止要求を即時記録しloop予約と実入力許可を止める]
    #   処理: [controllerを停止状態にし、予約済みloopを取り消して理由を表示する]
    #   引数: [reason: 停止を要求した要因]
    #   戻り値: []
    # }
    def stop(self, reason: str = "ユーザー停止") -> None:
        self.controller.stop(reason)
        self._loop_active = False
        if self.loop_job is not None:
            try:
                self.root.after_cancel(self.loop_job)
            except tk.TclError as exc:
                self.runtime_log.write("error", str(exc), {"operation": "cancel_loop_callback"})
            self.loop_job = None
        self.runtime_log.write("run_control", "stopped", {"reason": reason})
        status = f"停止要求を受け付けました（{reason}）。推論結果は破棄します" if self._execution_in_progress else f"停止中（{reason}）"
        self._set_status(status)

    # {
    #   責務: [run_and_execute: 候補を準備し判断・安全検証・実行をworkerへ委譲する]
    #   処理: [UI上の入力を検証して値を退避し、開始世代付きでpipelineを非同期起動する]
    #   引数: [loop_step: 連続実行step由来, expected_rearm_token: 呼び出し元が保持する実行世代]
    #   戻り値: [bool: workerを開始したか]
    #   エラー: [入力・設定・pipeline構築に失敗した場合は記録しFalse]
    # }
    # {
    #   責務: [run_and_execute: 候補を準備し判断・安全検証・実行をworkerへ委譲する]
    #   処理: [UI上の入力を検証して値を退避し、開始世代付きでpipelineを非同期起動する]
    #   引数: [loop_step: 連続実行step由来, expected_rearm_token: 呼び出し元が保持する実行世代]
    #   戻り値: [bool: workerを開始したか]
    #   エラー: [入力・設定・pipeline構築に失敗した場合は記録しFalse]
    # }
    def run_and_execute(self, *, loop_step: bool = False, expected_rearm_token: int | None = None) -> bool:
        if self._loop_active and not loop_step:
            self._set_status("連続実行中は単独実行を開始できません")
            return False
        if self._execution_in_progress:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return False
        pipeline: DecisionPipeline | None = None
        try:
            run_token = self.controller.rearm_token if expected_rearm_token is None else expected_rearm_token
            self.controller.ensure_running(run_token)
            self._validate_live_execution()
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            evaluation = ActionEvaluator().explain(observation, candidates)
            self.evaluation.delete("1.0", tk.END)
            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=not self.live_execution.get(), window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get(), window_process_id=self.window_process_ids.get(self.window_choice.get()))
            started = self._start_pipeline_worker(pipeline, "execute", self.purpose.get(), self.personality.get(), run_token, loop_step)
            pipeline = None
            return started
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "execution"})
            messagebox.showerror("実行エラー", str(exc))
            return False
        finally:
            if pipeline is not None:
                pipeline.close()

    # {
    #   責務: [run: 実入力を許可せず候補判断をworkerへ委譲する]
    #   処理: [画面入力と設定をUI threadで読み、実行世代付きpipelineを非同期起動する]
    #   引数: []
    #   戻り値: []
    # }
    def run(self) -> None:
        if self._loop_active:
            self._set_status("連続実行中は単独判断を開始できません")
            return
        if self._execution_in_progress:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        pipeline: DecisionPipeline | None = None
        try:
            run_token = self.controller.rearm_token
            self.controller.ensure_running(run_token)
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=True, window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get(), window_process_id=self.window_process_ids.get(self.window_choice.get()))
            self._start_pipeline_worker(pipeline, "decision", self.purpose.get(), self.personality.get(), run_token, False)
            pipeline = None
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "decision"})
            messagebox.showerror("判断エラー", str(exc))
        finally:
            if pipeline is not None:
                pipeline.close()


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
