import os
import ctypes
import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.execution_mode import execution_labels
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.game_session import GameSessionController, LoopObservation, SessionRuntime, SessionSnapshot, SessionStatus, SessionStep
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.outcome import OutcomeEvaluator
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.window_selector import WindowsWindowSelector
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.ui.shell import ApplicationShell, ShellState, ShellStateStore


class MemorySource:
    def __init__(self) -> None:
        self.observation: ScreenObservation | None = None
        self.candidates: list[ActionCandidate] = []

    def update(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> None:
        self.observation = observation
        self.candidates = candidates

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        if self.observation is None:
            raise RuntimeError("The session has no observation yet")
        return self.observation, self.candidates


class TkAfterScheduler:
    """Adapt Tk's timer to the GUI-independent session scheduler contract."""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root

    def schedule(self, delay_ms: int, callback) -> object:
        return self.root.after(delay_ms, callback)

    def cancel(self, token: object) -> None:
        self.root.after_cancel(token)


class Application:
    def __init__(self, root: tk.Tk) -> None:
        config_store = ConfigStore(Path("data/config.json"))
        config = config_store.load()
        self.root = root
        self.runtime_log = RuntimeLog()
        self.memory_source = MemorySource()
        self._session_dry_run = True
        self._session_signature = None
        self._last_session_mode: tuple[SessionStatus, bool] | None = None
        self.windows: list = []
        self.window_handles: dict[str, int] = {}
        self._last_cursor_position: tuple[int, int] | None = None
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
        if os.name == "nt":
            self.refresh_windows()
            self._poll_global_stop()

    def _save_current_config(self) -> None:
        self.config_store.save(AppConfig(
            self.provider.get(),
            self.model.get(),
            self.endpoint.get(),
            self.personality.get(),
            self.purpose.get(),
            self.live_execution.get(),
            self.input_mode.get(),
        ))

    def _update_memory_source(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
        self.memory_source.update(observation, candidates)
        return observation, candidates

    def _create_session_runtime(self) -> DecisionPipeline:
        provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
        return DecisionPipeline(
            self.memory_source,
            Path("data/games/sandbox"),
            provider,
            self.controller,
            dry_run=self._session_dry_run,
            window_handle=self.window_handles.get(self.window_choice.get()),
            input_mode=self.input_mode.get(),
        )

    def _ensure_session(self, dry_run: bool) -> None:
        signature = (
            dry_run,
            self.provider.get(),
            self.model.get(),
            self.endpoint.get(),
            self.window_handles.get(self.window_choice.get()),
            self.input_mode.get(),
        )
        if self.session_controller.is_running and signature == self._session_signature:
            return
        if self.session_controller.is_running:
            self.session_controller.stop("session settings changed")
        self._session_dry_run = dry_run
        self._session_signature = signature
        self.outcome_evaluator = OutcomeEvaluator()
        self.previous_observation = None
        self.current_assessment = None
        self.session_controller.start()

    def _observe_for_loop(self) -> LoopObservation:
        if os.name == "nt" and self.capture_screen() is None:
            self.runtime_log.write("run_control", "loop_stopped_by_capture_failure")
            return LoopObservation(None, terminal_reason="capture failure")
        observation, _ = self._update_memory_source()
        assessment = self.current_assessment or self._assess_observation(observation)
        return LoopObservation(observation, assessment.status)

    def _perform_session_step(self, runtime: SessionRuntime, command: str) -> SessionStep:
        if command != "decide" and not self._session_dry_run and not self.live_execution.get():
            return SessionStep(terminal_reason="live execution permission revoked")
        observation, _ = self.memory_source.read()
        if command == "decide":
            decision = runtime.run(purpose=self.purpose.get(), personality=self.personality.get())
            self._set_status(f"選択: {decision.action_id} / {decision.reason}")
            self.runtime_log.write("decision", decision.reason, {"action_id": decision.action_id, "provider": self.provider.get()})
        else:
            evaluation = ActionEvaluator().explain(observation, self.memory_source.read()[1])
            self.evaluation.delete("1.0", tk.END)
            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
            result = runtime.run_and_execute(purpose=self.purpose.get(), personality=self.personality.get())
            self._set_status(f"実行: {result.action_id} / {result.mode} / {result.detail}")
            self.runtime_log.write("execution", result.detail, {"action_id": result.action_id, "mode": result.mode})
            if self.session_controller.is_looping:
                self._last_cursor_position = self._cursor_position()
        metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
        self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
        return SessionStep()

    def _on_session_state_change(self, snapshot: SessionSnapshot) -> None:
        self.runtime_log.write("session", snapshot.status.value, {
            "steps": snapshot.steps,
            "stop_reason": snapshot.stop_reason,
        })
        session_mode = (snapshot.status, snapshot.looping)
        mode_changed = session_mode != self._last_session_mode
        self._last_session_mode = session_mode
        if mode_changed and snapshot.status is SessionStatus.RUNNING:
            self._set_status("連続実行中" if snapshot.looping else "実行可能")
        elif mode_changed and snapshot.status is SessionStatus.COMPLETED:
            self._set_status(f"連続実行停止: {snapshot.stop_reason}")
        elif mode_changed and snapshot.status in {SessionStatus.STOPPED, SessionStatus.IDLE}:
            self._set_status("停止中")
        elif mode_changed and snapshot.status is SessionStatus.FAILED:
            self._set_status(f"セッションエラー: {snapshot.error}")

    def _on_session_error(self, error: Exception) -> None:
        self.runtime_log.write("error", str(error), {"operation": "game_session"})
        self._set_status(f"セッションエラー: {error}")
        messagebox.showerror("セッションエラー", str(error))

    def close(self) -> None:
        self.session_controller.stop("window closed")
        self.root.destroy()

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

    def _on_live_execution_changed(self) -> None:
        self._refresh_execution_controls()
        if (
            not self.live_execution.get()
            and self._session_dry_run is False
            and self.session_controller.is_running
        ):
            self.session_controller.stop("live execution permission revoked")

    def _validate_live_execution(self) -> None:
        if self.live_execution.get() and self.input_mode.get() == "window_message" and not self.window_handles.get(self.window_choice.get()):
            raise ValueError("実入力には対象ウィンドウの選択が必要です")

    def _cursor_position(self) -> tuple[int, int] | None:
        if os.name != "nt":
            return None
        point = (ctypes.c_long * 2)()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point[0]), int(point[1])

    def _poll_global_stop(self) -> None:
        if os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(0x7B) & 1:
            self.stop()
        current = self._cursor_position()
        if self.session_controller.is_looping and self._last_cursor_position is not None and current != self._last_cursor_position:
            self.runtime_log.write("run_control", "stopped_by_manual_mouse_move")
            self.stop()
        self.root.after(100, self._poll_global_stop)

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

    def refresh_windows(self) -> None:
        try:
            self.windows = WindowsWindowSelector().list_windows()
            self.window_handles = {window.display_name: window.handle for window in self.windows}
            self.window_combo["values"] = list(self.window_handles)
            if self.window_handles and not self.window_choice.get():
                self.window_choice.set(next(iter(self.window_handles)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "window_list"})
            messagebox.showerror("ウィンドウ一覧エラー", str(exc))

    def _assess_observation(self, observation: ScreenObservation):
        try:
            if self.provider.get() == "Ollama":
                assessment = OllamaProvider(self.model.get(), self.endpoint.get()).assess_outcome(observation, self.previous_observation)
            else:
                assessment = self.outcome_evaluator.assess(observation)
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "outcome_assessment"})
            assessment = self.outcome_evaluator.assess(observation)
        self.previous_observation = observation
        self.current_assessment = assessment
        self.runtime_log.write("outcome", assessment.reason, {"status": assessment.status, "confidence": assessment.confidence, "provider": self.provider.get()})
        return assessment

    def capture_screen(self) -> ScreenObservation | None:
        try:
            from ai_game_player.frame_analyzer import FrameAnalyzer
            selected_handle = self.window_handles.get(self.window_choice.get())
            observation = FrameAnalyzer(TesseractOcrRecognizer.optional()).analyze(WindowsScreenCapture().capture(selected_handle), "live")
            self.obs.delete("1.0", tk.END)
            self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            assessment = self._assess_observation(observation)
            self.outcome.config(text=f"状態: {assessment.status} ({assessment.confidence:.0%})")
            self.runtime_log.write("screen_capture", "observation updated", {"screen_id": observation.screen_id})
            return observation
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "screen_capture"})
            messagebox.showerror("画面取得エラー", str(exc))
            return None

    def start_loop(self) -> None:
        try:
            self._save_current_config()
            self._update_memory_source()
            if self.live_execution.get():
                self._validate_live_execution()
            self._ensure_session(dry_run=not self.live_execution.get())
            self._last_cursor_position = self._cursor_position()
            self.runtime_log.write("run_control", "loop_started")
            self.session_controller.start_loop(command="execute", interval_ms=1000)
        except Exception as exc:
            if self.session_controller.status is not SessionStatus.FAILED:
                self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
                messagebox.showerror("連続実行エラー", str(exc))

    def start(self) -> None:
        try:
            self._save_current_config()
            self._update_memory_source()
            self._ensure_session(dry_run=not self.live_execution.get())
            self.runtime_log.write("run_control", "started")
        except Exception as exc:
            if self.session_controller.status is not SessionStatus.FAILED:
                self.runtime_log.write("error", str(exc), {"operation": "start_session"})
                messagebox.showerror("開始エラー", str(exc))

    def stop(self) -> None:
        self.session_controller.stop()
        self.runtime_log.write("run_control", "stopped")

    def run_and_execute(self) -> None:
        try:
            self._save_current_config()
            self._update_memory_source()
            if self.live_execution.get():
                self._validate_live_execution()
            self._ensure_session(dry_run=not self.live_execution.get())
            self.session_controller.step("execute")
        except Exception as exc:
            if self.session_controller.status is not SessionStatus.FAILED:
                self.runtime_log.write("error", str(exc), {"operation": "execution"})
                messagebox.showerror("実行エラー", str(exc))

    def run(self) -> None:
        try:
            self._save_current_config()
            self._update_memory_source()
            self._ensure_session(dry_run=True)
            self.session_controller.step("decide")
        except Exception as exc:
            if self.session_controller.status is not SessionStatus.FAILED:
                self.runtime_log.write("error", str(exc), {"operation": "decision"})
                messagebox.showerror("判断エラー", str(exc))


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
