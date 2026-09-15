import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ai_game_player.e2e_runner import ContinuousE2ERunner
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.window_selector import WindowsWindowSelector


TITLE = "Kadoka E2E Sample Game"


class WindowsSampleObservationSource:
    """Captures the real sample window and exposes only automatically detected interior UI candidates."""

    def __init__(self, window_handle: int) -> None:
        self.window_handle = window_handle
        self.capture = WindowsScreenCapture()
        self.analyzer = FrameAnalyzer()

    def read(self) -> tuple[ScreenObservation, list]:
        frame = self.capture.capture(self.window_handle)
        observation = self.analyzer.analyze(frame, "windows-e2e-sample")
        features = dict(observation.features)
        candidates = []
        for value in features.get("image_candidates", []):
            if not isinstance(value, dict):
                continue
            bbox = value.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            _, y, width, height = (int(part) for part in bbox)
            if y < 30 or width < 80 or height < 40:
                continue
            if width >= frame.width * 0.9 or height >= frame.height * 0.7:
                continue
            candidates.append(value)
        features["image_candidates"] = candidates
        return ScreenObservation(
            observation.screen_id,
            observation.width,
            observation.height,
            observation.ocr_text,
            features,
        ), []


def read_completed(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(value.get("completed")) if isinstance(value, dict) else False


def find_window(title: str, timeout_seconds: float = 15.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    selector = WindowsWindowSelector()
    while time.monotonic() < deadline:
        for window in selector.list_windows():
            if window.title == title:
                return window.handle
        time.sleep(0.1)
    raise RuntimeError(f"sample window not found: {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Kadoka closed-loop Windows E2E acceptance")
    parser.add_argument("--steps", type=int, default=24, help="sample milestone length")
    parser.add_argument("--duration", type=float, default=0.0, help="optional minimum run duration in seconds")
    parser.add_argument("--data-dir", type=Path, default=Path("build/e2e/game"))
    parser.add_argument("--log", type=Path, default=Path("build/e2e/e2e_trace.jsonl"))
    parser.add_argument("--state-file", type=Path, default=Path("build/e2e/sample_state.json"))
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows E2E acceptance requires Windows")
    if args.steps < 1:
        raise ValueError("steps must be positive")
    if args.duration < 0:
        raise ValueError("duration must not be negative")

    root = Path(__file__).resolve().parents[1]
    sample_script = root / "tools" / "e2e_sample_game.py"
    state_file = args.state_file.resolve()
    log_path = args.log.resolve()
    data_dir = args.data_dir.resolve()
    for path in (state_file, log_path):
        if path.exists():
            path.unlink()

    sample_steps = max(args.steps, 100000 if args.duration else args.steps)
    process = subprocess.Popen(
        [sys.executable, str(sample_script), "--state-file", str(state_file), "--steps", str(sample_steps), "--title", TITLE],
        cwd=root,
    )
    try:
        handle = find_window(TITLE)
        source = WindowsSampleObservationSource(handle)
        pipeline = DecisionPipeline(
            source,
            data_dir,
            RuleProvider(),
            dry_run=False,
            window_handle=handle,
            input_mode="mouse",
        )
        runner = ContinuousE2ERunner(
            pipeline,
            lambda: source.read()[0],
            log_path,
            milestone_probe=lambda: read_completed(state_file),
            max_steps=max(args.steps + 5, 100000 if args.duration else args.steps + 5),
            max_wall_seconds=max(120.0, args.duration + 60.0),
            minimum_duration_seconds=args.duration,
            settle_seconds=0.08,
            step_delay_seconds=0.02,
        )
        report = runner.run("advance the visible sample-game control until the milestone")
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if not report.success:
            return 1
        if not report.live_input_verified:
            print("E2E completed without verifying live Windows input", file=sys.stderr)
            return 2
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
