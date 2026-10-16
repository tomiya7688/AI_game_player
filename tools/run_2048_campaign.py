"""Run a bounded live-input endurance campaign against the pinned 2048 build."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from ai_game_player.campaign_runner import (
    PR_CAMPAIGN_DURATION_SECONDS,
    RELEASE_CAMPAIGN_DURATION_SECONDS,
    CampaignActionResult,
    CampaignConfig,
    CampaignEpisode,
    CampaignFailure,
    CampaignRunner,
    CampaignState,
)
from ai_game_player.decision_context import DecisionContext
from ai_game_player.fail_safe_runtime import FailSafeConfig
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.screen_capture import WindowsScreenCapture
from tools.reference_game_2048_launcher import ReferenceGame2048Launcher
from tools.reference_game_2048_source import SOURCE_LOCK_PATH


SCREEN_READY_TIMEOUT_SECONDS = 45.0
SCREEN_READY_POLL_SECONDS = 0.5
CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS = 5.0
CAMPAIGN_PURPOSE = "perform live arrow-key actions and measure the 2048 screen loop"
DIRECTION_ACTION_IDS = (
    "2048-move-up",
    "2048-move-down",
    "2048-move-left",
    "2048-move-right",
)


class Cached2048ObservationSource:
    def __init__(self, window_handle: int, ocr_recognizer: object | None) -> None:
        self.window_handle = window_handle
        self.capture = WindowsScreenCapture()
        self.analyzer = FrameAnalyzer(ocr_recognizer)
        self.adapter = ReferenceGame2048Adapter()
        self._cached_read: tuple[ScreenObservation, list[ActionCandidate]] | None = None

    def capture_playable_state(self) -> CampaignState:
        started_at = time.monotonic()
        deadline = started_at + SCREEN_READY_TIMEOUT_SECONDS
        last_state: dict[str, object] = {}
        capture_attempts = 0
        last_board_detected = False
        last_game_over: bool | None = None
        last_candidate_count = 0
        last_frame_signature = ""
        frame_signatures: set[str] = set()
        while time.monotonic() < deadline:
            capture_attempts += 1
            try:
                frame = self.capture.capture(self.window_handle)
            except Exception as error:
                raise CampaignFailure(
                    "capture_failure",
                    f"2048 window capture failed: {type(error).__name__}: {error}",
                ) from error
            try:
                observation = self.analyzer.analyze(frame, "live-2048-campaign")
                adapted, candidates = self.adapter.adapt(frame, observation)
            except Exception as error:
                raise CampaignFailure(
                    "recognition_failure",
                    f"2048 screen analysis failed: {type(error).__name__}: {error}",
                ) from error

            frame_signature = observation.features.get("signature")
            if isinstance(frame_signature, str) and frame_signature:
                last_frame_signature = frame_signature
                frame_signatures.add(frame_signature)

            raw_state = adapted.features.get("game_2048")
            if not isinstance(raw_state, dict):
                raise CampaignFailure(
                    "recognition_failure",
                    "2048 screen adapter did not return its game state",
                )
            last_state = raw_state
            board = raw_state.get("board")
            signature = raw_state.get("state_signature")
            terminal = raw_state.get("game_over")
            last_board_detected = isinstance(board, list)
            last_game_over = terminal if type(terminal) is bool else None
            last_candidate_count = len(candidates)
            if (
                isinstance(board, list)
                and isinstance(signature, str)
                and signature
                and type(terminal) is bool
                and (terminal or candidates)
            ):
                score = raw_state.get("score")
                max_tile = max(
                    (
                        value
                        for row in board
                        if isinstance(row, list)
                        for value in row
                        if type(value) is int
                    ),
                    default=0,
                )
                self._cached_read = (adapted, candidates)
                return CampaignState(signature, terminal, score, max_tile)
            time.sleep(SCREEN_READY_POLL_SECONDS)

        confidence = last_state.get("confidence")
        raise CampaignFailure(
            "recognition_failure",
            "2048 board did not become actionable within "
            f"{SCREEN_READY_TIMEOUT_SECONDS:g} seconds; "
            f"capture_attempts={capture_attempts}; "
            f"elapsed_seconds={time.monotonic() - started_at:.3f}; "
            f"board_detected={last_board_detected}; "
            f"game_over={last_game_over!r}; "
            f"actionable_candidate_count={last_candidate_count}; "
            f"distinct_frame_signatures={len(frame_signatures)}; "
            f"last_frame_signature={last_frame_signature or 'unavailable'}; "
            f"board_confidence={confidence!r}",
        )

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        cached_read = self._cached_read
        if cached_read is None:
            raise RuntimeError("campaign action requested without a fresh screen observation")
        self._cached_read = None
        return cached_read

    def close(self) -> None:
        self.capture.close()


class CyclingDirectionProvider:
    def __init__(self) -> None:
        self._next_direction_index = 0

    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        del personality
        allowed_ids = {candidate.action_id for candidate in context.candidates if candidate.allowed}
        selected_action_id = next(
            (
                DIRECTION_ACTION_IDS[(self._next_direction_index + offset) % len(DIRECTION_ACTION_IDS)]
                for offset in range(len(DIRECTION_ACTION_IDS))
                if DIRECTION_ACTION_IDS[(self._next_direction_index + offset) % len(DIRECTION_ACTION_IDS)]
                in allowed_ids
            ),
            None,
        )
        if selected_action_id is None:
            raise CampaignFailure(
                "recognition_failure",
                "2048 screen did not provide an allowed arrow-key candidate",
            )
        selected_index = DIRECTION_ACTION_IDS.index(selected_action_id)
        self._next_direction_index = (selected_index + 1) % len(DIRECTION_ACTION_IDS)
        return ActionDecision(
            selected_action_id,
            "cycle through the screen adapter's allowed 2048 arrow-key candidates",
            "campaign_direction_cycle",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )


class ReferenceGame2048Episode(CampaignEpisode):
    def __init__(
        self,
        episode_number: int,
        cache_root: Path,
        runtime_root: Path,
        browser_path: Path | None,
        ocr_recognizer: object | None,
    ) -> None:
        self.episode_number = episode_number
        self.launcher = ReferenceGame2048Launcher(cache_root, browser_path)
        self.source: Cached2048ObservationSource | None = None
        self.pipeline: DecisionPipeline | None = None
        self._safety_rearmed = False
        self._closed = False
        try:
            self.launcher.start()
            window = self.launcher.window_info
            if window is None or not window.handle or not window.process_id:
                raise CampaignFailure(
                    "episode_start_failure",
                    "2048 launcher did not provide a browser HWND and owning process ID",
                )
            self.source = Cached2048ObservationSource(window.handle, ocr_recognizer)
            self.pipeline = DecisionPipeline(
                self.source,
                runtime_root / f"episode-{episode_number:04d}",
                CyclingDirectionProvider(),
                dry_run=False,
                window_handle=window.handle,
                window_process_id=window.process_id,
                input_mode="window_message",
                # {
                #   理由: [action後のWGC captureとscreen analysisを含む周期が2秒を超えても新しい観測を通知できるよう5秒待ち、無観測が続く場合はwatchdogで停止する]
                # }
                fail_safe_config=FailSafeConfig(
                    observation_timeout_seconds=CAMPAIGN_OBSERVATION_TIMEOUT_SECONDS
                ),
            )
        except Exception:
            self.close()
            raise

    def observe(self) -> CampaignState:
        if self.source is None:
            raise CampaignFailure("capture_failure", "2048 episode has no window observation source")
        capture_started_at = time.monotonic()
        state = self.source.capture_playable_state()
        capture_elapsed_seconds = time.monotonic() - capture_started_at
        pipeline = self.pipeline
        if pipeline is not None:
            first_observation = not self._safety_rearmed
            try:
                # {
                #   理由: [playable boardの取得は最大45秒待つため、5秒の観測leaseは最初の新鮮なboard取得直後に開始し、episode起動中の失効を防ぐ]
                # }
                if first_observation:
                    pipeline.rearm_safety()
                    self._safety_rearmed = True
                pipeline.record_observation()
            except Exception as error:
                raise CampaignFailure(
                    "safety_block",
                    "2048 screen capture completed in "
                    f"{capture_elapsed_seconds:.3f} seconds, but the fail-safe runtime "
                    "could not accept the fresh observation; "
                    f"first_observation={first_observation}; "
                    f"error={type(error).__name__}: {error}",
                ) from error
        return state

    def act(self) -> CampaignActionResult:
        if self.pipeline is None:
            raise CampaignFailure("execution_failure", "2048 episode has no live decision pipeline")
        try:
            result = self.pipeline.run_and_execute(purpose=CAMPAIGN_PURPOSE)
        except Exception as error:
            safety_result = self.pipeline.last_safety_result
            runtime = self.pipeline.executor.fail_safe_runtime
            fail_safe_detail = (
                f"; fail-safe state={runtime.state.value}, reason={runtime.reason}"
                if runtime is not None
                else ""
            )
            if safety_result is not None and safety_result.status.value == "block":
                category = "safety_block"
            elif "safety" in str(error).casefold() or "blocked" in str(error).casefold():
                category = "safety_block"
            elif "recognition" in str(error).casefold() or "candidate" in str(error).casefold():
                category = "recognition_failure"
            else:
                category = "decision_failure"
            raise CampaignFailure(
                category,
                f"2048 pipeline action failed: {type(error).__name__}: {error}{fail_safe_detail}",
            ) from error
        return CampaignActionResult(result.action_id, result.executed, result.mode, result.detail)

    def record_outcome(self, status: str, confidence: float, evidence: str) -> None:
        if self.pipeline is None:
            raise RuntimeError("2048 pipeline is unavailable for recording the action outcome")
        try:
            self.pipeline.record_safety_outcome(status, confidence, evidence)
        except Exception as error:
            raise CampaignFailure(
                "safety_block",
                f"2048 action outcome could not be recorded: {type(error).__name__}: {error}",
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_errors: list[Exception] = []
        if self.pipeline is not None:
            try:
                self.pipeline.close()
            except Exception as error:
                close_errors.append(error)
            self.pipeline = None
        if self.source is not None:
            try:
                self.source.close()
            except Exception as error:
                close_errors.append(error)
            self.source = None
        try:
            self.launcher.close()
        except Exception as error:
            close_errors.append(error)
        if close_errors:
            raise RuntimeError(
                "2048 episode cleanup failed: "
                + "; ".join(f"{type(error).__name__}: {error}" for error in close_errors)
            ) from close_errors[0]


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("GITHUB_SHA")
    return result.stdout.strip() or os.environ.get("GITHUB_SHA")


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the pinned 2048 game through the shared live-input campaign runner"
    )
    parser.add_argument("--profile", choices=("pr", "release"), default="pr")
    parser.add_argument("--duration-seconds", type=float, help="override the selected 600s or 1800s profile")
    parser.add_argument("--cache-dir", type=Path, default=Path("build/reference_games/2048"))
    parser.add_argument("--artifact-dir", type=Path, default=Path("build/e2e"))
    parser.add_argument("--browser", type=Path, help="Edge or Chrome executable; omitted to search installed browsers")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    if os.name != "nt":
        print("2048 campaign requires Windows window capture and input", file=sys.stderr)
        return 2
    duration_seconds = arguments.duration_seconds
    if duration_seconds is None:
        duration_seconds = (
            PR_CAMPAIGN_DURATION_SECONDS
            if arguments.profile == "pr"
            else RELEASE_CAMPAIGN_DURATION_SECONDS
        )
    try:
        config = CampaignConfig(arguments.profile, duration_seconds)
        root = Path(__file__).resolve().parents[1]
        source_lock = json.loads(SOURCE_LOCK_PATH.read_text(encoding="utf-8"))
        ocr_recognizer = (
            TesseractOcrRecognizer.optional()
            if shutil.which("tesseract") or os.environ.get("TESSERACT_CMD")
            else None
        )
        run_name = datetime.now(timezone.utc).strftime("2048-campaign-%Y%m%dT%H%M%SZ")
        output_root = arguments.artifact_dir.resolve() / run_name
        cache_root = arguments.cache_dir.resolve()
        runtime_root = output_root / "runtime"
        runner = CampaignRunner(config)
        report = runner.run(
            lambda episode_number: ReferenceGame2048Episode(
                episode_number,
                cache_root,
                runtime_root,
                arguments.browser,
                ocr_recognizer,
            ),
            output_root / "trace.jsonl",
            output_root / "report.json",
            metadata={
                "git_commit": _git_commit(root),
                "game_source_commit": source_lock.get("commit"),
                "game_source_tree": source_lock.get("git_tree_sha"),
                "profile": arguments.profile,
                "input_mode": "window_message",
                "score_ocr_enabled": ocr_recognizer is not None,
                "screenshots_stored": False,
            },
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["success"] else 1
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"2048 campaign could not start: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
