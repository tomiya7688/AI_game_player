"""固定版CSSの発光・縮尺・overlayを画像回帰資料へ保存する（開発専用）。"""

import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import zlib

from tools.reference_game_2048_launcher import LoopbackStaticServer
from tools.reference_game_2048_source import PinnedGameSource

# 生成用の既知状態はfixture oracleだけ。認識器や判断器へ渡さない。
SCENARIOS = (
    (256, 0.75, 0, 0, False),
    (512, 1.0, 0, 3, False),
    (512, 1.25, 3, 3, False),
    (1024, 0.75, 3, 0, False),
    (2048, 1.0, 0, 0, False),
    (512, 1.0, 3, 0, True),
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path("build/reference-game"))
    parser.add_argument("--output", type=Path, default=Path("build/glow-fixtures"))
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args(argv)
    from PIL import Image
    from playwright.sync_api import sync_playwright

    source = PinnedGameSource(args.cache_root)
    server = LoopbackStaticServer()
    url = server.start(source.materialize())
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.chromium, headless=True, args=["--no-sandbox"]
            )
            try:
                for value, scale, row, column, over in SCENARIOS:
                    cells = [[2, 0, 4, 0], [0, 8, 0, 16], [32, 0, 64, 0], [0, 0, 4, 0]]
                    cells[row][column] = value
                    if over:
                        cells = [
                            [2, 4, 8, 16],
                            [4, 8, 16, 32],
                            [8, 16, 32, 64],
                            [value, 32, 64, 2],
                        ]
                    stored = {
                        "grid": {
                            "size": 4,
                            "cells": [
                                [
                                    {"position": {"x": x, "y": y}, "value": cells[y][x]}
                                    if cells[y][x]
                                    else None
                                    for y in range(4)
                                ]
                                for x in range(4)
                            ],
                        },
                        "score": 1824,
                        "over": over,
                        "won": False,
                        "keepPlaying": True,
                    }
                    page = browser.new_page(
                        viewport={"width": 650, "height": 850},
                        device_scale_factor=scale,
                    )
                    try:
                        page.add_init_script(
                            'localStorage.setItem("gameState", JSON.stringify('
                            + json.dumps(stored)
                            + "));"
                        )
                        page.goto(url)
                        page.evaluate("document.fonts.ready")
                        if over:
                            page.wait_for_function(
                                'getComputedStyle(document.querySelector(".game-message")).opacity === "1"'
                            )
                        else:
                            page.wait_for_timeout(300)
                        # DOM bounds are an independent generation oracle, never recognizer input.
                        bounds = page.locator(".game-container").bounding_box()
                        png = page.screenshot()
                        image = Image.open(BytesIO(png)).convert("RGBA")
                        pixels = image.tobytes("raw", "BGRA")
                        name = f"glow-{value}-{scale:g}-{row}-{column}" + (
                            "-overlay" if over else ""
                        )
                        (args.output / (name + ".bgra.zlib")).write_bytes(
                            zlib.compress(pixels, 9)
                        )
                        metadata = {
                            "source_commit": source.lock["commit"],
                            "browser_version": browser.version,
                            "width": image.width,
                            "height": image.height,
                            "scale": scale,
                            "bgra_sha256": sha256(pixels).hexdigest(),
                            "expected_board": cells,
                            "expected_board_bbox": [
                                round(bounds[k] * scale)
                                for k in ("x", "y", "width", "height")
                            ],
                            "overlay": over,
                            "oracle_scope": "fixture generation only; recognition receives BGRA pixels",
                        }
                        (args.output / (name + ".json")).write_text(
                            json.dumps(metadata, indent=2) + "\n"
                        )
                        print(name, metadata["expected_board_bbox"], flush=True)
                    finally:
                        page.close()
            finally:
                browser.close()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
