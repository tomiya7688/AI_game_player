"""Probe the common screen/decision path on a pinned local browser game.

This is an optional developer probe, not a Windows acceptance gate or a
game-specific policy. Input is disabled unless --allow-live-input is supplied.
"""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time

from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.screen_capture import ScreenFrame
from tools.browser_probe_support import (
    export_game_assets,
    new_probe_directory,
    publish_validation_report,
)


class BrowserObservationSource:
    def __init__(self, page, image_class):
        self.page = page
        self.image_class = image_class
        self.analyzer = FrameAnalyzer(TesseractOcrRecognizer.optional())
        self.observations = []

    def read(self):
        image = self.image_class.open(BytesIO(self.page.screenshot())).convert("RGBA")
        observation = self.analyzer.analyze(
            ScreenFrame(image.width, image.height, image.tobytes("raw", "BGRA")),
            "browser-game",
        )
        self.observations.append(
            {
                "ocr": observation.ocr_text,
                "image_candidates": len(
                    observation.features.get("image_candidates", [])
                ),
                "signature": observation.features["signature"],
            }
        )
        return observation, []


class BrowserClickExecutor:
    def __init__(self, page, target_url, before_dispatch):
        self.page = page
        self.target_url = target_url
        self.actions = []
        self.before_dispatch = before_dispatch

    def execute(self, candidate):
        if self.page.is_closed() or self.page.url != self.target_url:
            raise RuntimeError("browser target changed")
        if candidate.kind != "click" or candidate.x is None or candidate.y is None:
            raise RuntimeError("this probe accepts only recognized click candidates")
        viewport = self.page.viewport_size
        if not (
            0 <= candidate.x < viewport["width"]
            and 0 <= candidate.y < viewport["height"]
        ):
            raise RuntimeError("candidate lies outside the browser viewport")
        self.before_dispatch()
        self.page.mouse.click(candidate.x, candidate.y)
        self.actions.append(
            {
                "id": candidate.action_id,
                "label": candidate.label,
                "kind": candidate.kind,
                "x": candidate.x,
                "y": candidate.y,
            }
        )
        self.page.wait_for_timeout(100)
        return ExecutionResult(
            candidate.action_id, True, "browser_test", "click in isolated target page"
        )


def verify_checkout(root, expected_commit):
    if len(expected_commit) != 40 or any(
        c not in "0123456789abcdef" for c in expected_commit
    ):
        raise ValueError("expected commit must be a full lowercase Git SHA")
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True, timeout=10
    )
    if head != expected_commit or dirty or not (root / "index.html").is_file():
        raise ValueError(
            "source must be a clean checkout at the expected commit with index.html"
        )


def probe(page, target_url, output, image_class, max_steps, allow_live_input):
    page.goto(target_url)
    page.wait_for_timeout(500)
    page.screenshot(path=str(output / "start.png"))
    source = BrowserObservationSource(page, image_class)
    started = time.monotonic()
    costs = []
    failure = None
    with tempfile.TemporaryDirectory(prefix="kadoka-generic-browser-") as directory:
        pipeline = DecisionPipeline(
            source, Path(directory), RuleProvider(), dry_run=True
        )
        generation = pipeline.controller.rearm_token
        executor = BrowserClickExecutor(
            page, target_url, lambda: pipeline.controller.ensure_running(generation)
        )
        pipeline.executor.close()
        pipeline.executor = ActionExecutor(
            dry_run=not allow_live_input,
            live_executor=executor,
            run_controller_stop_checker=lambda: not pipeline.controller.is_running,
        )
        try:
            for _ in range(max_steps):
                step_started = time.perf_counter()
                try:
                    result = pipeline.run_and_execute(
                        purpose="Play the game and make progress"
                    )
                except Exception as error:
                    failure = f"{type(error).__name__}: {error}"
                    break
                costs.append((time.perf_counter() - step_started) * 1000)
                if not result.executed:
                    break
            pipeline.controller.stop()
        finally:
            pipeline.close()
    page.screenshot(path=str(output / "final.png"))
    return {
        "provider": "unmodified RuleProvider",
        "recognition": "FrameAnalyzer + optional Tesseract OCR; no game adapter or manual candidates",
        "input": "isolated Chromium mouse through ActionExecutor"
        if allow_live_input
        else "dry_run",
        "actions": executor.actions,
        "observations": source.observations,
        "duration_seconds": time.monotonic() - started,
        "latency_ms": {
            "p50": statistics.median(costs) if costs else None,
            "max": max(costs) if costs else None,
        },
        "failure": failure,
        "windows_field_play": "Unverified",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--width", type=int, default=650)
    parser.add_argument("--height", type=int, default=850)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--allow-live-input", action="store_true")
    args = parser.parse_args(argv)
    if (
        not 1 <= args.max_steps <= 300
        or not 100 <= args.width <= 1920
        or not 100 <= args.height <= 1080
    ):
        parser.error("steps must be 1..300 and viewport must be 100..1920 by 100..1080")
    root = args.source.resolve()
    verify_checkout(root, args.expected_commit)
    output = new_probe_directory(args.output)
    from PIL import Image
    from playwright.sync_api import sync_playwright

    document_root = tempfile.TemporaryDirectory(prefix="kadoka-game-assets-")
    try:
        export_game_assets(root, args.expected_commit, Path(document_root.name))
    except BaseException:
        document_root.cleanup()
        raise
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(SimpleHTTPRequestHandler, directory=document_root.name),
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    target_url = f"http://127.0.0.1:{server.server_port}/index.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.chromium, headless=True, args=["--no-sandbox"]
            )
            try:
                page = browser.new_page(
                    viewport={"width": args.width, "height": args.height}
                )
                report = probe(
                    page,
                    target_url,
                    output,
                    Image,
                    args.max_steps,
                    args.allow_live_input,
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        document_root.cleanup()
    report.update({"schema_version": 1, "source_commit": args.expected_commit})
    report["evidence_directory"] = str(output)
    publish_validation_report(output / "report.json", report)
    publish_validation_report(args.output / "report.json", report)
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in ("actions", "observations")
            },
            indent=2,
        )
    )
    return 1 if report["failure"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
