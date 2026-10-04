import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import TclError

from ai_game_player.ui.shell import (
    WORKSPACES,
    ApplicationShell,
    ShellController,
    ShellState,
    ShellStateStore,
)


class ShellStateTests(unittest.TestCase):
    def test_default_state_opens_all_shell_panels_on_play(self):
        self.assertEqual(ShellState(), ShellState("Play", True, True))

    def test_invalid_workspace_or_visibility_is_rejected(self):
        with self.assertRaises(ValueError):
            ShellState("Unknown")
        with self.assertRaises(ValueError):
            ShellState("Play", 1, True)

    def test_shell_layout_state_round_trips_without_runtime_config(self):
        expected = ShellState("Reasoning", False, True)
        with tempfile.TemporaryDirectory() as directory:
            store = ShellStateStore(Path(directory) / "data/shell_state.json")
            store.save(expected)
            self.assertEqual(store.load(), expected)
            self.assertEqual(set(store.path.parent.iterdir()), {store.path})

    def test_missing_malformed_and_legacy_state_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shell_state.json"
            store = ShellStateStore(path)
            self.assertEqual(store.load(), ShellState())
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(store.load(), ShellState())
            path.write_text('{"workspace":"not-a-workspace"}', encoding="utf-8")
            self.assertEqual(store.load(), ShellState())
            path.write_text('{"workspace":"History"}', encoding="utf-8")
            self.assertEqual(store.load(), ShellState("History", True, True))


class ShellControllerTests(unittest.TestCase):
    def test_all_workspace_slots_can_be_selected_and_state_persisted(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        for workspace in WORKSPACES:
            controller.select_workspace(workspace)
            self.assertEqual(controller.state.workspace, workspace)
        self.assertEqual(saved[-1], ShellState("Mods", True, True))
        with self.assertRaises(ValueError):
            controller.select_workspace("Runtime")

    def test_panel_toggle_reports_visibility_and_notifies_persistence(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        self.assertFalse(controller.toggle_inspector())
        self.assertFalse(controller.state.inspector_visible)
        self.assertFalse(controller.toggle_bottom_panel())
        self.assertFalse(controller.state.bottom_panel_visible)
        self.assertEqual(saved, [ShellState("Play", False, True), ShellState("Play", False, False)])
        self.assertTrue(controller.toggle_inspector())
        self.assertTrue(controller.toggle_bottom_panel())

    def test_selecting_current_workspace_does_not_rewrite_state(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        controller.select_workspace("Play")
        self.assertEqual(saved, [])


class TkShellSmokeTests(unittest.TestCase):
    def test_panes_workspace_mounts_and_global_stop_are_wired(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            changed = []
            stopped = []
            shell = ApplicationShell(root, ShellState(), lambda: stopped.append(True), changed.append)
            shell.pack(fill=tk.BOTH, expand=True)
            root.update_idletasks()

            self.assertEqual(tuple(shell.workspace_pages), WORKSPACES)
            self.assertEqual(shell.player_seat_host.winfo_manager(), "pack")
            self.assertEqual(shell.bottom_host.winfo_manager(), "pack")
            self.assertEqual(len(shell.upper.panes()), 3)
            self.assertEqual(len(shell.vertical.panes()), 2)

            shell.toggle_inspector()
            self.assertEqual(len(shell.upper.panes()), 2)
            shell.toggle_inspector()
            self.assertEqual(shell.upper.panes()[-1], str(shell.inspector))
            shell.toggle_bottom_panel()
            self.assertEqual(len(shell.vertical.panes()), 1)
            shell.toggle_bottom_panel()
            self.assertEqual(shell.vertical.panes()[-1], str(shell.bottom_panel))

            shell.select_workspace("History")
            shell.stop_button.invoke()
            self.assertEqual(shell.state.workspace, "History")
            self.assertEqual(changed[-1].workspace, "History")
            self.assertEqual(stopped, [True])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
