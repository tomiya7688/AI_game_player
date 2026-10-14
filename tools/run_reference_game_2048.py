"""Open the pinned 2048 reference game for a Windows E2E test session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.reference_game_2048_launcher import ReferenceGame2048Launcher
from tools.reference_game_2048_source import SOURCE_LOCK_PATH


def main() -> int:
    parser = argparse.ArgumentParser(
        description="固定版MIT 2048を専用Windowsブラウザーwindowで起動します"
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("build/reference_games/2048"),
        help="検証済みsourceを次回以降も利用するcache directory",
    )
    parser.add_argument("--browser", type=Path, help="省略時はEdge、次にChromeを自動検索します")
    arguments = parser.parse_args()

    try:
        source_lock = json.loads(SOURCE_LOCK_PATH.read_text(encoding="utf-8"))
        with ReferenceGame2048Launcher(arguments.cache_dir.resolve(), arguments.browser) as launcher:
            launcher.start()
            print(
                json.dumps(
                    {
                        "game": "2048",
                        "source_commit": source_lock["commit"],
                        "source_tree": source_lock["git_tree_sha"],
                        "license": source_lock["license"]["spdx_id"],
                        "url": launcher.url,
                        "source": str(launcher.source_path),
                        "window_title": launcher.window_info.title,
                        "window_handle": f"0x{launcher.window_info.handle:X}",
                        "process_id": launcher.window_info.process_id,
                        "window_is_foreground": launcher.window_is_foreground,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print("ゲームウィンドウを閉じるか Ctrl+C を押すと、ブラウザーとローカルサーバーを終了します。")
            return launcher.wait_until_closed()
    except KeyboardInterrupt:
        print("利用者が2048ランチャーを停止しました。")
        return 130
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"2048ランチャーを起動できませんでした: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
