"""IDE-style application shell. This module contains presentation only."""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import ttk

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
        if not isinstance(self.inspector_visible, bool) or not isinstance(self.bottom_panel_visible, bool):
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
            json.dumps({
                "workspace": state.workspace,
                "inspector_visible": state.inspector_visible,
                "bottom_panel_visible": state.bottom_panel_visible,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class ShellController:
    """Tracks shell selection and panel visibility without owning game behavior."""

    def __init__(self, state: ShellState, on_change: Callable[[ShellState], None] | None = None) -> None:
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


class ApplicationShell(ttk.Frame):
    """Resizable IDE layout with replaceable, independent workspace mount points."""

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
        ttk.Label(command, text="Kadoka AI Game Player", font=("TkDefaultFont", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(command, text="Inspectorを表示/非表示", command=self.toggle_inspector).pack(side=tk.RIGHT)
        ttk.Button(command, text="Bottom Panelを表示/非表示", command=self.toggle_bottom_panel).pack(side=tk.RIGHT, padx=6)
        self.stop_button = ttk.Button(command, text="■ 停止", command=on_stop)
        self.stop_button.pack(side=tk.RIGHT, padx=10)
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
                ttk.Label(page, text=workspace, font=("TkDefaultFont", 14, "bold")).pack(anchor=tk.W, pady=(8, 4))
                ttk.Label(page, text=WORKSPACE_HINTS[workspace], wraplength=600, justify=tk.LEFT).pack(anchor=tk.W)

        self.inspector = ttk.Frame(self.upper, padding=8, width=220)
        ttk.Label(self.inspector, text="Inspector", font=("TkDefaultFont", 11, "bold")).pack(anchor=tk.W)
        ttk.Label(self.inspector, text="選択した項目の詳細がここに表示されます。", wraplength=200, justify=tk.LEFT).pack(anchor=tk.W, pady=8)
        self.inspector_content = ttk.Frame(self.inspector)
        self.inspector_content.pack(fill=tk.BOTH, expand=True)
        self.upper.add(self.navigator, weight=0)
        self.upper.add(self.main_view, weight=1)
        if state.inspector_visible:
            self.upper.add(self.inspector, weight=0)

        self.bottom_panel = ttk.Frame(self.vertical, padding=8, height=120)
        ttk.Label(self.bottom_panel, text="Bottom Panel — Timeline / Console / Trace").pack(anchor=tk.W)
        ttk.Label(self.bottom_panel, text="実行記録や診断表示を接続する領域です。", wraplength=900, justify=tk.LEFT).pack(anchor=tk.W, pady=6)
        self.bottom_host = ttk.Frame(self.bottom_panel)
        self.bottom_host.pack(fill=tk.BOTH, expand=True)
        if state.bottom_panel_visible:
            self.vertical.add(self.bottom_panel, weight=1)

        self.player_seat_host = ttk.Frame(self.workspace_pages["Play"])
        self.player_seat_host.pack(fill=tk.X)
        self.select_workspace(state.workspace, persist=False)

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

    def select_workspace(self, workspace: str, persist: bool = True) -> None:
        if workspace not in self.workspace_pages:
            raise ValueError(f"Unknown workspace: {workspace}")
        self.workspace_var.set(workspace)
        self.workspace_pages[workspace].tkraise()
        if persist:
            self.controller.select_workspace(workspace)

    def toggle_inspector(self) -> None:
        if self.controller.toggle_inspector():
            self.upper.insert(2, self.inspector, weight=0)
        else:
            self.upper.forget(self.inspector)

    def toggle_bottom_panel(self) -> None:
        if self.controller.toggle_bottom_panel():
            self.vertical.insert(1, self.bottom_panel, weight=1)
        else:
            self.vertical.forget(self.bottom_panel)
