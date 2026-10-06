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
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.outcome import OutcomeEvaluator
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import RunController
from ai_game_player.window_selector import WindowsWindowSelector
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.ui.shell import ApplicationShell, ShellState, ShellStateStore


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

    def _poll_global_stop(self) -> None:
        if os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(0x7B) & 1:
            self.stop()
        current = self._cursor_position()
        if self.loop_job is not None and self._last_cursor_position is not None and current != self._last_cursor_position:
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
        try:
            self._validate_live_execution()
        except ValueError as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            messagebox.showerror("連続実行エラー", str(exc))
            self._set_status("連続実行を開始できません")
            return

        self.controller.start()
        if self.loop_job is None:
            self._last_cursor_position = self._cursor_position()
            self.loop_guard.reset()
            self.runtime_log.write("run_control", "loop_started")
            self.loop_job = self.root.after(1000, self._loop_step)
            self._set_status("連続dry-run中")

    # {
    #   責務: [
    #     _loop_step: 連続実行の1stepを実行し、失敗時にloopを止める
    #   ]
    #   処理: [
    #     1: 実行中状態と画面・outcomeを確認する
    #     2: 実行に失敗したらloopを停止する
    #     3: 成功した場合だけ次stepを予約する
    #   ]
    #   引数: []
    #   戻り値: []
    # }
    def _loop_step(self) -> None:
        self.loop_job = None
        if not self.controller.is_running:
            return
        if os.name == "nt" and self.capture_screen() is None:
            self.runtime_log.write("run_control", "loop_stopped_by_capture_failure")
            self.stop()
            return
        current_observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        assessment = self.current_assessment or self._assess_observation(current_observation)
        if self.loop_guard.observe(current_observation):
            self.runtime_log.write("run_control", "loop_stopped_by_repeat")
            self.stop()
            return
        if assessment.status in {"success", "failure"}:
            self.runtime_log.write("run_control", "loop_stopped_by_outcome", {"status": assessment.status})
            self.stop()
            return
        if not self.run_and_execute():
            self.stop()
            return
        self._last_cursor_position = self._cursor_position()
        if self.controller.is_running:
            self.loop_job = self.root.after(1000, self._loop_step)

    def start(self) -> None:
        self.controller.start()
        self.runtime_log.write("run_control", "started")
        self._set_status("実行可能")

    def stop(self) -> None:
        self.controller.stop()
        if self.loop_job is not None:
            self.root.after_cancel(self.loop_job)
            self.loop_job = None
        self.runtime_log.write("run_control", "stopped")
        self._set_status("停止中")

    # {
    #   責務: [
    #     run_and_execute: 候補を判断・実行し結果をUIとログへ返す
    #   ]
    #   処理: [
    #     1: 実入力対象を検証する
    #     2: Observationと候補を読み込む
    #     3: DecisionPipelineで判断・実行する
    #     4: 結果を表示・記録する
    #   ]
    #   引数: []
    #   戻り値: [
    #     bool: 実行完了時True、例外時False
    #   ]
    # }
    def run_and_execute(self) -> bool:
        pipeline: DecisionPipeline | None = None
        try:
            self._validate_live_execution()
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            evaluation = ActionEvaluator().explain(observation, candidates)
            self.evaluation.delete("1.0", tk.END)
            self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=not self.live_execution.get(), window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get(), window_process_id=self.window_process_ids.get(self.window_choice.get()))
            result = pipeline.run_and_execute(purpose=self.purpose.get(), personality=self.personality.get())
            self._set_status(f"実行: {result.action_id} / {result.mode} / {result.detail}")
            metrics = MetricsCalculator().calculate(ExecutionHistory(Path("data/games/sandbox/execution_history.json")).load())
            self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")
            self.runtime_log.write("execution", result.detail, {"action_id": result.action_id, "mode": result.mode})
            return True
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "execution"})
            messagebox.showerror("実行エラー", str(exc))
            return False
        finally:
            if pipeline is not None:
                pipeline.close()

    # {
    #   責務: [
    #     run: 実入力を行わず候補判断と評価を表示する
    #   ]
    #   処理: [
    #     1: 設定・Observation・候補を読み込む
    #     2: dry-run用DecisionPipelineを実行する
    #     3: 判断とmetricsを画面・ログへ表示する
    #   ]
    #   引数: []
    #   戻り値: []
    # }
    def run(self) -> None:
        try:
            self.config_store.save(AppConfig(self.provider.get(), self.model.get(), self.endpoint.get(), self.personality.get(), self.purpose.get(), self.live_execution.get(), self.input_mode.get()))
            observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
            candidates = [ActionCandidate.from_dict(item) for item in json.loads(self.actions.get("1.0", tk.END))]
            provider = OllamaProvider(self.model.get(), self.endpoint.get()) if self.provider.get() == "Ollama" else RuleProvider()
            pipeline = DecisionPipeline(MemorySource(observation, candidates), Path("data/games/sandbox"), provider, self.controller, dry_run=True, window_handle=self.window_handles.get(self.window_choice.get()), input_mode=self.input_mode.get(), window_process_id=self.window_process_ids.get(self.window_choice.get()))
            decision = pipeline.run(purpose=self.purpose.get(), personality=self.personality.get())
            self._set_status(f"選択: {decision.action_id} / {decision.reason}")
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
