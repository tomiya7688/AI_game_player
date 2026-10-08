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
from ai_game_player.outcome import OutcomeAssessment, OutcomeEvaluator
from ai_game_player.game_session import GameSessionController, LoopObservation, SessionRuntime, SessionSnapshot, SessionStatus, SessionStep
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import ExecutionCancelled
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
    # {
    #   責務: [MemorySource: UIが更新した最新の画面観測と候補をsession中のPipelineへ渡す]
    #   フィールド: [observation: 最新の画面観測, candidates: 同じ観測から得た操作候補]
    # }
    def __init__(self) -> None:
        self.observation: ScreenObservation | None = None
        self.candidates: list[ActionCandidate] = []

    # {
    #   責務: [update: 次のstepでPipelineが読む画面観測と候補を保存する]
    #   処理: [観測と候補を同じ取得stepの組として差し替える]
    #   引数: [observation: 操作判断の対象となる画面, candidates: 画面から抽出した候補]
    #   戻り値: []
    # }
    def update(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> None:
        self.observation = observation
        self.candidates = candidates

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        if self.observation is None:
            raise RuntimeError("画面観測をsessionへ登録してからPipelineを実行してください")
        return self.observation, self.candidates


class TkAfterScheduler:
    # {
    #   責務: [TkAfterScheduler: Tkのafter予約をSessionScheduler契約へ変換する]
    #   フィールド: [root: callbackを予約・取消するTkルート]
    # }
    def __init__(self, root: tk.Misc) -> None:
        self.root = root

    # {
    #   責務: [schedule: 指定時間後にsession loop callbackをTk event loopへ登録する]
    #   処理: [Tk afterの予約tokenをsession controllerへ返す]
    #   引数: [delay_ms: callbackまで待つミリ秒, callback: UIスレッドで実行するloop処理]
    #   戻り値: [object: 予約取消に使うTk token]
    # }
    def schedule(self, delay_ms: int, callback) -> object:
        return self.root.after(delay_ms, callback)

    # {
    #   責務: [cancel: 予約済みsession loop callbackを取り消す]
    #   処理: [Tk after_cancelへ予約tokenを渡す]
    #   引数: [token: scheduleが返したTk予約token]
    #   戻り値: []
    # }
    def cancel(self, token: object) -> None:
        self.root.after_cancel(token)


# {
#   責務: [
#     Application: UI入力を受け取り、ゲーム実行状態と対象ウィンドウを管理する
#   ]
#   フィールド: [
#     window_handles: 表示名ごとのHWND
#     window_process_ids: 表示名ごとの列挙時PID
#     live_execution: 実入力の許可状態
#     _latest_assessment_task_id: 結果を適用できる最新の画面評価worker ID
#     _automated_cursor_position: 入力workerが通知した自動移動の予定座標と進行状態
#   ]
#   処理: [
#     1: UI commandの入力を検証する
#     2: 対象識別情報を実行パイプラインへ渡す
#     3: 画面評価結果の新旧を判定する
#     4: 自動カーソル移動を停止監視へ通知する
#     5: 実行結果と状態を画面へ表示する
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
    #     3: worker結果と最新画面評価IDを初期化する
    #     4: 自動カーソル位置のworker/UI間同期を初期化する
    #     5: UI commandと表示を接続する
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
        self.memory_source = MemorySource()
        self._session_dry_run = True
        self._last_session_mode: tuple[SessionStatus, bool] | None = None
        self._closing = False
        self.windows: list = []
        self.window_handles: dict[str, int] = {}
        self.window_process_ids: dict[str, int] = {}
        self._execution_in_progress = False
        self._background_task_counter = 0
        self._execution_task_id: int | None = None
        self._latest_assessment_task_id: int | None = None
        self._background_results: queue.Queue[tuple[str, int, int | None, str, object | None, Exception | None, bool, ScreenObservation | None]] = queue.Queue()
        self._last_cursor_position: tuple[int, int] | None = None
        self._automated_cursor_position_lock = threading.Lock()
        self._automated_cursor_position: tuple[int, int] | None = None
        self._automated_cursor_move_in_progress = False
        self.outcome_evaluator = OutcomeEvaluator()
        self.previous_observation: ScreenObservation | None = None
        self.current_assessment = None
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
        ttk.Checkbutton(prompt_settings, text="実入力を許可", variable=self.live_execution, command=self._on_live_execution_changed).pack(side=tk.LEFT, padx=5)
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
        self.session_controller = GameSessionController(
            runtime_factory=self._create_session_runtime,
            step_handler=self._perform_session_step,
            scheduler=TkAfterScheduler(root),
            loop_observer=self._observe_for_loop,
            on_state_change=self._on_session_state_change,
            on_error=self._on_session_error,
        )
        self.controller = self.session_controller.run_control
        root.protocol("WM_DELETE_WINDOW", self.close)
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
    #   責務: [_is_looping: Session controllerまたは互換テスト状態から連続実行状態を返す]
    #   処理: [通常実行ではSessionの状態を使い、部分初期化されたApplicationでは旧loop flagを読む]
    #   引数: []
    #   戻り値: [bool: 連続実行中ならTrue]
    # }
    def _is_looping(self) -> bool:
        session = getattr(self, "session_controller", None)
        if session is not None:
            return session.is_looping
        return bool(getattr(self, "_loop_active", False))

    # {
    #   責務: [_clear_loop_state: 互換実行状態に残るloop予約を解除する]
    #   処理: [旧callback tokenを取消し、連続実行flagとtokenを初期化する]
    #   引数: []
    #   戻り値: []
    # }
    def _clear_loop_state(self) -> None:
        self._loop_active = False
        token = getattr(self, "loop_job", None)
        if token is not None:
            try:
                self.root.after_cancel(token)
            except tk.TclError as exc:
                self.runtime_log.write("error", str(exc), {"operation": "cancel_loop_callback"})
        self.loop_job = None

    # {
    #   責務: [_loop_step: 互換実行環境で画面観測評価を開始する]
    #   処理: [連続実行状態を確認し、画面観測を評価workerへ渡す]
    #   引数: []
    #   戻り値: []
    # }
    def _loop_step(self) -> None:
        self.loop_job = None
        if not self._is_looping() or not self.controller.is_running:
            return
        run_token = self.controller.rearm_token
        if os.name == "nt":
            if self.capture_screen(loop_step=True) is None:
                self.stop("capture_failure")
            return
        try:
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "loop_observation"})
            self.stop("invalid_observation")
            return
        self._start_assessment_worker(observation, loop_step=True, run_token=run_token)

    # {
    #   責務: [_continue_loop_with_assessment: 互換loopの評価結果から次のstep可否を決める]
    #   処理: [反復・terminal状態を確認し、継続時だけworker stepを開始する]
    #   引数: [observation: 最新画面, assessment: 状態評価, run_token: 評価開始世代]
    #   戻り値: []
    # }
    def _continue_loop_with_assessment(self, observation: ScreenObservation, assessment: OutcomeAssessment, run_token: int | None) -> None:
        if not self._is_looping() or not self._run_is_current(run_token):
            return
        if self.loop_guard.observe(observation) or assessment.status in {"success", "failure"}:
            self.stop("terminal_outcome" if assessment.status in {"success", "failure"} else "repeated_observation")
            return
        if not self.run_and_execute(loop_step=True, expected_rearm_token=run_token):
            self.stop("pipeline_step_not_started")

    # {
    #   責務: [_complete_session_step: Sessionが存在する場合だけworker完了を通知する]
    #   処理: [新しいSession controllerへ完了・失敗を渡し、旧部分初期化テストでは何もしない]
    #   引数: [error: worker失敗時の例外または成功時None]
    #   戻り値: []
    # }
    def _complete_session_step(self, error: Exception | None = None) -> None:
        session = getattr(self, "session_controller", None)
        if session is not None:
            session.complete_step(error=error)

    # {
    #   責務: [_create_session_runtime: セッションで共有するpipelineを一度だけ構築する]
    #   処理: [現在の実行設定と動的MemorySourceを使い、RunController共有のpipelineを返す]
    #   引数: []
    #   戻り値: [DecisionPipeline: セッションの終了まで保持するpipeline]
    # }
    def _create_session_runtime(self) -> DecisionPipeline:
        self._validate_live_execution()
        provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
        return DecisionPipeline(
            self.memory_source,
            Path("data/games/sandbox"),
            provider,
            self.controller,
            dry_run=self._session_dry_run,
            window_handle=self.window_handles.get(self.window_choice.get()),
            input_mode=self.input_mode.get(),
            window_process_id=self.window_process_ids.get(self.window_choice.get()),
            automated_cursor_position_callback=self._record_automated_cursor_position,
        )

    # {
    #   責務: [_perform_session_step: pipeline stepをworkerへ渡してSessionへ完了待ちを返す]
    #   処理: [execute commandは実行、それ以外は判断として非同期起動しdeferred結果を返す]
    #   引数: [runtime: Sessionが所有するDecisionPipeline, command: executeまたはdecision]
    #   戻り値: [SessionStep: worker完了後にSessionがstepを確定するためのdeferred結果]
    # }
    def _perform_session_step(self, runtime: SessionRuntime, command: str) -> SessionStep:
        if self._session_dry_run is False:
            if not self.live_execution.get():
                return SessionStep(terminal_reason="live execution permission revoked")
            try:
                if not hasattr(self, "_validate_live_execution"):
                    raise ValueError("実入力の許可状態を再検証できません")
                self._validate_live_execution()
            except ValueError:
                return SessionStep(terminal_reason="live execution permission revoked")
        if not isinstance(runtime, DecisionPipeline):
            raise TypeError("セッションruntimeはDecisionPipelineである必要があります")
        started = self._start_pipeline_worker(
            runtime,
            "execute" if command == "execute" else "decision",
            self.purpose.get(),
            self.personality.get(),
            self.controller.rearm_token,
            self.session_controller.is_looping,
            close_pipeline_on_exit=False,
        )
        if not started:
            raise RuntimeError("セッションstep workerを開始できません")
        return SessionStep(deferred=True)

    # {
    #   責務: [_observe_for_loop: 連続実行の画面取得と非同期状態評価を開始する]
    #   処理: [画面を取得し、loop世代付き評価workerを開始してSessionへ完了待ちを返す]
    #   引数: []
    #   戻り値: [LoopObservation: 非同期評価のためdeferred状態にした観測]
    # }
    def _observe_for_loop(self) -> LoopObservation:
        try:
            if os.name == "nt":
                from ai_game_player.frame_analyzer import FrameAnalyzer
                selected_handle = self.window_handles.get(self.window_choice.get())
                observation = FrameAnalyzer(TesseractOcrRecognizer.optional()).analyze(
                    WindowsScreenCapture().capture(selected_handle), "live"
                )
                self.obs.delete("1.0", tk.END)
                self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            else:
                observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "loop_observation"})
            return LoopObservation(None, terminal_reason="capture failed")
        self._start_assessment_worker(
            observation,
            loop_step=True,
            run_token=self.controller.rearm_token,
        )
        return LoopObservation(observation, deferred=True)

    # {
    #   責務: [_on_session_state_change: Session状態をGUI表示とログへ反映する]
    #   処理: [状態が変わったときだけ記録し、停止・完了・失敗の結果を表示する]
    #   引数: [snapshot: Sessionの状態とstep数]
    #   戻り値: []
    # }
    def _on_session_state_change(self, snapshot: SessionSnapshot) -> None:
        current = (snapshot.status, snapshot.looping)
        if current != self._last_session_mode:
            self.runtime_log.write("session", snapshot.status.value, {"steps": snapshot.steps, "reason": snapshot.stop_reason})
            self._last_session_mode = current
        if snapshot.status is SessionStatus.FAILED:
            self._set_status(f"セッション失敗: {snapshot.error or snapshot.stop_reason}")
        elif snapshot.status is SessionStatus.COMPLETED:
            self._set_status(f"セッション完了: {snapshot.stop_reason or 'terminal'}")
        elif snapshot.status is SessionStatus.STOPPED and not self._execution_in_progress:
            self._set_status(f"停止中（{snapshot.stop_reason or '停止'}）")
        elif snapshot.looping:
            self._set_status("連続実行中")

    # {
    #   責務: [_on_session_error: Session制御で発生した例外をGUI利用者へ通知する]
    #   処理: [例外の詳細をログへ保存し、操作継続に必要なエラー表示を行う]
    #   引数: [error: Session処理で発生した例外]
    #   戻り値: []
    # }
    def _on_session_error(self, error: Exception) -> None:
        self.runtime_log.write("error", str(error), {"operation": "game_session"})
        if not self._closing:
            messagebox.showerror("セッションエラー", str(error))

    # {
    #   責務: [_on_live_execution_changed: 実入力設定変更時にSessionの実行モードを同期する]
    #   処理: [pending workerがない場合だけSession runtimeを停止し、表示と設定を更新する]
    #   引数: []
    #   戻り値: []
    # }
    def _on_live_execution_changed(self) -> None:
        self._refresh_execution_controls()
        if getattr(self.session_controller, "has_pending_step", False):
            self.live_execution.set(not self.live_execution.get())
            self._refresh_execution_controls()
            self._set_status("実行中は実入力設定を変更できません")
            return
        if not self.live_execution.get() and not self._session_dry_run:
            self._session_dry_run = True
            self.session_controller.stop("live execution permission revoked")

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
    #   責務: [_record_automated_cursor_position: 実入力workerの自動移動予定と状態を停止監視へ通知する]
    #   処理: [予定座標と移動中状態をlockで保護し、Noneなら未処理の自動移動記録を消去する]
    #   引数: [position: SetCursorPosへ渡す画面座標または記録消去用None, move_in_progress: API呼び出し前はTrue、成功・失敗後はFalse]
    #   戻り値: []
    # }
    def _record_automated_cursor_position(
        self,
        position: tuple[int, int] | None,
        move_in_progress: bool,
    ) -> None:
        with self._automated_cursor_position_lock:
            self._automated_cursor_position = position
            self._automated_cursor_move_in_progress = move_in_progress

    # {
    #   責務: [_poll_global_stop: UIを塞がずF12と手動カーソル移動による停止を検出する]
    #   処理: [F12を確認し、自動クリック先以外への連続実行中のカーソル移動で停止を要求する]
    #   引数: []
    #   戻り値: []
    # }
    def _poll_global_stop(self) -> None:
        if os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(WINDOWS_F12_VIRTUAL_KEY_CODE) & WINDOWS_KEY_PRESSED_FLAG:
            self.stop("F12")
        current = self._cursor_position()
        automated_move = False
        if self._is_looping() and current is not None:
            with self._automated_cursor_position_lock:
                expected_position = self._automated_cursor_position
                if current == expected_position and expected_position is not None:
                    self._last_cursor_position = current
                    automated_move = True
                    if not self._automated_cursor_move_in_progress:
                        self._automated_cursor_position = None
                elif (
                    self._automated_cursor_move_in_progress
                    and current == self._last_cursor_position
                ):
                    automated_move = True
        if (
            self._is_looping()
            and not automated_move
            and current is not None
            and self._last_cursor_position is not None
            and current != self._last_cursor_position
        ):
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
    #   処理: [観測をUIへ表示し、loop評価だけを実行世代へ結び付けてworkerを開始する]
    #   引数: [loop_step: Trueなら結果を現在の連続実行stepに限定し、Falseなら停止後も独立して評価する]
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
            run_token = self.controller.rearm_token if loop_step else None
            self._start_assessment_worker(observation, loop_step=loop_step, run_token=run_token)
            return observation
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "screen_capture"})
            messagebox.showerror("画面取得エラー", str(exc))
            return None

    # {
    #   責務: [_start_assessment_worker: Ollamaを含む画面状態評価をUIスレッド外で開始する]
    #   処理: [UI設定をworkerへ渡し、起動成功後にその評価IDを最新IDとして記録する]
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
            self._latest_assessment_task_id = task_id
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
    #   責務: [_start_pipeline_worker: セッションpipelineのstepをUIスレッド外で開始する]
    #   処理: [開始時の実行世代を固定し、使い捨てpipelineだけworker終了時に閉じる]
    #   引数: [pipeline: 実行パイプライン, operation: 判断または実行, purpose: ゲームの目的, personality: 判断人格, run_token: 実行開始世代, loop_step: 連続実行由来, close_pipeline_on_exit: worker終了時にpipelineを閉じるか]
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
        close_pipeline_on_exit: bool = True,
    ) -> bool:
        if self._execution_in_progress:
            if close_pipeline_on_exit:
                pipeline.close()
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return False

        self._background_task_counter += 1
        task_id = self._background_task_counter
        self._execution_task_id = task_id
        self._execution_in_progress = True
        worker = threading.Thread(
            target=self._run_pipeline_in_background,
            args=(task_id, pipeline, operation, purpose, personality, run_token, loop_step, close_pipeline_on_exit),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self._execution_in_progress = False
            self._execution_task_id = None
            if close_pipeline_on_exit:
                pipeline.close()
            raise
        self._set_status("Ollama推論中" if self.provider.get() == "Ollama" else "判断・実行中")
        return True

    # {
    #   責務: [_run_pipeline_in_background: pipelineを実行し結果・例外・終了処理をqueueへ送る]
    #   処理: [Tkを操作せず判断または実行を行い、指定された場合だけ使い捨てpipelineをcloseする]
    #   引数: [task_id: worker識別子, pipeline: 対象pipeline, operation: 判断または実行, run_token: 実行世代, close_pipeline_on_exit: セッション所有か使い捨てかを示す]
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
        close_pipeline_on_exit: bool,
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
            if close_pipeline_on_exit:
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
    #   処理: [実行世代と評価worker IDを確認して有効結果だけを表示し、カーソル停止基準は監視処理だけで更新する]
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
                if task_id != self._latest_assessment_task_id:
                    self.runtime_log.write(
                        "run_control",
                        "outcome_result_discarded",
                        {
                            "reason": "superseded by newer assessment",
                            "task_id": task_id,
                            "latest_task_id": self._latest_assessment_task_id,
                        },
                    )
                    if loop_step and self._is_looping() and self._run_is_current(run_token):
                        self.stop("assessment_superseded")
                    continue
                if error is not None:
                    self.runtime_log.write("error", str(error), {"operation": "outcome_assessment"})
                    if loop_step and self._is_looping():
                        self.stop("outcome_assessment_failed")
                    continue
                if not isinstance(result, OutcomeAssessment) or observation is None:
                    self.runtime_log.write("error", "状態評価workerの結果形式が不正です", {"operation": "outcome_assessment"})
                    if loop_step and self._is_looping():
                        self.stop("invalid_outcome_assessment_result")
                    continue
                self.previous_observation = observation
                self.current_assessment = result
                self.outcome.config(text=f"状態: {result.status} ({result.confidence:.0%})")
                self.runtime_log.write("outcome", result.reason, {"status": result.status, "confidence": result.confidence, "provider": self.provider.get()})
                if loop_step:
                    if observation is not None:
                        try:
                            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
                            self.memory_source.update(observation, candidates)
                            evaluation = ActionEvaluator().explain(observation, candidates)
                            self.evaluation.delete("1.0", tk.END)
                            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
                        except Exception as exc:
                            self.stop(f"invalid_loop_candidates: {exc}")
                            continue
                    if hasattr(self, "session_controller"):
                        self.session_controller.complete_loop_observation(LoopObservation(observation, result.status))
                    else:
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
                    self._complete_session_step(error)
                    if getattr(self, "_closing", False) and not self.session_controller.has_pending_step:
                        self.root.destroy()
                    continue
                self.runtime_log.write("error", str(error), {"operation": operation})
                messagebox.showerror("実行エラー" if operation == "execute" else "判断エラー", str(error))
                self._complete_session_step(error)
                if getattr(self, "_closing", False) and not self.session_controller.has_pending_step:
                    self.root.destroy()
                continue
            if not self._run_is_current(run_token):
                self.runtime_log.write("run_control", "inference_result_discarded", {"reason": self.controller.stop_reason or "実行世代が変更されました", "operation": operation})
                self._complete_session_step()
                if getattr(self, "_closing", False) and not self.session_controller.has_pending_step:
                    self.root.destroy()
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

            self._complete_session_step()
            if getattr(self, "_closing", False) and not self.session_controller.has_pending_step:
                self.root.destroy()
        self.root.after(BACKGROUND_RESULT_POLL_INTERVAL_MS, self._poll_background_results)

    # {
    #   責務: [
    #     start_loop: 対象を検証してから連続実行を開始する
    #   ]
    #   処理: [
    #     1: 実入力対象のHWNDとPIDを検証する
    #     2: 不正なら記録と通知を行い開始を拒否する
    #     3: 古い自動移動記録を消去してカーソル基準位置を保存する
    #     4: 正常ならcontrollerを開始し次stepを予約する
    #   ]
    #   引数: []
    #   戻り値: []
    # }
    def start_loop(self) -> None:
        if self._is_looping():
            return
        if not hasattr(self, "session_controller"):
            try:
                self._validate_live_execution()
            except ValueError as exc:
                self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
                messagebox.showerror("連続実行エラー", str(exc))
                self._set_status("連続実行を開始できません")
                return
            self.controller.start()
            self._loop_active = True
            self._record_automated_cursor_position(None, False)
            self._last_cursor_position = self._cursor_position()
            self.loop_guard.reset()
            self.runtime_log.write("run_control", "loop_started")
            self.loop_job = self.root.after(LOOP_STEP_DELAY_MS, self._loop_step)
            self._set_status("連続実行中")
            return
        if self._execution_in_progress or getattr(getattr(self, "session_controller", None), "has_pending_step", False):
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        try:
            self._validate_live_execution()
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            self.memory_source.update(observation, candidates)
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
        except ValueError as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            messagebox.showerror("連続実行エラー", str(exc))
            self._set_status("連続実行を開始できません")
            return
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            messagebox.showerror("連続実行エラー", str(exc))
            return

        self._session_dry_run = not self.live_execution.get()
        self._record_automated_cursor_position(None, False)
        self._last_cursor_position = self._cursor_position()
        try:
            self.session_controller.start_loop("execute", LOOP_STEP_DELAY_MS)
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            messagebox.showerror("連続実行エラー", str(exc))

    # {
    #   責務: [start: 「再開」操作で実行世代を進め、停止状態を解除する]
    #   処理: [連続実行が有効なら旧loopと予約callbackを解除し、RunControllerを再開して状態とログを更新する]
    #   引数: []
    #   戻り値: []
    # }
    def start(self) -> None:
        if not hasattr(self, "session_controller"):
            if getattr(self, "_loop_active", False) or getattr(self, "loop_job", None) is not None:
                self._clear_loop_state()
                self.runtime_log.write("run_control", "loop_stopped_by_rearm")
            self.controller.start()
            self.runtime_log.write("run_control", "started")
            self._set_status("実行可能")
            return
        if self.session_controller.has_pending_step:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        if self.session_controller.status is SessionStatus.RUNNING:
            self.session_controller.stop("rearmed")
        try:
            self.session_controller.start()
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_session"})
            messagebox.showerror("再開エラー", str(exc))
            return
        self.runtime_log.write("run_control", "started")
        self._set_status("実行可能")

    # {
    #   責務: [stop: 停止要求を即時記録しloop予約と実入力許可を止める]
    #   処理: [controllerを停止状態にし、予約済みloopを取り消して理由を表示する]
    #   引数: [reason: 停止を要求した要因]
    #   戻り値: []
    # }
    def stop(self, reason: str = "ユーザー停止") -> None:
        if hasattr(self, "session_controller"):
            self.session_controller.stop(reason)
        else:
            self.controller.stop(reason)
            self._clear_loop_state()
        self.runtime_log.write("run_control", "stopped", {"reason": reason})
        status = f"停止要求を受け付けました（{reason}）。推論結果は破棄します" if self._execution_in_progress else f"停止中（{reason}）"
        self._set_status(status)

    # {
    #   責務: [run_and_execute: 現在の観測と候補を検証してSessionへ実行stepを依頼する]
    #   処理: [設定を保存し、Session runtimeへ渡すデータを更新して非同期stepを開始する]
    #   引数: []
    #   戻り値: [bool: Sessionが実行stepを受け付けた場合はTrue]
    # }
    def run_and_execute(self) -> bool:
        return self._run_session_step("execute")

    # {
    #   責務: [run: 実入力を許可せず候補判断をworkerへ委譲する]
    #   処理: [画面入力と設定をUI threadで読み、実行世代付きpipelineを非同期起動する]
    #   引数: []
    #   戻り値: []
    # }
    def run(self) -> None:
        self._run_session_step("decision")

    # {
    #   責務: [_run_session_step: UI入力を検証してSessionへ非同期stepを依頼する]
    #   処理: [重複stepを拒否し、観測・候補・設定を更新した後にSession controllerを呼び出す]
    #   引数: [command: executeなら安全評価後に実行し、decisionなら候補判断だけを行う]
    #   戻り値: [bool: step要求を受け付けた場合はTrue]
    # }
    def _run_session_step(self, command: str) -> bool:
        if not hasattr(self, "session_controller"):
            try:
                self._validate_live_execution()
            except Exception as exc:
                self.runtime_log.write("error", str(exc), {"operation": command})
                messagebox.showerror("実行エラー", str(exc))
                return False
            return False
        if self._is_looping():
            self._set_status("連続実行中は単独stepを開始できません")
            return False
        if self.session_controller.has_pending_step or self._execution_in_progress:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return False
        try:
            self._validate_live_execution()
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            evaluation = ActionEvaluator().explain(observation, candidates)
            self.evaluation.delete("1.0", tk.END)
            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
            self.memory_source.update(observation, candidates)
            self._session_dry_run = command != "execute" or not self.live_execution.get()
            if self.session_controller.status is SessionStatus.RUNNING:
                self.session_controller.stop("new_step_configuration")
            self.session_controller.step(command)
            return True
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": command})
            messagebox.showerror("実行エラー" if command == "execute" else "判断エラー", str(exc))
            return False

    # {
    #   責務: [close: アプリ終了時にSessionを停止し、実行中workerの安全な終了を待つ]
    #   処理: [loopを停止し、pending stepがあれば結果pollerに終了後のroot破棄を任せる]
    #   引数: []
    #   戻り値: []
    # }
    def close(self) -> None:
        self._closing = True
        self.session_controller.stop("application_closed")
        if not self.session_controller.has_pending_step:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
