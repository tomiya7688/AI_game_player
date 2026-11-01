"""Opt-in Chromium screen/input validation; distinct from Windows field-play gates.

Run with the optional development packages Pillow and Playwright installed.
The simulator only validates predictions; it never chooses or sends an action.
"""

import argparse
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import threading
import time

from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import ReferenceGame2048TransitionModel
from ai_game_player.models import ScreenObservation
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.screen_capture import ScreenFrame
from tools.reference_game_2048_source import PinnedGameSource
from tools.run_2048_campaign import CyclingDirectionProvider
from tools.browser_probe_support import new_probe_directory, publish_validation_report


class BrowserScreenSource:
    def __init__(self, page, image_class, ocr=None):
        self.page = page
        self.image_class = image_class
        self.adapter = ReferenceGame2048Adapter()
        self.cached = None
        self.last_png = None
        self.ocr = ocr if ocr is not None else TesseractOcrRecognizer.optional()
        self.victory_observed = False

    def capture(self):
        self.last_png = self.page.screenshot()
        image = self.image_class.open(BytesIO(self.last_png)).convert("RGBA")
        frame = ScreenFrame(image.width, image.height, image.tobytes("raw", "BGRA"))
        observation = ScreenObservation(
            "chromium-2048",
            image.width,
            image.height,
            features={"signature": sha256(frame.bgra).hexdigest()},
        )
        self.cached = self.adapter.adapt(frame, observation)
        state = self.cached[0].features["game_2048"]
        board = state["board"]
        unresolved = not isinstance(board, list) or any(
            cell is None for row in board for cell in row
        )
        # OCR is needed at possible overlays/high tiles, not to read hidden game state.
        if self.ocr is not None and (
            unresolved
            or state["confidence"] < 0.65
            or any(cell >= 1024 for row in board for cell in row)
        ):
            words = [str(item["text"]) for item in self.ocr.recognize(frame)]
            observation = ScreenObservation(
                observation.screen_id,
                image.width,
                image.height,
                ocr_text=words,
                features=observation.features,
            )
            self.victory_observed = bool(
                re.search(r"\byou\s+win\b", " ".join(words), flags=re.IGNORECASE)
            )
            self.cached = self.adapter.adapt(frame, observation)
            state = self.cached[0].features["game_2048"]
            if self.victory_observed:
                self.cached = (self.cached[0], [])
                # An overlay can obscure the board. Do not manufacture its cells.
                if unresolved or state["confidence"] < 0.65:
                    return None
        if not isinstance(state["board"], list) or state["confidence"] < 0.65:
            raise RuntimeError(f"screen recognition failed: {state}")
        try:
            return Game2048State(
                state["board"], over=state["game_over"], won=self.victory_observed
            )
        except ValueError as error:
            raise RuntimeError(f"screen contains unresolved cells: {state}") from error

    def read(self):
        if self.cached is None:
            raise RuntimeError("capture a fresh screen before the decision")
        observation, candidates = self.cached
        self.cached = None
        return observation, candidates


class BrowserKeyExecutor:
    def __init__(self, page, target_url, before_dispatch):
        self.page = page
        self.target_url = target_url
        self.before_dispatch = before_dispatch

    def execute(self, candidate):
        keys = {
            "UP": "ArrowUp",
            "RIGHT": "ArrowRight",
            "DOWN": "ArrowDown",
            "LEFT": "ArrowLeft",
        }
        if (
            self.page.is_closed()
            or self.page.url != self.target_url
            or candidate.kind != "key"
            or candidate.label not in keys
        ):
            raise RuntimeError(
                "browser test accepts only the current game's arrow-key candidates"
            )
        self.before_dispatch()
        self.page.keyboard.press(keys[candidate.label])
        # Upstream's tile appearance animation lasts 200 ms. Observe after it,
        # without filling unknown cells or lowering recognition confidence.
        self.page.wait_for_timeout(350)
        return ExecutionResult(
            candidate.action_id, True, "browser_test", "arrow key sent to isolated page"
        )


