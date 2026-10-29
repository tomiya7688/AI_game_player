## 2025-05-18 - ToolTip accessibility pattern in Tkinter Shell
**Learning:** Adding a lightweight `ToolTip` class bound to `<Enter>` and `<Leave>` with `add="+"` allows displaying contextual accessibility hints on Tkinter buttons without overriding preexisting Tkinter event bindings or interfering with layout packing.
**Action:** Reuse `ToolTip` from `ai_game_player.ui.shell` when adding hover hints to interactive Tkinter widgets in the app shell.
