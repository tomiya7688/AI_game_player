import os
import ctypes
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.application_workflow import (
    ApplicationSettings,
    ApplicationWorkflowService,
    MemorySource,
    PreparedObservation,
)
from ai_game_player.capture_region import (
    CaptureRegion,
    CaptureRegionMode,
    NormalizedRect,
    PixelRect,
)
from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.execution_mode import execution_labels
from ai_game_player.metrics import MetricsCalculator
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.game_session import GameSessionController, LoopObservation, SessionRuntime, SessionSnapshot, SessionStatus, SessionStep
from ai_game_player.models import ActionDecision, ScreenObservation
from ai_game_player.observation_service import LiveObservationService
from ai_game_player.player_character import PlayerState
from ai_game_player.runtime.session_composition import RuntimeComposition
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import ExecutionCancelled
from ai_game_player.window_selector import WindowsWindowSelector
from ai_game_player.ui.shell import ApplicationShell, ShellState, ShellStateStore
from ai_game_player.ui.stop_help_hint import StopHelpHint
from ai_game_player.ui.theme import apply_misk_theme
from ai_game_player.ui.tooltip import ToolTip


# {
#   責務: [Application scheduling constants: Tkイベントポーリングと連続実行間隔を明示する]
#   フィールド: [background_result_poll_interval_ms: worker結果確認間隔, global_stop_poll_interval_ms: 緊急停止確認間隔, loop_step_delay_ms: 連続実行step間隔, windows_f12_virtual_key_code: F12キー識別値, windows_key_pressed_flag: キー状態フラグ]
# }
BACKGROUND_RESULT_POLL_INTERVAL_MS = 50
GLOBAL_STOP_POLL_INTERVAL_MS = 100
LOOP_STEP_DELAY_MS = 1000
WINDOWS_F12_VIRTUAL_KEY_CODE = 0x7B
WINDOWS_KEY_PRESSED_FLAG = 1
# OutcomeEvaluatorが返す状態。これ以外はPlayer Seatで「状態不明」と表示する。
KNOWN_OUTCOME_STATUSES = frozenset({"success", "failure", "ongoing"})
# 連続実行の画面取得失敗はSessionを完了扱いで終えるため、この理由ならPlayer Seatは要確認を示す。
LOOP_CAPTURE_FAILED_REASON = "capture failed"


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
#     Application: UI入力、capture領域、ゲーム実行状態、対象ウィンドウを管理する
#   ]
#   フィールド: [
#     window_handles: 表示名ごとのHWND
#     window_process_ids: 表示名ごとの列挙時PID
#     selected_window_display_name_var: Play Workspaceで選んだ対象windowの表示名
#     window_selection_status_var: 対象windowの候補と選択状態を説明する表示
#     live_execution: 実入力の許可状態
#     live_execution_toggle: Sessionの実行中に実入力設定を変更できないことを示すcheckbox
#     _session_dry_run: 現在のSessionが開始時に固定した入力モード
#     capture_mode/capture_region_x/y/width/height: capture方式とfixed/normalized ROIの入力値
#     _active_capture_region: capture serviceへ最後に適用したROI設定
#     _latest_assessment_task_id: 結果を適用できる最新の画面評価worker ID
#     _automated_cursor_position: 入力workerが通知した自動移動の予定座標と進行状態
#   ]
#   処理: [
#     1: UI commandの入力を検証する
#     2: 対象識別情報を実行パイプラインへ渡す
#     3: 画面評価結果の新旧を判定する
#     4: 自動カーソル移動を停止監視へ通知する
#     5: 実行結果と状態を画面へ表示する
#     6: 画面取得後に統合候補の由来と安全評価を表示する
#   ]
# }
class Application:
    # {
    #   責務: [
    #     __init__: UIと実行制御の初期状態を構築する
    #   ]
    #   処理: [
    #     1: 保存済みの判断設定・入力方式・画面取得領域を読み、実行制御を初期化する
    #     2: Play作業画面の上部に対象ウィンドウ選択欄を作り、OSのウィンドウ識別値とプロセスIDを管理する
    #     3: 一覧更新ボタンへ共通ToolTipを接続し、対象ウィンドウ検出の更新契機を案内する
    #     4: バックグラウンド処理の結果受取状態と、最新の画面評価の識別子を初期化する
    #     5: 自動入力によるカーソル位置をバックグラウンド処理と画面側で共有する状態を初期化する
    #     6: 画面取得方式と取得領域のUI入力を作り、設定の保存処理と画面取得部品へ接続する
    #     7: 任意入力の操作候補JSONと、候補評価の表示欄を作る
    #     8: ボタン・表示と実行処理を接続し、推論・評価・入力の部品を作る構成をセッション制御へ渡す
    #     9: StopHelpHintで共通の停止方法と補足説明の出し方を案内し、Play領域の幅で折り返す
    #     10: 実入力の許可欄に共通ToolTipを接続し、入力の発生条件・判断だけの場合・設定変更できない状態を説明する
    #   ]
    #   引数: [
    #     root: UIを配置する、作成済みのTkルートウィンドウ
    #     runtime_composition: 推論方法・評価器・操作判断パイプラインを生成する構成。Noneは標準構成
    #     observation_service: ゲーム画面を取得・解析する部品。Noneは保存済み領域を使うWindows標準構成
    #   ]
    #   戻り値: [None: Widgetと実行制御を初期化し、Tkイベントループで操作を受け付ける準備をする]
    # }
    def __init__(
        self,
        root: tk.Tk,
        runtime_composition: RuntimeComposition | None = None,
        observation_service: LiveObservationService | None = None,
    ) -> None:
        config_store = ConfigStore(Path("data/config.json"))
        config = config_store.load()
        self.root = root
        runtime_composition = runtime_composition or RuntimeComposition()
        self.runtime_log = RuntimeLog()
        memory_source = MemorySource()
        self.observation_service = observation_service or LiveObservationService(
            capture_region=config.capture_region
        )
        self.config_store = config_store
        self._active_capture_region = config.capture_region
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
        self.previous_observation: ScreenObservation | None = None
        self.current_assessment = None
        root.title("AI Game Player - Decision Sandbox")
        root.geometry("1120x760")
        root.minsize(800, 600)
        root.bind("<Escape>", lambda _event: self.stop())
        apply_misk_theme(root)
        self.shell_state_store = ShellStateStore(Path("data/shell_state.json"))
        self.shell = ApplicationShell(root, self.shell_state_store.load(), self.stop, self._save_shell_state)
        self.shell.pack(fill=tk.BOTH, expand=True)
        if self.shell.header_icon is not None:
            try:
                root.iconphoto(True, self.shell.header_icon)
            except tk.TclError as exc:
                self.runtime_log.write("error", str(exc), {"operation": "window_icon"})
        frame = ttk.Frame(self.shell.play_host, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        game_window_selection_panel = ttk.LabelFrame(frame, text="ゲーム / ウィンドウ")
        game_window_selection_panel.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(game_window_selection_panel, text="対象ウィンドウ").pack(side=tk.LEFT)
        self.selected_window_display_name_var = tk.StringVar()
        self.window_selection_status_var = tk.StringVar(
            value="ゲームを起動して「一覧更新」を押してください。"
        )
        self.window_selector_combobox = ttk.Combobox(
            game_window_selection_panel,
            textvariable=self.selected_window_display_name_var,
            state="readonly",
            width=42,
        )
        self.window_selector_combobox.pack(side=tk.LEFT, padx=5)
        self.refresh_windows_button = ttk.Button(
            game_window_selection_panel,
            text="一覧更新",
            command=self.refresh_windows,
        )
        self.refresh_windows_button.pack(side=tk.LEFT)
        ToolTip(
            self.refresh_windows_button,
            "起動中のウィンドウ一覧を取得・更新します。対象ウィンドウを起動または再起動した後に押してください。",
        )
        ttk.Label(
            game_window_selection_panel,
            textvariable=self.window_selection_status_var,
            wraplength=680,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        self.selected_window_display_name_var.trace_add(
            "write",
            lambda *_args: self._update_window_selection_status(),
        )
        self.provider = tk.StringVar(value=config.provider)
        self.model = tk.StringVar(value=config.model)
        self.endpoint = tk.StringVar(value=config.endpoint)
        self.personality = tk.StringVar(value=config.personality)
        self.purpose = tk.StringVar(value=config.purpose)
        self.live_execution = tk.BooleanVar(value=config.live_execution)
        self.input_mode = tk.StringVar(value=config.input_mode)
        self.capture_mode = tk.StringVar(value=config.capture_region.mode.value)
        saved_region = config.capture_region.rect
        self.capture_region_x = tk.StringVar(value=str(saved_region.x) if saved_region else "0")
        self.capture_region_y = tk.StringVar(value=str(saved_region.y) if saved_region else "0")
        self.capture_region_width = tk.StringVar(value=str(saved_region.width) if saved_region else "0")
        self.capture_region_height = tk.StringVar(value=str(saved_region.height) if saved_region else "0")
        self.workflow_service = ApplicationWorkflowService(
            config_store,
            runtime_composition,
            memory_source,
        )
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
        self.live_execution_toggle = ttk.Checkbutton(
            prompt_settings,
            text="実入力を許可",
            variable=self.live_execution,
            command=self._on_live_execution_changed,
        )
        self.live_execution_toggle.pack(side=tk.LEFT, padx=5)
        ToolTip(
            self.live_execution_toggle,
            "実入力を許可してから操作を実行すると、アプリは選択したゲームウィンドウへ入力を送ります。"
            "判断だけの実行では入力しません。連続実行中や処理の完了待ちは変更できません。",
        )
        ttk.Label(prompt_settings, text="入力方式").pack(side=tk.LEFT)
        ttk.Combobox(prompt_settings, textvariable=self.input_mode, values=("window_message", "mouse"), state="readonly", width=16).pack(side=tk.LEFT, padx=5)
        capture_settings = ttk.Frame(frame)
        capture_settings.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(capture_settings, text="取得範囲").pack(side=tk.LEFT)
        ttk.Combobox(
            capture_settings,
            textvariable=self.capture_mode,
            values=tuple(mode.value for mode in CaptureRegionMode),
            state="readonly",
            width=26,
        ).pack(side=tk.LEFT, padx=5)
        for label, variable in (
            ("X", self.capture_region_x),
            ("Y", self.capture_region_y),
            ("幅", self.capture_region_width),
            ("高さ", self.capture_region_height),
        ):
            ttk.Label(capture_settings, text=label).pack(side=tk.LEFT)
            ttk.Entry(capture_settings, textvariable=variable, width=7).pack(side=tk.LEFT, padx=(2, 5))
        ttk.Label(capture_settings, text="固定領域はscreen px、window-relativeは0〜1の比率").pack(side=tk.LEFT)
        ttk.Label(frame, text="画面観測JSON").pack(anchor=tk.W)
        ttk.Button(frame, text="画面取得（Windows）", command=self.capture_screen).pack(anchor=tk.W)
        self.obs = tk.Text(frame, height=10)
        self.obs.pack(fill=tk.BOTH, expand=True)
        self.obs.insert("1.0", json.dumps({"screen_id": "title", "width": 1280, "height": 720, "ocr_text": ["NEW GAME", "OPTION"]}, ensure_ascii=False, indent=2))
        ttk.Label(
            frame,
            text="Automation候補JSON（任意。画面取得後にOCR・画像候補を自動統合）",
        ).pack(anchor=tk.W, pady=(8, 0))
        self.actions = tk.Text(frame, height=10)
        self.actions.pack(fill=tk.BOTH, expand=True)
        self.actions.insert("1.0", "[]")
        controls = ttk.Frame(frame)
        controls.pack(anchor=tk.W, pady=8)
        StopHelpHint(frame).pack(anchor=tk.W, fill=tk.X)
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

    # {
    #   責務: [_show_player_state: 判断・実行の進行をPlayer Seatの状態表示へ伝える]
    #   処理: [shellがPlayer Seatを持つ場合だけ状態を渡す。Player Seatは表示専用のため、戻り値のFalseとTk破棄後のTclErrorは無視してCore処理を続ける]
    #   引数: [state: 表示するPlayerState]
    #   戻り値: []
    # }
    def _show_player_state(self, state: PlayerState) -> None:
        set_player_state = getattr(getattr(self, "shell", None), "set_player_state", None)
        if callable(set_player_state):
            try:
                set_player_state(state)
            except tk.TclError:
                pass

    # {
    #   責務: [_refresh_execution_controls: 設定と実行中Sessionのmodeを操作表示へ反映する]
    #   処理: [Session開始時に固定した実際の入力modeを表示し、stepまたはloop実行中は実入力checkboxを無効にする]
    #   引数: []
    #   戻り値: []
    # }
    def _refresh_execution_controls(self) -> None:
        execute_label, loop_label, status = execution_labels(self.live_execution.get())
        session_controller = getattr(self, "session_controller", None)
        session_is_looping = bool(session_controller and session_controller.is_looping)
        step_is_pending = bool(session_controller and session_controller.has_pending_step)
        if session_is_looping or step_is_pending:
            active_mode = "実入力" if not self._session_dry_run else "dry-run（実入力なし）"
            status = f"連続実行中の動作: {active_mode}" if session_is_looping else f"現在のstepの動作: {active_mode}"
        self.execute_button.config(text=execute_label)
        self.loop_button.config(text=loop_label)
        self.live_status.config(text=status)
        self.live_execution_toggle.config(state="disabled" if session_is_looping or step_is_pending else "normal")

    # {
    #   責務: [_is_looping: Session controllerが保持する連続実行状態をUIへ返す]
    #   処理: [Session controllerの公開状態を読み、Applicationにloop状態を複製しない]
    #   引数: []
    #   戻り値: [bool: 連続実行中ならTrue]
    # }
    def _is_looping(self) -> bool:
        return self.session_controller.is_looping

    # {
    #   責務: [_complete_session_step: worker完了をSessionへ通知する]
    #   処理: [非同期workerの例外または成功をSession controllerへ渡す]
    #   引数: [error: worker失敗時の例外または成功時None]
    #   戻り値: []
    # }
    def _complete_session_step(self, error: Exception | None = None) -> None:
        self.session_controller.complete_step(error=error)

    # {
    #   責務: [_create_session_runtime: composition rootからSession Controller用runtimeを構築する]
    #   処理: [現在の実行設定を固定してComposition Rootへ渡し、Journalとruntime logを含む同じProvider・Pipelineをsession終了まで保持する]
    #   引数: []
    #   戻り値: [SessionRuntime: Session Controllerが終了まで保持するJournal付きruntime]
    # }
    def _create_session_runtime(self) -> SessionRuntime:
        self._validate_live_execution()
        selected_window = self.selected_window_display_name_var.get()
        return self.workflow_service.create_session_runtime(
            self._application_settings(),
            game_directory=Path("data/games/sandbox"),
            dry_run=self._session_dry_run,
            window_handle=self.window_handles.get(selected_window),
            window_process_id=self.window_process_ids.get(selected_window),
            controller=self.controller,
            cursor_callback=self._record_automated_cursor_position,
            runtime_log=self.runtime_log,
        )

    # {
    #   責務: [_perform_session_step: session runtimeをworkerへ渡してSessionへ完了待ちを返す]
    #   処理: [execute commandは実行、それ以外は判断として非同期起動しdeferred結果を返す]
    #   引数: [runtime: Session Controllerが所有するsession runtime, command: executeまたはdecision]
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
    #   責務: [_capture_region_from_controls: capture modeに応じて入力欄の値を検証済みROI設定へ変換する]
    #   引数: []
    #   戻り値: [CaptureRegion: desktop全体、window全体、screen pixel固定領域、またはwindow比率領域]
    #   エラー: [数値形式、正の寸法、0から1の範囲がmodeの要件を満たさない場合: capture前に理由を送出]
    # }
    def _capture_region_from_controls(self) -> CaptureRegion:
        mode = CaptureRegionMode(self.capture_mode.get())
        if mode in {CaptureRegionMode.FULL_SCREEN, CaptureRegionMode.WINDOW}:
            return CaptureRegion(mode)
        values = (
            self.capture_region_x.get(),
            self.capture_region_y.get(),
            self.capture_region_width.get(),
            self.capture_region_height.get(),
        )
        try:
            if mode is CaptureRegionMode.FIXED_REGION:
                rect = PixelRect(*(int(value) for value in values))
            else:
                rect = NormalizedRect(*(float(value) for value in values))
        except (TypeError, ValueError) as error:
            coordinate_unit = "screen pixel整数" if mode is CaptureRegionMode.FIXED_REGION else "0から1のwindow比率"
            raise ValueError(
                f"取得範囲のX・Y・幅・高さは{coordinate_unit}で入力してください: {error}"
            ) from error
        return CaptureRegion(mode, rect)

    # {
    #   責務: [_apply_capture_region: UIのROI設定を変更時だけconfig.jsonとcapture serviceへ反映する]
    #   引数: []
    #   戻り値: [CaptureRegion: capture backendへ設定した画面領域]
    #   エラー: [入力欄・設定保存・service更新の失敗: captureを始めず理由を呼び出し元へ送出]
    # }
    def _apply_capture_region(self) -> CaptureRegion:
        capture_region = self._capture_region_from_controls()
        if capture_region == self._active_capture_region:
            return capture_region
        self.config_store.save(
            AppConfig(
                provider=self.provider.get(),
                model=self.model.get(),
                endpoint=self.endpoint.get(),
                personality=self.personality.get(),
                purpose=self.purpose.get(),
                live_execution=self.live_execution.get(),
                input_mode=self.input_mode.get(),
                capture_region=capture_region,
            )
        )
        self.observation_service.set_capture_region(capture_region)
        self._active_capture_region = capture_region
        return capture_region

    # {
    #   責務: [_observe_for_loop: 連続実行の画面取得と非同期状態評価を開始する]
    #   処理: [Player Seatを観察中にして画面を取得し、loop世代付き評価workerを開始してSessionへ完了待ちを返す。取得失敗はPlayer Seatを要確認にして終了理由を返す]
    #   引数: []
    #   戻り値: [LoopObservation: 非同期評価のためdeferred状態にした観測]
    # }
    def _observe_for_loop(self) -> LoopObservation:
        self._show_player_state(PlayerState.OBSERVE)
        try:
            if os.name == "nt":
                self._apply_capture_region()
                selected_handle = self.window_handles.get(self.selected_window_display_name_var.get())
                observation = self.observation_service.capture_observation(selected_handle)
                self.obs.delete("1.0", tk.END)
                self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            else:
                observation = ScreenObservation(**json.loads(self.obs.get("1.0", tk.END)))
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "loop_observation"})
            self._show_player_state(PlayerState.WARNING)
            return LoopObservation(None, terminal_reason=LOOP_CAPTURE_FAILED_REASON)
        self._start_assessment_worker(
            observation,
            loop_step=True,
            run_token=self.controller.rearm_token,
        )
        return LoopObservation(observation, deferred=True)

    # {
    #   責務: [_on_session_state_change: Session状態をGUI表示とログへ反映する]
    #   処理: [実行modeとcheckbox可否を更新し、状態が変わったときだけ記録して停止・完了・失敗の結果を表示する。Player Seatには失敗を要確認、完了を評価中、停止を実行待ち中でも即時に停止として示す]
    #   引数: [snapshot: Sessionの状態とstep数]
    #   戻り値: []
    # }
    def _on_session_state_change(self, snapshot: SessionSnapshot) -> None:
        self._refresh_execution_controls()
        current = (snapshot.status, snapshot.looping)
        if current != self._last_session_mode:
            self.runtime_log.write("session", snapshot.status.value, {"steps": snapshot.steps, "reason": snapshot.stop_reason})
            self._last_session_mode = current
        if snapshot.status is SessionStatus.FAILED:
            self._set_status(f"セッション失敗: {snapshot.error or snapshot.stop_reason}")
            self._show_player_state(PlayerState.WARNING)
        elif snapshot.status is SessionStatus.COMPLETED:
            self._set_status(f"セッション完了: {snapshot.stop_reason or 'terminal'}")
            self._show_player_state(
                PlayerState.WARNING
                if snapshot.stop_reason == LOOP_CAPTURE_FAILED_REASON
                else PlayerState.EVALUATE
            )
        elif snapshot.status is SessionStatus.STOPPED:
            # 実行中workerの完了を待つ間も、停止を受け付けたことはPlayer Seatへすぐ示す。
            self._show_player_state(PlayerState.STOP)
            if not self._execution_in_progress:
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
    #   処理: [連続実行中またはpending worker中の変更を元に戻し、それ以外では表示を更新して実入力解除時にSessionを停止する]
    #   引数: []
    #   戻り値: []
    # }
    def _on_live_execution_changed(self) -> None:
        self._refresh_execution_controls()
        if self.session_controller.is_looping or self.session_controller.has_pending_step:
            self.live_execution.set(not self.live_execution.get())
            self._refresh_execution_controls()
            message = "連続実行中は実入力設定を変更できません" if self.session_controller.is_looping else "実行中は実入力設定を変更できません"
            self._set_status(message)
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

        selected_window = self.selected_window_display_name_var.get()
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
            models = self.workflow_service.list_models(self.endpoint.get())
            self.model_combo["values"] = models
            self._set_status(f"Ollama接続OK: {len(models)}モデル")
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "ollama_connection"})
            self._set_status("Ollama接続エラー")

    def refresh_models(self) -> None:
        try:
            models = self.workflow_service.list_models(self.endpoint.get())
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
    #     3: 既存の選択が一覧に残る場合は維持し、消えた場合は選び直しを求める
    #     4: 候補数と選択状態に応じた案内をPlay Workspaceへ表示する
    #     5: 一覧取得失敗時は候補と選択を消し、古いHWNDを実行対象に残さない
    #   ]
    #   引数: []
    #   戻り値: []
    #   副作用: [
    #     window_handles、window_process_ids、選択UI、選択状態の案内を更新する
    #   ]
    # }
    def refresh_windows(self) -> None:
        try:
            previous_window_display_name = self.selected_window_display_name_var.get()
            self.windows = WindowsWindowSelector().list_windows()
            self.window_handles = {window.display_name: window.handle for window in self.windows}
            self.window_process_ids = {window.display_name: window.process_id for window in self.windows}
            self.window_selector_combobox["values"] = list(self.window_handles)
            if previous_window_display_name in self.window_handles:
                self.selected_window_display_name_var.set(previous_window_display_name)
            elif previous_window_display_name:
                self.selected_window_display_name_var.set("")
            elif self.window_handles:
                self.selected_window_display_name_var.set(next(iter(self.window_handles)))
            self._update_window_selection_status()
        except Exception as exc:
            self.windows = []
            self.window_handles = {}
            self.window_process_ids = {}
            self.window_selector_combobox["values"] = []
            self.selected_window_display_name_var.set("")
            self.window_selection_status_var.set(
                "ウィンドウ一覧を取得できませんでした。エラー内容を確認して再試行してください。"
            )
            self.runtime_log.write("error", str(exc), {"operation": "window_list"})
            messagebox.showerror("ウィンドウ一覧エラー", str(exc))

    # {
    #   責務: [_update_window_selection_status: 対象候補と現在の選択状態を利用者へ案内する]
    #   処理: [選択済み、未選択、候補なしの状態に応じてPlay Workspaceの案内を切り替える]
    #   引数: []
    #   戻り値: []
    # }
    def _update_window_selection_status(self) -> None:
        selected_window = self.selected_window_display_name_var.get()
        if selected_window in self.window_handles:
            selection_message = f"選択中の対象: {selected_window}"
        elif self.window_handles:
            selection_message = "対象ウィンドウを選択してください。"
        else:
            selection_message = "ゲームを起動して「一覧更新」を押してください。"
        self.window_selection_status_var.set(selection_message)

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
            session = getattr(self, "session_controller", None)
            if session is not None and session.is_running:
                return session.assess_outcome(observation, previous)
            return self.workflow_service.assess_outcome(
                provider_name, model, endpoint, observation, previous
            )
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "outcome_assessment"})
            return self.workflow_service.assess_locally(observation)

    # {
    #   責務: [capture_screen: 選択ROIの画面を取得し、統合候補をdry-run評価欄へ表示する]
    #   処理: [Player Seatを観察中にし、ObservationをUIへ保存しOCR/画像/Automation候補を統合する。candidate preview失敗はcapture成功と分けて報告し、状態評価workerを開始する。取得失敗はPlayer Seatを要確認にする]
    #   引数: [loop_step: Trueなら結果を現在の連続実行stepに限定し、Falseなら停止後も独立して評価する]
    #   戻り値: [ScreenObservation | None: 取得した観測または取得失敗]
    # }
    def capture_screen(self, *, loop_step: bool = False) -> ScreenObservation | None:
        self._show_player_state(PlayerState.OBSERVE)
        try:
            self._apply_capture_region()
            selected_handle = self.window_handles.get(self.selected_window_display_name_var.get())
            observation = self.observation_service.capture_observation(selected_handle)
            self.obs.delete("1.0", tk.END)
            self.obs.insert("1.0", json.dumps(observation.to_dict(), ensure_ascii=False, indent=2))
            self.runtime_log.write("screen_capture", "observation updated", {"screen_id": observation.screen_id})
            try:
                self._update_candidate_preview(observation, register_for_session=False)
            except Exception as preview_error:
                self.runtime_log.write(
                    "error",
                    str(preview_error),
                    {"operation": "candidate_preview"},
                )
                self._set_status(f"画面は取得しましたが候補評価に失敗しました: {preview_error}")
            run_token = self.controller.rearm_token if loop_step else None
            self._start_assessment_worker(observation, loop_step=loop_step, run_token=run_token)
            return observation
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "screen_capture"})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("画面取得エラー", str(exc))
            return None

    # {
    #   責務: [_update_candidate_preview: 画面Observationから候補を統合し、由来と安全評価をGUIへ表示する]
    #   処理: [画面取得のpreviewでは共有Session sourceを変更せず、連続実行stepの確定時だけ評価済み候補をsourceへ登録する]
    #   引数: [observation: captureされた画面と検出候補, register_for_session: 評価候補をSession Pipeline sourceへ登録するか]
    #   戻り値: [PreparedObservation: 統合候補と由来付き評価結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: 入力候補のschemaが不正な場合]
    # }
    def _update_candidate_preview(
        self,
        observation: ScreenObservation,
        *,
        register_for_session: bool = True,
    ) -> PreparedObservation:
        update_observation = (
            self.workflow_service.update_observation
            if register_for_session
            else self.workflow_service.preview_observation
        )
        prepared = update_observation(observation, self.actions.get("1.0", tk.END))
        self._display_step_evaluation(prepared.evaluation)
        return prepared

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
        self._background_results.put(("assessment", task_id, run_token, "assessment", assessment, error, loop_step, observation, None))

    # {
    #   責務: [_start_pipeline_worker: セッションpipelineのstepをUIスレッド外で開始する]
    #   処理: [開始時の実行世代を固定し、使い捨てpipelineだけworker終了時に閉じる。Player Seatは実入力の実行なら操作中、判断とdry-run実行なら考え中にする]
    #   引数: [pipeline: 実行パイプライン, operation: 判断または実行, purpose: ゲームの目的, personality: 判断人格, run_token: 実行開始世代, loop_step: 連続実行由来, close_pipeline_on_exit: worker終了時にpipelineを閉じるか]
    #   戻り値: [bool: workerを開始したか]
    # }
    def _start_pipeline_worker(
        self,
        pipeline: SessionRuntime,
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
        # dry-runの実行は入力を送らないため、「操作中」ではなく「考え中」と示す。
        live_input = operation == "execute" and getattr(self, "_session_dry_run", True) is False
        self._show_player_state(PlayerState.ACT if live_input else PlayerState.THINK)
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
        pipeline: SessionRuntime,
        operation: str,
        purpose: str,
        personality: str,
        run_token: int,
        loop_step: bool,
        close_pipeline_on_exit: bool,
    ) -> None:
        result: object | None = None
        execution_records = None
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
            load_execution_history = getattr(pipeline, "load_execution_history", None)
            if callable(load_execution_history):
                execution_records = load_execution_history()
        except Exception as exc:
            error = exc
        finally:
            if close_pipeline_on_exit:
                try:
                    pipeline.close()
                except Exception as exc:
                    if error is None:
                        error = exc
        self._background_results.put(("pipeline", task_id, run_token, operation, result, error, loop_step, None, execution_records))

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
    #   処理: [実行世代と評価worker IDを確認して有効結果だけを表示し、判断・実行ActionはSession状態とは別にshellへ保持し、ログ障害時もpipeline stepを完了し、カーソル停止基準は監視処理だけで更新する。Player Seatは評価結果で評価中または状態不明、失敗で要確認にするが、判断・実行worker中は評価結果で上書きしない]
    #   引数: []
    #   戻り値: []
    # }
    def _poll_background_results(self) -> None:
        while True:
            try:
                task_type, task_id, run_token, operation, result, error, loop_step, observation, execution_records = self._background_results.get_nowait()
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
                    self._show_player_state(PlayerState.WARNING)
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
                # 判断・実行workerが動いている間は、その考え中・操作中の表示を評価結果で上書きしない。
                if not getattr(self, "_execution_in_progress", False):
                    self._show_player_state(
                        PlayerState.EVALUATE
                        if result.status in KNOWN_OUTCOME_STATUSES
                        else PlayerState.UNKNOWN
                    )
                self.runtime_log.write("outcome", result.reason, {"status": result.status, "confidence": result.confidence, "provider": self.provider.get()})
                if loop_step:
                    if observation is not None:
                        try:
                            self._update_candidate_preview(observation)
                        except Exception as exc:
                            self.stop(f"invalid_loop_candidates: {exc}")
                            continue
                    self.session_controller.complete_loop_observation(LoopObservation(observation, result.status))
                continue

            if task_type != "pipeline" or task_id != self._execution_task_id:
                continue
            self._execution_in_progress = False
            self._execution_task_id = None
            if error is not None:
                try:
                    if isinstance(error, ExecutionCancelled) or not self._run_is_current(run_token):
                        reason = error.reason if isinstance(error, ExecutionCancelled) else self.controller.stop_reason or "実行世代が変更されました"
                        try:
                            self.runtime_log.write("run_control", "inference_result_discarded", {"reason": reason, "operation": operation})
                        except Exception as logging_error:
                            self._set_status(f"停止結果の記録に失敗しました: {logging_error}")
                        if self.controller.is_running:
                            self._set_status("停止要求後の推論応答を破棄しました")
                    else:
                        try:
                            self.runtime_log.write("error", str(error), {"operation": operation})
                        except Exception as logging_error:
                            self._set_status(f"実行エラーの記録に失敗しました: {logging_error}")
                        self._show_player_state(PlayerState.WARNING)
                        messagebox.showerror("実行エラー" if operation == "execute" else "判断エラー", str(error))
                finally:
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

            if execution_records is not None:
                metrics = MetricsCalculator().calculate(execution_records)
                self.metrics.config(text=f"指標: total={metrics.total}, dry-run={metrics.dry_run}, executed={metrics.executed}, failed={metrics.failed}")

            if operation == "execute" and isinstance(result, ExecutionResult):
                self._set_status(f"実行: {result.action_id} / {result.mode} / {result.detail}")
                self.shell.set_latest_action(result.action_id, f"{result.mode} / {result.detail}")
                self._show_player_state(PlayerState.EVALUATE)
                self.runtime_log.write("execution", result.detail, {"action_id": result.action_id, "mode": result.mode})
            elif operation == "decision" and isinstance(result, ActionDecision):
                self._set_status(f"選択: {result.action_id} / {result.reason}")
                self.shell.set_latest_action(result.action_id, result.reason)
                self._show_player_state(PlayerState.EVALUATE)
                self.runtime_log.write("decision", result.reason, {"action_id": result.action_id, "provider": self.provider.get()})

            self._complete_session_step()
            if getattr(self, "_closing", False) and not self.session_controller.has_pending_step:
                self.root.destroy()
        self.root.after(BACKGROUND_RESULT_POLL_INTERVAL_MS, self._poll_background_results)

    # {
    #   責務: [
    #     start_loop: 対象を検証してから連続実行を開始する
    #   ]
    #   処理: [入力対象と設定を検証し、Session controllerへ連続実行を依頼する。準備・開始の失敗はPlayer Seatを要確認にしてダイアログで通知する]
    #   引数: []
    #   戻り値: []
    # }
    def start_loop(self) -> None:
        if self.session_controller.is_looping:
            return
        if self._execution_in_progress or self.session_controller.has_pending_step:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        try:
            self._validate_live_execution()
            prepared_step = self.workflow_service.prepare_step(
                self._application_settings(),
                self.obs.get("1.0", tk.END),
                self.actions.get("1.0", tk.END),
                "execute",
            )
            self._display_step_evaluation(prepared_step.evaluation)
        except ValueError as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("連続実行エラー", str(exc))
            self._set_status("連続実行を開始できません")
            return
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("連続実行エラー", str(exc))
            return

        self._session_dry_run = prepared_step.dry_run
        self._record_automated_cursor_position(None, False)
        self._last_cursor_position = self._cursor_position()
        try:
            self.session_controller.start_loop("execute", LOOP_STEP_DELAY_MS)
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_loop"})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("連続実行エラー", str(exc))

    # {
    #   責務: [start: 「再開」操作で実行世代を進め、停止状態を解除する]
    #   処理: [Session controllerの停止・保留状態を確認し、Sessionを再開して状態とログを更新する。成功時はPlayer Seatを待機中、失敗時は要確認にする]
    #   引数: []
    #   戻り値: []
    # }
    def start(self) -> None:
        if self.session_controller.has_pending_step:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return
        if self.session_controller.status is SessionStatus.RUNNING:
            self.session_controller.stop("rearmed")
        try:
            self.session_controller.start()
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": "start_session"})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("再開エラー", str(exc))
            return
        self.runtime_log.write("run_control", "started")
        self._set_status("実行可能")
        self._show_player_state(PlayerState.IDLE)

    # {
    #   責務: [stop: 停止要求を即時記録しloop予約と実入力許可を止める]
    #   処理: [Session controllerへ停止を要求し、理由を利用者へ表示する]
    #   引数: [reason: 停止を要求した要因]
    #   戻り値: []
    # }
    def stop(self, reason: str = "ユーザー停止") -> None:
        self.session_controller.stop(reason)
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
    #   処理: [重複stepを拒否し、観測・候補・設定を更新した後にSession controllerを呼び出す。準備の失敗はPlayer Seatを要確認にしてダイアログで通知する]
    #   引数: [command: executeなら安全評価後に実行し、decisionなら候補判断だけを行う]
    #   戻り値: [bool: step要求を受け付けた場合はTrue]
    # }
    def _run_session_step(self, command: str) -> bool:
        if self._is_looping():
            self._set_status("連続実行中は単独stepを開始できません")
            return False
        if self.session_controller.has_pending_step or self._execution_in_progress:
            self._set_status("停止要求を反映中です。前の推論応答を待っています")
            return False
        try:
            self._validate_live_execution()
            prepared_step = self.workflow_service.prepare_step(
                self._application_settings(),
                self.obs.get("1.0", tk.END),
                self.actions.get("1.0", tk.END),
                command,
            )
            self._display_step_evaluation(prepared_step.evaluation)
            self._session_dry_run = prepared_step.dry_run
            if self.session_controller.status is SessionStatus.RUNNING:
                self.session_controller.stop("new_step_configuration")
            self.session_controller.step(command)
            return True
        except Exception as exc:
            self.runtime_log.write("error", str(exc), {"operation": command})
            self._show_player_state(PlayerState.WARNING)
            messagebox.showerror("実行エラー" if command == "execute" else "判断エラー", str(exc))
            return False

    # {
    #   責務: [_application_settings: UI変数からstep開始時の設定snapshotを作る]
    #   処理: [各Tk variableとCaptureRegion入力を一度読み、workflow serviceへUI objectを渡さない]
    #   引数: []
    #   戻り値: [ApplicationSettings: 判断・実入力・capture mode/ROIを含むstep開始時の設定snapshot]
    # }
    def _application_settings(self) -> ApplicationSettings:
        return ApplicationSettings(
            provider=self.provider.get(),
            model=self.model.get(),
            endpoint=self.endpoint.get(),
            personality=self.personality.get(),
            purpose=self.purpose.get(),
            live_execution=self.live_execution.get(),
            input_mode=self.input_mode.get(),
            capture_region=self._capture_region_from_controls(),
        )

    # {
    #   責務: [_display_step_evaluation: workflow serviceの候補評価結果を評価欄に表示する]
    #   処理: [評価欄の旧JSONを置き換えて新しい候補理由を整形表示する]
    #   引数: [evaluation: 操作候補ごとの許可状態と理由]
    #   戻り値: []
    # }
    def _display_step_evaluation(self, evaluation: list[dict[str, object]]) -> None:
        self.evaluation.delete("1.0", tk.END)
        self.evaluation.insert("1.0", json.dumps(evaluation, ensure_ascii=False, indent=2))

    # {
    #   責務: [close: アプリ終了時にSessionと共有capture producerを停止し、実行中workerの終了を待つ]
    #   処理: [loopとcapture serviceを停止し、pending stepがあれば結果pollerに終了後のroot破棄を任せる]
    #   引数: []
    #   戻り値: []
    # }
    def close(self) -> None:
        self._closing = True
        self.session_controller.stop("application_closed")
        close_observation_service = getattr(self.observation_service, "close", None)
        if callable(close_observation_service) and close_observation_service() is False:
            self.runtime_log.write(
                "error",
                "画面capture producerは停止期限を超えたため、アプリ終了時点では未停止でした。",
                {"operation": "close_observation_service"},
            )
        if not self.session_controller.has_pending_step:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    Application(root)
    root.mainloop()


if __name__ == "__main__":
    main()
