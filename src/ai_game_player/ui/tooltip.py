"""Tooltip support for Tkinter widgets."""

import tkinter as tk
from typing import Optional


class ToolTip:
    """Attaches a hover tooltip to a Tkinter widget to improve UX and accessibility."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: Optional[str] = None
        self._tip_window: Optional[tk.Toplevel] = None
        self.widget.bind("<Enter>", self._schedule_tip, add="+")
        self.widget.bind("<Leave>", self._hide_tip, add="+")
        self.widget.bind("<ButtonPress>", self._hide_tip, add="+")

    def _schedule_tip(self, _event: Optional[tk.Event] = None) -> None:
        self._unschedule()
        self._after_id = self.widget.after(self.delay_ms, self._show_tip)

    def _unschedule(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show_tip(self) -> None:
        if self._tip_window or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 20
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return

        try:
            tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                tw,
                text=self.text,
                justify=tk.LEFT,
                background="#ffffe1",
                foreground="#000000",
                relief=tk.SOLID,
                borderwidth=1,
                font=("TkDefaultFont", 9, "normal"),
                padx=6,
                pady=3,
            )
            label.pack()
            self._tip_window = tw
        except Exception:
            self._tip_window = None

    def _hide_tip(self, _event: Optional[tk.Event] = None) -> None:
        self._unschedule()
        if self._tip_window:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None
