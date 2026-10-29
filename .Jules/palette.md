## 2025-05-18 - Tkinter Widget ToolTip Pattern
**Learning:** Desktop Tkinter interfaces lack native hover tooltip hints, causing ambiguity for icon buttons and disabled toggle switches ("実入力を許可"). Lightweight delayed hover tooltips using `tk.Toplevel` (`wm_overrideredirect(True)`) provide accessible context without disrupting event propagation or focus states.
**Action:** Always wrap desktop toolbar actions and key control toggles with `ToolTip(widget, text)` when adding new Tkinter UI elements.
