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


# {
#   責務: [_bundle_self_check: 配布bundleの自己検査を実行し、指定pathへJSON reportを書く]
#   処理: [--bundle-self-checkの次の引数をreport pathとして受け取り、GUIを起動せずに検査する]
#   引数: [なし（sys.argvを読む）] 戻り値: [int: 全項目合格なら0、不合格なら1]
#   エラー: [SystemExit: report pathが無い場合]
# }
def _bundle_self_check() -> int:
    from ai_game_player.bundle_self_check import run_bundle_self_check

    arguments = sys.argv[1:]
    index = arguments.index("--bundle-self-check")
    if index + 1 >= len(arguments):
        raise SystemExit("--bundle-self-check requires a report path")
    return run_bundle_self_check(Path(arguments[index + 1]))


# {
#   責務: [main: GUI・watchdog・制限planner worker・配布自己検査の専用入口を振り分ける]
#   処理: [workerではGUI import前に資源guardへ移り、通常起動経路を保持する]
#   引数: [なし] 戻り値: [int: 選択した入口の終了code]
# }
def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--planning-worker":
        from ai_game_player.planning_worker import main as planning_worker_main

        return planning_worker_main(sys.argv[2:])
    if "--failsafe-watchdog" in sys.argv[1:]:
        return _fail_safe_watchdog()
    if "--bundle-self-check" in sys.argv[1:]:
        return _bundle_self_check()
    if "--smoke-test" in sys.argv[1:]:
        return _smoke_test()

    from ai_game_player.app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
