"""IDE-style application shell. This module contains presentation only."""

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import ttk

from ai_game_player.ui.tooltip import ToolTip

WORKSPACES = ("Play", "Vision", "Reasoning", "Evaluation", "History", "Mods")
WORKSPACE_HINTS = {
    "Vision": "画面認識と候補の表示領域です。Workspaceの内容は個別機能から接続します。",
    "Reasoning": "判断過程の表示領域です。",
    "Evaluation": "結果と評価の表示領域です。",
    "History": "実行履歴の表示領域です。",
    "Mods": "拡張機能の表示領域です。",
}


@dataclass(frozen=True)
class ShellState:
    """Small user-facing layout state; unrelated runtime settings stay elsewhere."""

    workspace: str = "Play"
    inspector_visible: bool = True
    bottom_panel_visible: bool = True

    def __post_init__(self) -> None:
        if self.workspace not in WORKSPACES:
            raise ValueError(f"Unknown workspace: {self.workspace}")
        if not isinstance(self.inspector_visible, bool) or not isinstance(
            self.bottom_panel_visible, bool
        ):
            raise ValueError("Panel visibility settings must be booleans")


class ShellStateStore:
    """Persist shell navigation and panel visibility independently from game settings."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ShellState:
        if not self.path.exists():
            return ShellState()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return ShellState()
            return ShellState(
                workspace=value.get("workspace", "Play"),
                inspector_visible=value.get("inspector_visible", True),
                bottom_panel_visible=value.get("bottom_panel_visible", True),
            )
        except (OSError, ValueError, TypeError):
            return ShellState()

    def save(self, state: ShellState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "workspace": state.workspace,
                    "inspector_visible": state.inspector_visible,
                    "bottom_panel_visible": state.bottom_panel_visible,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class ShellController:
    """Tracks shell selection and panel visibility without owning game behavior."""

    def __init__(
        self, state: ShellState, on_change: Callable[[ShellState], None] | None = None
    ) -> None:
        self.state = state
        self.on_change = on_change or (lambda _state: None)

    def select_workspace(self, workspace: str) -> None:
        next_state = replace(self.state, workspace=workspace)
        if next_state != self.state:
            self._change(next_state)

    def toggle_inspector(self) -> bool:
        visible = not self.state.inspector_visible
        self._change(replace(self.state, inspector_visible=visible))
        return visible

    def toggle_bottom_panel(self) -> bool:
        visible = not self.state.bottom_panel_visible
        self._change(replace(self.state, bottom_panel_visible=visible))
        return visible

    def _change(self, state: ShellState) -> None:
        self.state = state
        self.on_change(state)


# {
#   責務: [ApplicationShell: 共通Workspace配置、全画面共通の停止操作、現在のSession状態、およびPlay Workspaceの直近Actionを表示する]
#   フィールド: [
#     controller: 現在のWorkspaceと各Panelの表示状態を保持するShellController,
#     workspace_pages: Workspace名から表示領域への対応,
#     inspector_toggle_button: Inspectorを次に表示するか隠すかを案内するボタン,
#     bottom_panel_toggle_button: Bottom Panelを次に表示するか隠すかを案内するボタン,
#     status_var: 現在のSession状態を表示する文字列変数,
#     latest_action_var: 最後に選択または実行したActionを表示する文字列変数
#   ]
# }
class ApplicationShell(ttk.Frame):
    """Resizable IDE layout with replaceable, independent workspace mount points."""

    # {
    #   責務: [__init__: ApplicationShellの共通ヘッダー、Workspace領域、Inspector、Bottom Panel、および停止ボタンを構築・配置する]
    #   処理: [
    #     1: ShellControllerとUI変数を初期化する
    #     2: アプリケーションタイトルとPanel切替ボタン、停止ボタンを配置する
    #     3: ヘッダー3ボタンへ補足説明を接続し、停止案内ではEscのフォーカス条件とWindowsのF12・連続実行中の手動マウス移動を説明する
    #     4: 左右・上下のPanedWindowにNavigator、Workspace表示領域、Inspector、Bottom Panelを配置する
    #     5: 直近Action表示枠とPlay Workspaceホスト領域を生成し、初期Workspaceと保存済みPanel状態に対応する切替ラベルを表示する
    #   ]
    #   引数: [
    #     self: 構築したUI部品とcontrollerを保持するApplicationShell
    #     master: 親となるTkinterウィジェット
    #     state: 初期表示のWorkspaceおよびPanel表示状態
    #     on_stop: 停止ボタン押下時に呼び出すコールバック関数
    #     on_state_change: Shell表示状態変更時に呼び出すコールバック関数
    #   ]
    #   戻り値: [None: Shellを親ウィジェット内に構築し、初期状態に対応するPanel配置と操作ラベルを設定する]
    # }
    def __init__(
        self,
        master: tk.Misc,
        state: ShellState,
        on_stop: Callable[[], None],
        on_state_change: Callable[[ShellState], None],
    ) -> None:
        super().__init__(master)
        self.controller = ShellController(state, on_state_change)
        self.workspace_var = tk.StringVar(master=self, value=state.workspace)
        self.workspace_buttons: dict[str, ttk.Radiobutton] = {}
        self.workspace_pages: dict[str, ttk.Frame] = {}

        command = ttk.Frame(self, padding=(8, 6))
        command.pack(fill=tk.X)
        ttk.Label(
            command, text="Kadoka AI Game Player", font=("TkDefaultFont", 11, "bold")
        ).pack(side=tk.LEFT)
        self.inspector_toggle_button = ttk.Button(
            command, text="", command=self.toggle_inspector
        )
        self.inspector_toggle_button.pack(side=tk.RIGHT)
        self.bottom_panel_toggle_button = ttk.Button(
            command, text="", command=self.toggle_bottom_panel
        )
        self.bottom_panel_toggle_button.pack(side=tk.RIGHT, padx=6)
        ToolTip(
            self.inspector_toggle_button, "右側のInspectorを表示または非表示にします"
        )
        ToolTip(
            self.bottom_panel_toggle_button,
            "下部のBottom Panelを表示または非表示にします",
        )
        stop_button_label = "■ 停止 (Esc / F12)" if os.name == "nt" else "■ 停止 (Esc)"
        self.stop_button = ttk.Button(command, text=stop_button_label, command=on_stop)
        self.stop_button.pack(side=tk.RIGHT, padx=10)
        stop_help_text = (
            "停止ボタン、または本アプリにフォーカスがある状態のEscで停止を要求します。"
            "WindowsではF12でも停止を要求できます。"
            "Windowsの連続実行中は、自動操作以外のマウス移動を検出すると停止を要求します"
            if os.name == "nt"
            else "停止ボタン、または本アプリにフォーカスがある状態のEscで停止を要求します"
        )
        ToolTip(self.stop_button, stop_help_text)
        self.status_var = tk.StringVar(master=self, value="待機中")
        ttk.Label(command, textvariable=self.status_var).pack(side=tk.RIGHT, padx=8)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X)

        self.vertical = ttk.Panedwindow(self, orient=tk.VERTICAL)
        self.vertical.pack(fill=tk.BOTH, expand=True)
        self.upper = ttk.Panedwindow(self.vertical, orient=tk.HORIZONTAL)
        self.vertical.add(self.upper, weight=5)

        self.navigator = ttk.Frame(self.upper, padding=8)
        ttk.Label(self.navigator, text="Navigator").pack(anchor=tk.W, pady=(0, 8))
        for workspace in WORKSPACES:
            button = ttk.Radiobutton(
                self.navigator,
                text=workspace,
                variable=self.workspace_var,
                value=workspace,
                command=lambda item=workspace: self.select_workspace(item),
            )
            button.pack(fill=tk.X, pady=2)
            self.workspace_buttons[workspace] = button

        self.main_view = ttk.Frame(self.upper, padding=8)
        for workspace in WORKSPACES:
            page = ttk.Frame(self.main_view)
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.workspace_pages[workspace] = page
            if workspace != "Play":
                ttk.Label(
                    page, text=workspace, font=("TkDefaultFont", 14, "bold")
                ).pack(anchor=tk.W, pady=(8, 4))
                ttk.Label(
                    page,
                    text=WORKSPACE_HINTS[workspace],
                    wraplength=600,
                    justify=tk.LEFT,
                ).pack(anchor=tk.W)

        self.inspector = ttk.Frame(self.upper, padding=8, width=220)
        ttk.Label(
            self.inspector, text="Inspector", font=("TkDefaultFont", 11, "bold")
        ).pack(anchor=tk.W)
        ttk.Label(
            self.inspector,
            text="選択した項目の詳細がここに表示されます。",
            wraplength=200,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=8)
        self.inspector_content = ttk.Frame(self.inspector)
        self.inspector_content.pack(fill=tk.BOTH, expand=True)
        self.upper.add(self.navigator, weight=0)
        self.upper.add(self.main_view, weight=1)
        if state.inspector_visible:
            self.upper.add(self.inspector, weight=0)

        self.bottom_panel = ttk.Frame(self.vertical, padding=8, height=120)
        ttk.Label(
            self.bottom_panel, text="Bottom Panel — Timeline / Console / Trace"
        ).pack(anchor=tk.W)
        ttk.Label(
            self.bottom_panel,
            text="実行記録や診断表示を接続する領域です。",
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=6)
        self.bottom_host = ttk.Frame(self.bottom_panel)
        self.bottom_host.pack(fill=tk.BOTH, expand=True)
        if state.bottom_panel_visible:
            self.vertical.add(self.bottom_panel, weight=1)

        self.latest_action_var = tk.StringVar(
            master=self, value="直近Action: まだありません"
        )
        self.latest_action_label = ttk.Label(
            self.workspace_pages["Play"],
            textvariable=self.latest_action_var,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=700,
        )
        self.latest_action_label.pack(fill=tk.X, pady=(0, 6))
        self.player_seat_host = ttk.Frame(self.workspace_pages["Play"])
        self.player_seat_host.pack(fill=tk.X)
        self.select_workspace(state.workspace, persist=False)
        self._update_panel_toggle_labels()

    @property
    def state(self) -> ShellState:
        return self.controller.state

    @property
    def play_host(self) -> ttk.Frame:
        return self.workspace_pages["Play"]

    @property
    def inspector_host(self) -> ttk.Frame:
        return self.inspector_content

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    # {
    #   責務: [set_latest_action: 一時的なSession状態とは別に最後の判断または実行Actionを保持する]
    #   処理: [Action IDと判断理由または実行結果をPlay Workspace上部へ表示する]
    #   引数: [action_id: 判断または実行された操作候補の識別子, detail: 選択理由または実行結果]
    #   戻り値: []
    # }
    def set_latest_action(self, action_id: str, detail: str = "") -> None:
        displayed_action_id = action_id.strip() or "不明"
        displayed_detail = detail.strip()
        summary = f"直近Action: {displayed_action_id}"
        if displayed_detail:
            summary = f"{summary} / {displayed_detail}"
        self.latest_action_var.set(summary)

    def select_workspace(self, workspace: str, persist: bool = True) -> None:
        if workspace not in self.workspace_pages:
            raise ValueError(f"Unknown workspace: {workspace}")
        self.workspace_var.set(workspace)
        self.workspace_pages[workspace].tkraise()
        if persist:
            self.controller.select_workspace(workspace)

    # {
    #   責務: [_update_panel_toggle_labels: InspectorとBottom Panelの切替ボタンに、次のクリックで行う操作を表示する]
    #   処理: [controllerが保持する各Panelの表示状態を読み、表示中は「を非表示」、非表示中は「を表示」をボタンに設定する]
    #   引数: [self: controllerと二つの切替ボタンの構築が済んだApplicationShell]
    #   戻り値: [None: ボタンの文字だけを更新し、Panel状態の変更や保存コールバックの呼び出しは行わない]
    # }
    def _update_panel_toggle_labels(self) -> None:
        inspector_button_text = (
            "Inspectorを非表示" if self.state.inspector_visible else "Inspectorを表示"
        )
        bottom_panel_button_text = (
            "Bottom Panelを非表示"
            if self.state.bottom_panel_visible
            else "Bottom Panelを表示"
        )
        self.inspector_toggle_button.config(text=inspector_button_text)
        self.bottom_panel_toggle_button.config(text=bottom_panel_button_text)

    # {
    #   責務: [toggle_inspector: Inspector paneの表示状態を切り替える]
    #   処理: [controllerの表示状態を切り替え、表示時は右端へ追加し、非表示時はPaneから外す。最後にボタンへ次の操作を表示する]
    #   引数: [self: InspectorとPanel切替ボタンを構築済みのApplicationShell]
    #   戻り値: [None: 状態変更はcontrollerの保存コールバックへ通知し、Pane配置とラベルを更新する]
    # }
    def toggle_inspector(self) -> None:
        if self.controller.toggle_inspector():
            self.upper.add(self.inspector, weight=0)
        else:
            self.upper.forget(self.inspector)
        self._update_panel_toggle_labels()

    # {
    #   責務: [toggle_bottom_panel: 下部のBottom Panelを表示または非表示へ切り替える]
    #   処理: [controllerの表示状態を切り替え、表示時は下端へ追加し、非表示時はPaneから外す。最後にボタンへ次の操作を表示する]
    #   引数: [self: Bottom PanelとPanel切替ボタンを構築済みのApplicationShell]
    #   戻り値: [None: 状態変更はcontrollerの保存コールバックへ通知し、Pane配置とラベルを更新する]
    # }
    def toggle_bottom_panel(self) -> None:
        if self.controller.toggle_bottom_panel():
            self.vertical.add(self.bottom_panel, weight=1)
        else:
            self.vertical.forget(self.bottom_panel)
        self._update_panel_toggle_labels()
