"""Run a bounded live-input endurance campaign against the pinned 2048 build."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import replace
from uuid import uuid4
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
from ai_game_player.event_journal import EventJournal
from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition
from ai_game_player.planner_factory import reference_2048_planner_provider
from ai_game_player.run_control import RunController
from ai_game_player.decision_context import DecisionContext
from ai_game_player.fail_safe_runtime import FailSafeConfig
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture
from tools.recognition_failure_evidence import RecognitionFailureEvidence
from tools.startup_failure_evidence import StartupFailureEvidence
from tools.observation_timing import ObservationTiming
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
    def __init__(
        self,
        window_handle: int,
        ocr_recognizer: object | None,
        failure_evidence: RecognitionFailureEvidence | None = None,
    ) -> None:
        self.window_handle = window_handle
        self.capture = WindowsScreenCapture()
        self.analyzer = FrameAnalyzer(ocr_recognizer)
        self.adapter = ReferenceGame2048Adapter()
        self.failure_evidence = failure_evidence
        self._cached_read: tuple[ScreenObservation, list[ActionCandidate]] | None = None
        self.latest_observation: ScreenObservation | None = None
        self.source_id = "campaign." + uuid4().hex
        self.capture_sequence = 0
        self.settle_seconds = 0.0
        self.not_before = 0.0
        self.fresh_frame_callback: Callable[[], None] | None = None

    def capture_playable_state(
        self,
        on_fresh_frame: Callable[[], None] | None = None,
        *,
        timeout_seconds: float = SCREEN_READY_TIMEOUT_SECONDS,
    ) -> CampaignState:
        self.observation_timing = ObservationTiming()
        started_at = time.monotonic()
        deadline = started_at + min(SCREEN_READY_TIMEOUT_SECONDS, timeout_seconds)
        remaining = self.not_before - started_at
        if remaining > 0:
            time.sleep(min(remaining, max(0, deadline - time.monotonic())))
        last_state: dict[str, object] = {}
        capture_attempts = 0
        last_board_detected = False
        last_game_over: bool | None = None
        last_candidate_count = 0
        last_frame_signature = ""
        frame_signatures: set[str] = set()
        last_frame: ScreenFrame | None = None
        while time.monotonic() < deadline:
            capture_attempts += 1
            try:
                with self.observation_timing.measure("capture"):
                    frame = self.capture.capture(self.window_handle)
            except Exception as error:
                raise CampaignFailure(
                    "capture_failure",
                    f"2048 window capture failed: {type(error).__name__}: {error}",
                ) from error
            last_frame = frame
            if on_fresh_frame is not None:
                with self.observation_timing.measure("observation_refresh"):
                    on_fresh_frame()
            try:
                with self.observation_timing.measure("frame_analysis"):
                    observation = self.analyzer.analyze(frame, "live-2048-campaign")
                with self.observation_timing.measure("board_adaptation"):
                    adapted, candidates = self.adapter.adapt(frame, observation)
                self.capture_sequence += 1
                adapted = replace(
                    adapted,
                    features={
                        **adapted.features,
                        "capture_metadata": {
                            "source_id": self.source_id,
                            "frame_id": f"{self.source_id}.frame{self.capture_sequence}",
                            "source_generation": 0,
                            "monotonic_ns": int(frame.captured_at * 1_000_000_000),
                        },
                    },
                )
            except Exception as error:
                raise self._recognition_failure(
                    f"2048 screen analysis failed: {type(error).__name__}: {error}",
                    frame,
                    {},
                    {"capture_attempts": capture_attempts},
                ) from error

            frame_signature = observation.features.get("signature")
            if isinstance(frame_signature, str) and frame_signature:
                last_frame_signature = frame_signature
                frame_signatures.add(frame_signature)

            raw_state = adapted.features.get("game_2048")
            if not isinstance(raw_state, dict):
                raise self._recognition_failure(
                    "2048 screen adapter did not return its game state",
                    frame,
                    {},
                    {"capture_attempts": capture_attempts},
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
                self.latest_observation = adapted
                if self.failure_evidence is not None:
                    with self.observation_timing.measure("evidence_retention"):
                        self.failure_evidence.record_playable(frame, raw_state)
                return CampaignState(signature, terminal, score, max_tile)
            with self.observation_timing.measure("retry_wait"):
                time.sleep(SCREEN_READY_POLL_SECONDS)

        confidence = last_state.get("confidence")
        candidate_confidence_threshold = getattr(
            self.adapter,
            "minimum_candidate_confidence",
            None,
        )
        raise self._recognition_failure(
            "2048 board did not become actionable within "
            f"{SCREEN_READY_TIMEOUT_SECONDS:g} seconds; "
            f"capture_attempts={capture_attempts}; "
            f"elapsed_seconds={time.monotonic() - started_at:.3f}; "
            f"board_detected={last_board_detected}; "
            f"game_over={last_game_over!r}; "
            f"actionable_candidate_count={last_candidate_count}; "
            f"distinct_frame_signatures={len(frame_signatures)}; "
            f"last_frame_signature={last_frame_signature or 'unavailable'}; "
            f"candidate_confidence_threshold={candidate_confidence_threshold!r}; "
            f"board_confidence={confidence!r}",
            last_frame,
            last_state,
            {
                "capture_attempts": capture_attempts,
                "last_frame_signature": last_frame_signature,
                "distinct_frame_signatures": len(frame_signatures),
                "candidate_confidence_threshold": candidate_confidence_threshold,
                "actionable_candidate_count": last_candidate_count,
            },
        )

    def _recognition_failure(
        self,
        detail: str,
        frame: ScreenFrame | None,
        game_state: dict[str, object],
        diagnostics: dict[str, object],
    ) -> CampaignFailure:
        if self.failure_evidence is not None:
            try:
                manifest = self.failure_evidence.save_failure(
                    frame,
                    game_state,
                    {
                        **diagnostics,
                        "failure_detail": detail,
                        "window_handle": self.window_handle,
                        "capture_backend_error": getattr(
                            self.capture, "last_backend_error", None
                        ),
                    },
                )
                detail += f"; failure_evidence={manifest}"
            except Exception as error:
                detail += f"; failure_evidence_error={type(error).__name__}: {error}"
        return CampaignFailure("recognition_failure", detail)

    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        cached_read = self._cached_read
        if cached_read is None:
            raise RuntimeError(
                "campaign action requested without a fresh screen observation"
            )
        self._cached_read = None
        return cached_read

    def read_fresh(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        self.capture_playable_state(
            on_fresh_frame=self.fresh_frame_callback, timeout_seconds=3.0
        )
        return self.read()

    def close(self) -> None:
        self.capture.close()


class CyclingDirectionProvider:
    def __init__(self) -> None:
        self._next_direction_index = 0

    def choose_context(
        self, context: DecisionContext, personality: str = ""
    ) -> ActionDecision:
        del personality
        allowed_ids = {
            candidate.action_id for candidate in context.candidates if candidate.allowed
        }
        selected_action_id = next(
            (
                DIRECTION_ACTION_IDS[
                    (self._next_direction_index + offset) % len(DIRECTION_ACTION_IDS)
                ]
                for offset in range(len(DIRECTION_ACTION_IDS))
                if DIRECTION_ACTION_IDS[
                    (self._next_direction_index + offset) % len(DIRECTION_ACTION_IDS)
                ]
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
        save_failure_evidence: bool = False,
        decision_provider: str = "cycling",
        planner_conditional: bool = False,
        planning_settle_ms: int = 900,
        test_seed: int | None = None,
        event_journal: EventJournal | None = None,
    ) -> None:
        if decision_provider not in {"cycling", "baseline", "planner"}:
            raise ValueError("unknown campaign decision provider")
        if planner_conditional and decision_provider != "planner":
            raise ValueError("conditional planning requires the planner profile")
        if type(planning_settle_ms) is not int or not 0 <= planning_settle_ms <= 2000:
            raise ValueError("planning settle must be 0..2000ms")
        self.episode_number = episode_number
        self.launcher = ReferenceGame2048Launcher(cache_root, browser_path)
        if test_seed is not None:
            if type(test_seed) is not int or not 0 <= test_seed <= 0xFFFFFFFF:
                raise ValueError("campaign comparison seed must be uint32")
            self.launcher.server.test_seed = test_seed
        if save_failure_evidence:
            self.launcher.failure_evidence = StartupFailureEvidence(
                runtime_root / f"episode-{episode_number:04d}"
            )
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
            evidence = (
                RecognitionFailureEvidence(
                    runtime_root / f"episode-{episode_number:04d}"
                )
                if save_failure_evidence
                else None
            )
            self.source = Cached2048ObservationSource(
                window.handle, ocr_recognizer, evidence
            )
            controller = RunController()
            provider = (
                reference_2048_planner_provider(
                    controller=controller, allow_conditional=planner_conditional
                )
                if decision_provider == "planner"
                else BaselineDecisionProvider(min_margin=0, min_recognition=0.65)
                if decision_provider == "baseline"
                else CyclingDirectionProvider()
            )
            if decision_provider != "cycling":
                self.source.settle_seconds = planning_settle_ms / 1000
                self.source.not_before = time.monotonic() + self.source.settle_seconds
            self.pipeline = DecisionPipeline(
                self.source,
                runtime_root / f"episode-{episode_number:04d}",
                provider,
                controller=controller,
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
                **(
                    {"event_journal": event_journal}
                    if event_journal is not None
                    else {}
                ),
            )
            self.source.fresh_frame_callback = self.pipeline.record_observation
        except Exception:
            self.close()
            raise

    def observe(self) -> CampaignState:
        if self.source is None:
            raise CampaignFailure(
                "capture_failure", "2048 episode has no window observation source"
            )
        pipeline = self.pipeline
        complete = getattr(pipeline, "complete_execution_observation", None)
        observation_token = (
            pipeline.controller.rearm_token if callable(complete) else None
        )
        first_observation = not self._safety_rearmed
        capture_started_at = time.monotonic()

        def refresh_observation_after_frame() -> None:
            try:
                if pipeline is not None:
                    pipeline.record_observation()
            except Exception as error:
                raise CampaignFailure(
                    "safety_block",
                    "2048 fresh screen frame arrived, but FailSafeRuntime rejected its observation update; "
                    f"first_observation={first_observation}; "
                    f"capture_elapsed_seconds={time.monotonic() - capture_started_at:.3f}; "
                    f"error={type(error).__name__}: {error}",
                ) from error

        state = self.source.capture_playable_state(
            on_fresh_frame=(
                refresh_observation_after_frame
                if pipeline is not None and not first_observation
                else None
            )
        )
        capture_elapsed_seconds = time.monotonic() - capture_started_at
        if pipeline is not None and first_observation:
            try:
                # {
                #   理由: [playable boardの取得は最大45秒待つため、5秒の観測leaseは最初の新鮮なboard取得直後に開始し、episode起動中の失効を防ぐ]
                # }
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
        latest = getattr(self.source, "latest_observation", None)
        # 初回は実操作前の初期化で、安全再開によって世代も進む。
        # 後続の観測だけを、取得開始時に保持した実行世代へ結び付ける。
        if not first_observation and latest is not None and callable(complete):
            complete(latest, expected_rearm_token=observation_token)
        return state

    def act(self) -> CampaignActionResult:
        if self.pipeline is None:
            raise CampaignFailure(
                "execution_failure", "2048 episode has no live decision pipeline"
            )
        try:
            result = DecisionOrchestrator(self.pipeline).run(
                execute=True, purpose=CAMPAIGN_PURPOSE
            )
            if isinstance(result, DecisionDisposition):
                raise CampaignFailure(
                    "decision_failure",
                    "input withheld: " + json.dumps(result.to_dict(), sort_keys=True),
                )
            if self.source is not None:
                self.source.not_before = time.monotonic() + self.source.settle_seconds
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
            elif (
                "safety" in str(error).casefold() or "blocked" in str(error).casefold()
            ):
                category = "safety_block"
            elif (
                "recognition" in str(error).casefold()
                or "candidate" in str(error).casefold()
            ):
                category = "recognition_failure"
            else:
                category = "decision_failure"
            raise CampaignFailure(
                category,
                f"2048 pipeline action failed: {type(error).__name__}: {error}{fail_safe_detail}"
                f"; observation_timing={json.dumps(self._observation_timing_snapshot(), sort_keys=True)}",
            ) from error
        return CampaignActionResult(
            result.action_id, result.executed, result.mode, result.detail
        )

    def _observation_timing_snapshot(self) -> dict[str, object] | None:
        """最後の観測区間の計測を返す。観測前なら未計測を示すNoneを返す。"""
        timing = getattr(getattr(self, "source", None), "observation_timing", None)
        return timing.snapshot() if timing is not None else None

    def record_outcome(self, status: str, confidence: float, evidence: str) -> None:
        if self.pipeline is None:
            raise RuntimeError(
                "2048 pipeline is unavailable for recording the action outcome"
            )
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
                + "; ".join(
                    f"{type(error).__name__}: {error}" for error in close_errors
                )
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
    parser.add_argument(
        "--duration-seconds",
        type=float,
        help="override the selected 600s or 1800s profile",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("build/reference_games/2048")
    )
    parser.add_argument("--artifact-dir", type=Path, default=Path("build/e2e"))
    parser.add_argument(
        "--browser",
        type=Path,
        help="Edge or Chrome executable; omitted to search installed browsers",
    )
    parser.add_argument(
        "--save-failure-evidence",
        action="store_true",
        help="save bounded owned-window startup evidence and owned 2048 recognition failure frames (PNG/BGRA)",
    )
    parser.add_argument(
        "--decision-provider",
        choices=("cycling", "baseline", "planner"),
        default="cycling",
        help="optional decision comparison; default Windows endurance gate remains cycling",
    )
    parser.add_argument(
        "--planner-conditional",
        action="store_true",
        help="explicit experiment: permit a single uncalibrated recognized world; global risk remains unknown",
    )
    parser.add_argument(
        "--planning-settle-ms",
        type=int,
        default=900,
        help="bounded screenshot settling profile for baseline/planner, independent of Safety lease",
    )
    result = parser.parse_args()
    if result.planner_conditional and result.decision_provider != "planner":
        parser.error("--planner-conditional requires --decision-provider planner")
    if not 0 <= result.planning_settle_ms <= 2000:
        parser.error("--planning-settle-ms must be 0..2000")
    return result


def main() -> int:
    arguments = _parse_arguments()
    if os.name != "nt":
        print(
            "2048 campaign requires Windows window capture and input", file=sys.stderr
        )
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
                save_failure_evidence=arguments.save_failure_evidence,
                decision_provider=arguments.decision_provider,
                planner_conditional=arguments.planner_conditional,
                planning_settle_ms=arguments.planning_settle_ms,
            ),
            output_root / "trace.jsonl",
            output_root / "report.json",
            metadata={
                "git_commit": _git_commit(root),
                "game_source_commit": source_lock.get("commit"),
                "game_source_tree": source_lock.get("git_tree_sha"),
                "profile": arguments.profile,
                "decision_provider": arguments.decision_provider,
                "planner_conditional": arguments.planner_conditional,
                "planning_global_world_risk": "unknown"
                if arguments.decision_provider == "planner"
                else "not_applicable",
                "planning_settle_ms": arguments.planning_settle_ms,
                "input_mode": "window_message",
                "score_ocr_enabled": ocr_recognizer is not None,
                "failure_evidence_enabled": arguments.save_failure_evidence,
                "screenshot_policy": "startup_and_recognition_failure_only"
                if arguments.save_failure_evidence
                else "off",
            },
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["success"] else 1
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(
            f"2048 campaign could not start: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