def run_episode(browser, target_url, image_class, seed, max_steps, output):
    context = browser.new_context(
        viewport={"width": 650, "height": 850}, device_scale_factor=1
    )
    # Seed the test game's RNG, never expose it or DOM state to the decision provider.
    context.add_init_script(f"""let testSeed = {seed}; Math.random = () => {{
        testSeed = (1664525 * testSeed + 1013904223) >>> 0;
        return testSeed / 4294967296;
    }};""")
    page = context.new_page()
    page.goto(target_url)
    page.wait_for_timeout(300)
    source = BrowserScreenSource(page, image_class)
    model = ReferenceGame2048TransitionModel()
    steps = []
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(
            prefix="kadoka-browser-transition-"
        ) as directory:
            pipeline = DecisionPipeline(
                source, Path(directory), CyclingDirectionProvider(), dry_run=True
            )
            # Use the existing executor seam. Safety evaluation, guard, verifier and run generation remain active.
            pipeline.executor.close()
            generation = pipeline.controller.rearm_token
            pipeline.executor = ActionExecutor(
                dry_run=False,
                live_executor=BrowserKeyExecutor(
                    page,
                    target_url,
                    lambda: pipeline.controller.ensure_running(generation),
                ),
                run_controller_stop_checker=lambda: not pipeline.controller.is_running,
            )
            try:
                state = source.capture()
                for _ in range(max_steps):
                    if state is None or state.over or state.won:
                        break
                    before = state
                    result = pipeline.run_and_execute(
                        purpose="test the screen-based 2048 input loop"
                    )
                    if not result.executed:
                        raise RuntimeError(
                            "pipeline did not execute the selected action"
                        )
                    direction = result.action_id.removeprefix("2048-move-").upper()
                    slide = model.apply_slide(before, direction)
                    predicted = {
                        outcome.state.cells for outcome in model.spawn_outcomes(slide)
                    }
                    state = source.capture()
                    matched = state.cells in predicted if state is not None else None
                    steps.append(
                        {
                            "action": result.action_id,
                            "changed": state.cells != before.cells
                            if state is not None
                            else None,
                            "prediction_matched": matched,
                            "merge_score_delta": slide.reward_delta,
                            "max_tile": max(map(max, state.cells))
                            if state is not None
                            else None,
                            "victory_observed": source.victory_observed,
                        }
                    )
                    if matched is False:
                        output.mkdir(parents=True, exist_ok=True)
                        (output / f"mismatch-{seed}.png").write_bytes(source.last_png)
                        raise AssertionError(
                            f"screen prediction mismatch for seed {seed} step {len(steps)}"
                        )
                # Stop must prevent another input through the same pipeline.
                pipeline.controller.stop()
                try:
                    pipeline.run_and_execute(purpose="verify stopped input rejection")
                except RuntimeError:
                    stopped_input_rejected = True
                else:
                    raise AssertionError("stopped pipeline accepted an action")
            finally:
                pipeline.close()
        # Read a visible UI metric after play only; never supply it to the decision.
        visible_score = page.locator(".score-container").evaluate(
            "e => e.childNodes[0].textContent"
        )
        output.mkdir(parents=True, exist_ok=True)
        (output / f"final-{seed}.png").write_bytes(source.last_png)
        return {
            "seed": seed,
            "actions": len(steps),
            "duration_seconds": time.monotonic() - started,
            "game_over": state.over if state is not None else None,
            "game_won": source.victory_observed,
            "visible_score": visible_score,
            "max_tile": max(map(max, state.cells)) if state is not None else None,
            "stopped_input_rejected": stopped_input_rejected,
            "predictions_matched": all(
                step["prediction_matched"]
                for step in steps
                if step["prediction_matched"] is not None
            ),
            "terminal_board_unverified": state is None,
            "steps": steps,
        }
    finally:
        context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=300)
    parser.add_argument("--allow-live-input", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("build/transition-2048/screen")
    )
    args = parser.parse_args(argv)
    if not args.allow_live_input:
        parser.error("screen/input validation requires explicit --allow-live-input")
    if not 1 <= args.episodes <= 10 or not 1 <= args.max_steps <= 1000:
        parser.error("episodes must be 1..10 and max-steps must be 1..1000")
    from PIL import Image
    from playwright.sync_api import sync_playwright

    output = new_probe_directory(args.output)
    source = PinnedGameSource(Path("build/reference-game"))
    root = source.materialize()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root))
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target_url = f"http://127.0.0.1:{server.server_port}/index.html"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=args.chromium, headless=True, args=["--no-sandbox"]
        )
        try:
            episodes = []
            for index in range(args.episodes):
                result = run_episode(
                    browser, target_url, Image, 479 + index, args.max_steps, output
                )
                episodes.append(result)
                print(
                    json.dumps({k: v for k, v in result.items() if k != "steps"}),
                    flush=True,
                )
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
    report = {
        "schema_version": 1,
        "pinned_commit": source.lock["commit"],
        "input": "isolated Chromium keyboard via ActionExecutor seam",
        "observation": "screenshot BGRA -> existing board recognizer -> DecisionPipeline",
        "decision": "existing CyclingDirectionProvider; simulator predictions are validation-only",
        "windows_field_play": "Unverified: WGC/Win32 input/watchdog not exercised",
        "episodes": episodes,
    }
    report["evidence_directory"] = str(output)
    publish_validation_report(output / "report.json", report)
    publish_validation_report(args.output / "report.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
