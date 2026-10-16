import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ai_game_player.e2e_runner import run_continuous_e2e
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.window_selector import WindowInfo, WindowsWindowSelector


TITLE = "Kadoka E2E Sample Game"
SAMPLE_CONTROL_CONFIDENCE = 1.0
SAMPLE_CONTROL_ASPECT_RATIO_RELATIVE_TOLERANCE = 0.12


# {
#   責務: [WindowsSampleObservationSource: サンプルゲーム画面を観測し操作候補を抽出する]
#   フィールド: [window_handle: キャプチャ対象HWND, capture: Windows画面取得器, analyzer: フレーム解析器]
# }
class WindowsSampleObservationSource:
    """Capture the sample UI and confirm a detected region with the sample game's active-control metadata."""

    # {
    #   責務: [__init__: 指定HWND用の画面取得・解析器とsample stateを接続する]
    #   処理: [対象handle・active controlを読むstate path・capture/analyzerを保持する]
    #   引数: [window_handle: キャプチャする対象HWND, state_file: sample gameが宣言する操作対象JSON]
    #   戻り値: []
    # }
    def __init__(self, window_handle: int, state_file: Path) -> None:
        self.window_handle = window_handle
        self.state_file = state_file
        self.capture = WindowsScreenCapture()
        self.analyzer = FrameAnalyzer()

    # {
    #   責務: [read: sample gameが意味を宣言し、画面検出も一致する操作候補を返す]
    #   処理: [画面を取得し、active controlと縦横比が一致する検出領域を一意に選びsample metadataで確認する]
    #   引数: []
    #   戻り値: [tuple[ScreenObservation, list[ActionCandidate]]: 観測と意味が確認済みのsample操作候補]
    # }
    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        frame = self.capture.capture(self.window_handle)
        observation = self.analyzer.analyze(frame, "windows-e2e-sample")
        features = dict(observation.features)
        image_candidates = []
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
            image_candidates.append(value)
        features["image_candidates"] = image_candidates
        active_control = read_active_control(self.state_file)
        candidate = match_sample_control_to_image(active_control, image_candidates)
        return ScreenObservation(
            observation.screen_id,
            observation.width,
            observation.height,
            observation.ocr_text,
            features,
        ), [candidate] if candidate is not None else []


def read_active_control(path: Path) -> dict[str, object] | None:
    """Read the current control meaning declared by the deterministic sample game."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"sample game state could not be read: {path}") from error
    if not isinstance(state, dict):
        raise RuntimeError("sample game state must be a JSON object")
    if state.get("completed") is True:
        return None
    control = state.get("active_control")
    if not isinstance(control, dict):
        raise RuntimeError("sample game state has no active control description")
    action_id = control.get("action_id")
    label = control.get("label")
    bbox_size = control.get("bbox_size")
    if (
        not isinstance(action_id, str)
        or not action_id.strip()
        or not isinstance(label, str)
        or not label.strip()
        or not isinstance(bbox_size, list)
        or len(bbox_size) != 2
        or any(type(value) is not int or value <= 0 for value in bbox_size)
    ):
        raise RuntimeError("sample game active control has invalid identity, label, or dimensions")
    return {"action_id": action_id, "label": label, "bbox_size": tuple(bbox_size)}


def match_sample_control_to_image(
    active_control: dict[str, object] | None,
    image_candidates: object,
) -> ActionCandidate | None:
    """Promote one detected rectangle only when sample semantics confirm that control."""
    if active_control is None or not isinstance(image_candidates, list):
        return None
    bbox_size = active_control.get("bbox_size")
    if not isinstance(bbox_size, tuple) or len(bbox_size) != 2:
        return None
    expected_width, expected_height = bbox_size
    matching_candidates: list[dict[str, object]] = []
    for value in image_candidates:
        if not isinstance(value, dict):
            continue
        bbox = value.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            actual_width, actual_height = int(bbox[2]), int(bbox[3])
        except (TypeError, ValueError):
            continue
        width_scale = actual_width / expected_width
        height_scale = actual_height / expected_height
        scale_difference = abs(width_scale - height_scale)
        largest_scale = max(width_scale, height_scale)
        if largest_scale <= 0:
            continue
        if scale_difference <= largest_scale * SAMPLE_CONTROL_ASPECT_RATIO_RELATIVE_TOLERANCE:
            matching_candidates.append(value)
    if len(matching_candidates) != 1:
        return None

    match = matching_candidates[0]
    try:
        action_id = str(active_control["action_id"])
        label = str(active_control["label"])
        x = int(match["x"])
        y = int(match["y"])
        raw_bbox = match["bbox"]
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            return None
        bbox = tuple(int(value) for value in raw_bbox)
    except (KeyError, TypeError, ValueError):
        return None
    return ActionCandidate(
        action_id,
        "click",
        label,
        x,
        y,
        SAMPLE_CONTROL_CONFIDENCE,
        bbox=bbox,
    )


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
#   責務: [find_window: タイトルとsample state fileを確認して対象HWNDと所有PIDを取得する]
#   処理: [一定時間window一覧を再取得し、タイトル一致とstate file作成の両方を待つ]
#   引数: [title: 対象タイトル, state_file: sample UI stateのパス, timeout_seconds: 検索上限秒]
#   戻り値: [WindowInfo: HWNDと列挙時PIDを含む対象情報]
#   エラー: [期限内に対象が見つからない場合RuntimeError]
# }
def find_window(title: str, state_file: Path, timeout_seconds: float = 15.0) -> WindowInfo:
    deadline = time.monotonic() + timeout_seconds
    selector = WindowsWindowSelector()
    while time.monotonic() < deadline:
        for window in selector.list_windows():
            if window.title == title and state_file.is_file():
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
        window = find_window(TITLE, state_file)
        handle = window.handle
        source = WindowsSampleObservationSource(handle, state_file)
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
