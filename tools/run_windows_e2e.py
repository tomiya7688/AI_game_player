import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ai_game_player.e2e_runner import run_continuous_e2e
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.window_selector import WindowInfo, WindowsWindowSelector


TITLE = "Kadoka E2E Sample Game"


# {
#   責務: [WindowsSampleObservationSource: サンプルゲーム画面を観測し操作候補を抽出する]
#   フィールド: [window_handle: キャプチャ対象HWND, capture: Windows画面取得器, analyzer: フレーム解析器]
# }
class WindowsSampleObservationSource:
    """Captures the real sample window and exposes only automatically detected interior UI candidates."""

    # {
    #   責務: [__init__: 指定HWND用の画面取得・解析器を初期化する]
    #   処理: [対象handleとcapture/analyzerを保持する]
    #   引数: [window_handle: キャプチャする対象HWND]
    #   戻り値: []
    # }
    def __init__(self, window_handle: int) -> None:
        self.window_handle = window_handle
        self.capture = WindowsScreenCapture()
        self.analyzer = FrameAnalyzer()

    # {
    #   責務: [read: サンプル画面を取得し内部UI領域の候補を含む観測を返す]
    #   処理: [画面を解析し外枠や巨大領域の候補を除外する]
    #   引数: []
    #   戻り値: [tuple[ScreenObservation, list]: 観測と追加候補]
    # }
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


# {
#   責務: [read_completed: サンプルゲームの到達状態を読み取る]
#   処理: [JSONを安全に解析しcompleted値を返す]
#   引数: [path: 状態JSONのパス]
#   戻り値: [bool: completed状態]
# }
def read_completed(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(value.get("completed")) if isinstance(value, dict) else False


# {
#   責務: [find_window: タイトル一致するウィンドウのHWNDと所有PIDを取得する]
#   処理: [一定時間一覧を再取得し、タイトル一致したWindowInfoを返す]
#   引数: [title: 対象タイトル, timeout_seconds: 検索上限秒]
#   戻り値: [WindowInfo: HWNDと列挙時PIDを含む対象情報]
#   エラー: [期限内に対象が見つからない場合RuntimeError]
# }
def find_window(title: str, timeout_seconds: float = 15.0) -> WindowInfo:
    deadline = time.monotonic() + timeout_seconds
    selector = WindowsWindowSelector()
    while time.monotonic() < deadline:
        for window in selector.list_windows():
            if window.title == title:
                return window
        time.sleep(0.1)
    raise RuntimeError(f"sample window not found: {title}")


# {
#   責務: [main: Windows上で自動候補によるサンプルゲームE2Eを実行する]
#   処理: [サンプルを起動し、HWND/PIDを結び付けてlive閉ループを検証する]
#   引数: []
#   戻り値: [int: 成功・失敗を示す終了コード]
# }
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
    pipeline: DecisionPipeline | None = None
    try:
        window = find_window(TITLE)
        handle = window.handle
        source = WindowsSampleObservationSource(handle)
        pipeline = DecisionPipeline(
            source,
            data_dir,
            RuleProvider(),
            dry_run=False,
            window_handle=handle,
            window_process_id=window.process_id,
            input_mode="mouse",
        )
        # Live input starts SAFE_IDLE by design. E2E must model the user's explicit Start/re-arm action.
        pipeline.rearm_safety()
        report = run_continuous_e2e(
            pipeline,
            lambda: source.read()[0],
            log_path,
            milestone_probe=lambda: read_completed(state_file),
            max_steps=max(args.steps + 5, 100000 if args.duration else args.steps + 5),
            max_wall_seconds=max(120.0, args.duration + 60.0),
            minimum_duration_seconds=args.duration,
            settle_seconds=0.08,
            step_delay_seconds=0.02,
            purpose="advance the visible sample-game control until the milestone",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["success"]:
            return 1
        if not report["live_input_verified"]:
            print("E2E completed without verifying live Windows input", file=sys.stderr)
            return 2
        return 0
    finally:
        if pipeline is not None:
            pipeline.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
