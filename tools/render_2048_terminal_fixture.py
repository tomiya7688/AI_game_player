"""固定版2048の終了画面をheadless Edgeで描画し、画素だけで再検査できる資料を保存する。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import zlib

from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.reference_game_2048_launcher import LoopbackStaticServer, find_browser_executable
from tools.reference_game_2048_source import PinnedGameSource


TERMINAL_BOARD = [[2, 4, 8, 16], [4, 8, 16, 32], [8, 16, 32, 64], [16, 32, 64, 2]]
RENDER_SCALES = (0.75, 1.0)
VIEWPORT = {"width": 1180, "height": 850}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=Path("build/e2e/terminal-render"))
    arguments = parser.parse_args()
    try:
        from PIL import Image
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        parser.error(f"終了画面の生成にはPillowとplaywrightが必要です。pip install Pillow playwrightを実行してください: {error}")
    source = PinnedGameSource(Path("build/reference_games/2048"))
    source_path = source.materialize()
    output = arguments.output_directory
    output.mkdir(parents=True, exist_ok=True)
    server = LoopbackStaticServer()
    url = server.start(source_path)
    # 保存状態を与えるのは再現用ブラウザだけ。認識器へDOM・保存状態を渡さない。
    state = {
        "grid": {
            "size": 4,
            "cells": [
                [{"position": {"x": x, "y": y}, "value": TERMINAL_BOARD[y][x]} for y in range(4)]
                for x in range(4)
            ],
        },
        "score": 1000,
        "over": True,
        "won": False,
        "keepPlaying": False,
    }
    failed = False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(find_browser_executable()), headless=True)
            try:
                for scale in RENDER_SCALES:
                    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=scale)
                    try:
                        page.add_init_script('localStorage.setItem("gameState", JSON.stringify(' + json.dumps(state) + '));')
                        page.goto(url)
                        page.locator(".game-message.game-over").wait_for()
                        # 固定版CSSのdelay 1200ms + fade-in 800msが完了するまで待つ。
                        page.wait_for_function("""() => {
                            const message = document.querySelector('.game-message.game-over');
                            return message && getComputedStyle(message).opacity === '1';
                        }""")
                        page.evaluate("document.fonts.ready")
                        png = page.screenshot()
                        image = Image.open(io.BytesIO(png)).convert("RGBA")
                        frame = ScreenFrame(image.width, image.height, image.tobytes("raw", "BGRA"))
                        adapted, candidates = ReferenceGame2048Adapter().adapt(
                            frame, ScreenObservation("rendered-2048-terminal", frame.width, frame.height)
                        )
                        game_state = adapted.features["game_2048"]
                        passed = game_state["board"] == TERMINAL_BOARD and game_state["game_over"] is True and not candidates
                        failed = failed or not passed
                        name = f"terminal-{scale:g}"
                        (output / f"{name}.png").write_bytes(png)
                        (output / f"{name}.bgra.zlib").write_bytes(zlib.compress(frame.bgra))
                        manifest = {
                            "source_commit": source.lock["commit"],
                            "browser_version": browser.version,
                            "capture_method": "playwright-headless-page-screenshot",
                            "viewport_css_pixels": VIEWPORT,
                            "device_scale_factor": scale,
                            "width": frame.width,
                            "height": frame.height,
                            "bgra_sha256": sha256(frame.bgra).hexdigest(),
                            "expected_board": TERMINAL_BOARD,
                            "expected_game_over": True,
                            "recognition": game_state,
                            "candidate_count": len(candidates),
                        }
                        (output / f"{name}.json").write_text(
                            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                        )
                        print(f"{name}: board_match={passed}, confidence={game_state['confidence']}")
                    finally:
                        page.close()
            finally:
                browser.close()
    finally:
        server.close()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
