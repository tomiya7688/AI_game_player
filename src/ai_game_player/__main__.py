import sys


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


def main() -> int:
    if "--smoke-test" in sys.argv[1:]:
        return _smoke_test()

    from ai_game_player.app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
