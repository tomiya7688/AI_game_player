import os
import ctypes
import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.outcome import OutcomeEvaluator
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import RunController
from ai_game_player.window_selector import WindowsWindowSelector
from ai_game_player.screen_capture import WindowsScreenCapture


class MemorySource:
    def __init__(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> None:
        self.observation = observation
        self.candidates = candidates

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        return self.observation, self.candidates


class Application:
    def __init__(self, root: tk.Tk) -> None:
        config_store = ConfigStore(Path("data/config.json"))
        config = config_store.load()
        self.root = root
        self.runtime_log = RuntimeLog()
        self.controller = RunController()
        self.windows: list = []
        self.window_handles: dict[str, int] = {}
        self.loop_job: str | None = None
        self._last_cursor_position: tuple[int, int] | None = None
        self.outcome_evaluator = OutcomeEvaluator()
        root.title("AI Game Player - Decision Sandbox")
        root.geometry("900x650")
        root.bind("<Escape>", lambda _event: self.stop())
        frame = ttk.Frame(root, padding=10)
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
        prompt_settings = ttk.Frame(frame)
        prompt_settings.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(prompt_settings, text="人格").pack(side=tk.LEFT)
        ttk.Entry(prompt_settings, textvariable=self.personality, width=20).pack(side=tk.LEFT, padx=5)
        ttk.Label(prompt_settings, text="目的").pack(side=tk.LEFT)
        ttk.Entry(prompt_settings, textvariable=self.purpose, width=34).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(prompt_settings, text="実入力を許可", variable=self.live_execution).pack(side=tk.LEFT, padx=5)
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
        ttk.Label(frame, text="停止方法: ■ 停止ボタン / Esc / F12 / 手動マウス移動").pack(anchor=tk.W)
        ttk.Button(controls, text="1ステップ判断（操作は実行しない）", command=self.run).pack(side=tk.LEFT)
        ttk.Button(controls, text="判断＋実行（dry-run）", command=self.run_and_execute).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="連続dry-run開始", command=self.start_loop).pack(side=tk.LEFT)
        ttk.Button(controls, text="■ 停止（連続実行を停止）", command=self.stop).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="再開", command=self.start).pack(side=tk.LEFT)
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
        if os.name == "nt":
            self.refresh_windows()
            self._poll_global_stop()

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
        if self.loop_job is not None and self._last_cursor_position is not None and current != self._last_cursor_position:
            self.runtime_log.write("run_control", "stopped_by_manual_mouse_move")
            self.stop()
        self.root.after(100, self._poll_global_stop)

    def refresh_models(self) -> None:
        try:
            models = OllamaProvider.list_models(self.endpoint.get())
            self.model_combo["values"] = models
            if models and self.model.get() not in models:
                self.model.set(models[0])
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "model_list"})
            self.result.config(text="Ollamaモデル一覧を取得できません")

    def refresh_windows(self) -> None:
        try:
            self.windows = WindowsWindowSelector().list_windows()
            self.window_handles = {window.title: window.handle for window in self.windows}
            self.window_combo["values"] = list(self.window_handles)
            if self.window_handles and not self.window_choice.get():
                self.window_choice.set(next(iter(self.window_handles)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "window_list"})
            messagebox.showerror("ウィンドウ一覧エラー", str(exc))

    def capture_screen(self) -> None:
        try:
            from ai_game_player.frame_analyzer import FrameAnalyzer
            selected_handle = self.window_handles.get(self.window_choice.get())
            observation = FrameAnalyzer().analyze(WindowsScreenCapture().capture(selected_handle), "live")
            self.obs.delete("1.0", tk.END)
            self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            assessment = self.outcome_evaluator.assess(observation)
            self.outcome.config(text=f"状態: {assessment.status} ({assessment.confidence:.0%})")
            self.runtime_log.write("screen_capture", "observation updated", {"screen_id": observation.screen_id})
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "screen_capture"})
            messagebox.showerror("画面取得エラー", str(exc))

    def start_loop(self) -> None:
        self.controller.start()
        if self.loop_job is None:
            self._last_cursor_position = self._cursor_position()
            self.runtime_log.write("run_control", "loop_started")
            self.loop_job = self.root.after(1000, self._loop_step)
            self.result.config(text="連続dry-run中")

    def _loop_step(self) -> None:
        self.loop_job = None
        if not self.controller.is_running:
            return
        if os.name == "nt":
            self.capture_screen()
        current_observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        assessment = self.outcome_evaluator.assess(current_observation)
        if assessment.status in {"success", "failure"}:
            self.runtime_log.write("run_control", "loop_stopped_by_outcome", {"status": assessment.status})
            self.stop()
            return
        self.run_and_execute()
        self._last_cursor_position = self._cursor_position()
        if self.controller.is_running:
            self.loop_job = self.root.after(1000, self._loop_step)

    def start(self) -> None:
        self.controller.start()
        self.runtime_log.write("run_control", "started")
        self.result.config(text="実行可能")

    def stop(self) -> None:
        self.controller.stop()
        if self.loop_job is not None:
            self.root.after_cancel(self.loop_job)
            self.loop_job = None
        self.runtime_log.write("run_control", "stopped")
        self.result.config(text="停止中")

    def run_and_execute(self) -> None:
        try:
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            evaluation = ActionEvaluator().explain(observation, candidates)
            self.evaluation.delete("1.0", tk.END)
            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=not self.live_execution.get(), window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get())
            result = pipeline.run_and_execute(purpose=self.purpose.get(), personality=self.personality.get())
            self.result.config(text=f"実行: {result.action_id} / {result.mode} / {result.detail}")
            metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
            self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
            self.runtime_log.write("execution", result.detail, {"action_id": result.action_id, "mode": result.mode})
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "execution"})
            messagebox.showerror("実行エラー", str(exc))
    def run(self) -> None:
        try:
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=not self.live_execution.get(), window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get())
            decision = pipeline.run(purpose=self.purpose.get(), personality=self.personality.get())
            self.result.config(text=f"選択: {decision.action_id} / {decision.reason}")
            metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
            self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
            self.runtime_log.write("decision", decision.reason, {"action_id": decision.action_id, "provider": self.provider.get()})
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "decision"})
            messagebox.showerror("判断エラー", str(exc))


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()