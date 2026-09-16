import sys
from pathlib import Path


def _smoke_test() -> int:
    import tkinter as tk

    from ai_game_player.app import Application

    root = tk.Tk()
    root.withdraw()
    try:
        Application(root)
        root.update_idletasks()
    finally:
        root.destroy()
    return 0


def _fail_safe_watchdog() -> int:
    from ai_game_player.fail_safe_runtime import run_external_watchdog

    arguments = sys.argv[1:]
    index = arguments.index("--failsafe-watchdog")
    if index + 1 >= len(arguments):
        raise SystemExit("--failsafe-watchdog requires a state directory")
    return run_external_watchdog(Path(arguments[index + 1]))


def main() -> int:
    if "--failsafe-watchdog" in sys.argv[1:]:
        return _fail_safe_watchdog()
    if "--smoke-test" in sys.argv[1:]:
        return _smoke_test()

    from ai_game_player.app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
