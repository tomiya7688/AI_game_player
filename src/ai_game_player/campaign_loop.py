from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Callable

from ai_game_player.campaign_artifacts import (
    append_campaign_event,
    bounded_error_detail,
    episode_event,
    update_metrics,
)
from ai_game_player.campaign_contract import CampaignConfig, CampaignEpisode, CampaignFailure, CampaignState


@dataclass
# {
#   責務: [CampaignProgress: 1回のcampaignで変化するepisode、状態、action、停止、metricを集約する]
#   フィールド: [started_at: monotonic開始時刻, episode_number: 開始済みepisode数, current_episode/state: 現在のLevel episodeとscreen state, counts: step/action/restart/recovery上限に使うcounter, milestone_enabled/reached: predicate設定と到達状態, latencies_ms: actionからafter-observationまでの測定値, scores/tiles: report用screen-derived metric, success/stop_reason/failure_detail: 最終結果]
# }
class CampaignProgress:
    started_at: float
    episode_number: int = 0
    current_episode: CampaignEpisode | None = None
    state: CampaignState | None = None
    step_count: int = 0
    action_count: int = 0
    terminal_restart_count: int = 0
    recovery_count: int = 0
    live_input_verified: bool = False
    unchanged_action_count: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    last_score: int | None = None
    peak_score: int | None = None
    score_samples: int = 0
    max_tile: int = 0
    milestone_enabled: bool = False
    milestone_reached: bool = False
    success: bool = False
    stop_reason: str = "unexpected_failure"
    failure_detail: str | None = None


# {
#   責務: [CampaignLoop: episode lifecycle、deadline、terminal restart、stall recovery、action後観測を実行する]
#   フィールド: [config: deadlineとbounded recovery上限, clock/sleeper: 再現可能な時刻・待機関数]
#   処理: [screen recognitionとinputはCampaignEpisodeへ委譲し、共通状態遷移だけを管理する]
# }
class CampaignLoop:
    # {
    #   責務: [__init__: loopの上限設定と時間測定関数を保持する]
    #   引数: [config: duration、step、restart、stall上限, clock: monotonic時計, sleeper: screen settle後の待機関数]
    #   戻り値: []
    # }
    def __init__(
        self,
        config: CampaignConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.clock = clock
        self.sleeper = sleeper

    # {
    #   責務: [execute: 最初のepisodeを開始してmilestoneまたはdeadlineまで共通action loopを動かす]
    #   引数: [episode_factory: episode番号からLevel固有runtimeを作る関数, trace_path: lifecycle/step event保存先, progress: campaign counterとmetric, milestone_probe: after-observationが目標状態へ到達したか判定する任意関数]
    #   戻り値: [bool: milestone_probeがtrueを返してdeadlineより前に停止した場合はtrue]
    #   エラー: [CampaignFailure: step/restart/stall上限、milestone判定、episode起動、capture、decision、inputが失敗した場合]
    # }
    def execute(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        milestone_probe: Callable[[CampaignState], bool] | None = None,
    ) -> bool:
        progress.episode_number = 1
        self._start_episode(episode_factory, trace_path, progress, "episode_started", "initial_start")
        return self._run_until_deadline(episode_factory, trace_path, progress, milestone_probe)

    # {
    #   責務: [_run_until_deadline: step上限とterminal状態を確認し、milestoneまたはdeadlineまでepisode actionを繰り返す]
    #   引数: [episode_factory: terminal/stall後のepisode factory, trace_path: lifecycle event保存先, progress: 現在のcampaign状態, milestone_probe: after-observationの状態を目標判定する任意関数]
    #   戻り値: [bool: milestone_probeがtrueを返してdeadlineより前に停止した場合はtrue]
    #   エラー: [CampaignFailure: step/restart/stall上限、milestone判定、episode起動、capture、decision、inputが失敗した場合]
    # }
    def _run_until_deadline(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        milestone_probe: Callable[[CampaignState], bool] | None,
    ) -> bool:
        while self.clock() - progress.started_at < self.config.duration_seconds:
            if progress.step_count >= self.config.max_steps:
                raise CampaignFailure(
                    "step_limit",
                    f"campaign reached the {self.config.max_steps} action limit before its duration deadline",
                )
            state = progress.state
            if state is None:
                raise CampaignFailure("recognition_failure", "campaign episode returned no initial screen state")
            if state.terminal:
                self._restart_terminal_episode(episode_factory, trace_path, progress, state)
                continue
            if self._run_action_step(episode_factory, trace_path, progress, state, milestone_probe):
                return True
        return False

    # {
    #   責務: [_start_episode: Level固有runtimeを起動して初期画面とmetricを取得する]
    #   引数: [episode_factory: episode番号を受ける生成関数, trace_path: episode event保存先, progress: episode番号と現在状態, reason: 起動理由の記録値]
    #   戻り値: []
    #   エラー: [CampaignFailure: episode作成または初期screen observationが失敗した場合]
    # }
    def _start_episode(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        event_name: str,
        reason: str,
    ) -> None:
        episode_number = progress.episode_number
        episode = self._create_episode(episode_factory, episode_number)
        progress.current_episode = episode
        state = episode.observe()
        progress.state = state
        progress.last_score, progress.peak_score, progress.max_tile = update_metrics(
            state, progress.last_score, progress.peak_score, progress.max_tile
        )
        if state.score is not None:
            progress.score_samples += 1
        append_campaign_event(trace_path, episode_event(event_name, episode_number, reason, state))

    # {
    #   責務: [_restart_terminal_episode: game-overを記録し現episodeを閉じて新しいgameを起動する]
    #   引数: [episode_factory: 新しいbrowser episode生成関数, trace_path: lifecycle event保存先, progress: restart countと現在episode, terminal_state: Adapterが検出した終了画面]
    #   戻り値: []
    #   エラー: [CampaignFailure: restart上限、close失敗、新episode起動失敗、または再起動後のterminal画面]
    # }
    def _restart_terminal_episode(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        terminal_state: CampaignState,
    ) -> None:
        if progress.terminal_restart_count >= self.config.terminal_restart_limit:
            raise CampaignFailure(
                "terminal_restart_limit",
                f"campaign reached the {self.config.terminal_restart_limit} terminal restart limit",
            )
        append_campaign_event(
            trace_path,
            episode_event("episode_terminal", progress.episode_number, "game_over_detected", terminal_state),
        )
        progress.terminal_restart_count += 1
        self._replace_episode(episode_factory, trace_path, progress, "terminal_restart")

    # {
    #   責務: [_replace_episode: 現episodeを解放し番号を進めて新しいscreen episodeを開始する]
    #   引数: [episode_factory: 新episode生成関数, trace_path: episode lifecycle保存先, progress: 現在のepisode状態, reason: episodeを作り直した理由]
    #   戻り値: []
    #   エラー: [CampaignFailure: 現episode終了、新episode起動、または初期board認識が失敗した場合]
    # }
    def _replace_episode(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        reason: str,
    ) -> None:
        self.close_episode(progress)
        progress.episode_number += 1
        event_name = "recovery_started" if reason == "screen_stall" else "episode_started"
        self._start_episode(episode_factory, trace_path, progress, event_name, reason)
        state = progress.state
        if state is not None and state.terminal:
            raise CampaignFailure(
                "restart_failed",
                f"new episode {progress.episode_number} opened with a terminal screen instead of a playable board",
            )
        progress.unchanged_action_count = 0

    # {
    #   責務: [_run_action_step: action実行、after-state観測、Safety結果、milestone、step metricを記録する]
    #   処理: [live OS inputを確認し、画面settle後に再captureしてsignature変化・terminal・latencyをJSONLへ保存する]
    #   引数: [episode_factory: stall recovery用factory, trace_path: step trace保存先, progress: current episodeと集計counter, before: action直前のscreen state]
    #   戻り値: [bool: after-stateがmilestoneに到達した場合はtrue]
    #   エラー: [CampaignFailure: live input不在、screen観測、Safety outcome記録、またはmilestone判定が失敗した場合]
    # }
    def _run_action_step(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
        before: CampaignState,
        milestone_probe: Callable[[CampaignState], bool] | None,
    ) -> bool:
        episode = progress.current_episode
        if episode is None:
            raise CampaignFailure("episode_start_failure", "campaign has no active episode")
        step_started = self.clock()
        action = episode.act()
        progress.step_count += 1
        if not action.executed or action.mode != "live":
            append_campaign_event(
                trace_path,
                {
                    "event": "action_not_executed",
                    "step": progress.step_count,
                    "episode": progress.episode_number,
                    "action_id": action.action_id,
                    "mode": action.mode,
                    "detail": bounded_error_detail(action.detail),
                },
            )
            raise CampaignFailure(
                "live_input_not_verified",
                f"action {action.action_id!r} did not complete as live OS input",
            )

        progress.action_count += 1
        progress.live_input_verified = True
        remaining_seconds = self.config.duration_seconds - (self.clock() - progress.started_at)
        if self.config.step_delay_seconds and remaining_seconds > 0:
            self.sleeper(min(self.config.step_delay_seconds, remaining_seconds))
        after = episode.observe()
        latency_ms = max(0.0, (self.clock() - step_started) * 1000.0)
        progress.latencies_ms.append(latency_ms)
        progress.last_score, progress.peak_score, progress.max_tile = update_metrics(
            after, progress.last_score, progress.peak_score, progress.max_tile
        )
        if after.score is not None:
            progress.score_samples += 1
        state_changed = before.signature != after.signature
        status, confidence, evidence = _outcome_for_state_change(state_changed, after.terminal)
        progress.state = after
        progress.unchanged_action_count = (
            0 if state_changed or after.terminal else progress.unchanged_action_count + 1
        )
        append_campaign_event(
            trace_path,
            {
                "event": "step",
                "step": progress.step_count,
                "episode": progress.episode_number,
                "action_id": action.action_id,
                "action_detail": bounded_error_detail(action.detail),
                "state_signature": after.signature,
                "state_changed": state_changed,
                "terminal": after.terminal,
                "score": after.score,
                "peak_score": progress.peak_score,
                "max_tile": after.max_tile,
                "latency_ms": round(latency_ms, 3),
            },
        )
        episode.record_outcome(status, confidence, evidence)
        if self._probe_milestone(milestone_probe, after):
            append_campaign_event(
                trace_path,
                {
                    "event": "milestone_reached",
                    "step": progress.step_count,
                    "episode": progress.episode_number,
                    "state_signature": after.signature,
                },
            )
            return True
        if progress.unchanged_action_count >= self.config.stall_action_limit:
            self._recover_stalled_episode(episode_factory, trace_path, progress)
        return False

    # {
    #   責務: [_probe_milestone: 最新の画面状態を任意のLevel固有predicateへ渡してmilestone到達を判定する]
    #   引数: [milestone_probe: CampaignStateを受けて到達状態を返す関数または未設定, state: live action後に観測した画面由来状態]
    #   戻り値: [bool: callbackがboolのtrueを返した場合は到達]
    #   エラー: [CampaignFailure: callbackがbool以外を返す、または判定中に例外を出した場合]
    # }
    def _probe_milestone(
        self,
        milestone_probe: Callable[[CampaignState], bool] | None,
        state: CampaignState,
    ) -> bool:
        if milestone_probe is None:
            return False
        try:
            reached = milestone_probe(state)
        except Exception as error:
            raise CampaignFailure(
                "milestone_failure",
                f"campaign milestone callback failed: {type(error).__name__}: {error}",
            ) from error
        if type(reached) is not bool:
            raise CampaignFailure(
                "milestone_failure",
                "campaign milestone callback must return bool",
            )
        return reached

    # {
    #   責務: [_recover_stalled_episode: stall上限を確認して新しいepisodeでbounded recoveryを実行する]
    #   引数: [episode_factory: recovery後のepisode生成関数, trace_path: recovery event保存先, progress: stall回数・状態・episode番号]
    #   戻り値: []
    #   エラー: [CampaignFailure: recovery上限またはepisode再生成に失敗した場合]
    # }
    def _recover_stalled_episode(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        progress: CampaignProgress,
    ) -> None:
        if progress.recovery_count >= self.config.recovery_limit:
            raise CampaignFailure(
                "stall_limit",
                f"screen state did not change for {progress.unchanged_action_count} actions after {progress.recovery_count} bounded recoveries",
            )
        state = progress.state
        if state is None:
            raise CampaignFailure("recognition_failure", "stalled episode has no current screen state")
        append_campaign_event(
            trace_path,
            {
                "event": "stall_detected",
                "episode": progress.episode_number,
                "unchanged_actions": progress.unchanged_action_count,
                "recovery": progress.recovery_count + 1,
                "state_signature": state.signature,
            },
        )
        progress.recovery_count += 1
        self._replace_episode(episode_factory, trace_path, progress, "screen_stall")

    # {
    #   責務: [close_episode: 現episodeのwindow capture・pipeline・browser resourceを一度だけ解放する]
    #   引数: [progress: 解放するepisodeを保持するcampaign状態]
    #   戻り値: []
    #   エラー: [CampaignFailure: episode cleanupが失敗した場合]
    # }
    def close_episode(self, progress: CampaignProgress) -> None:
        episode = progress.current_episode
        progress.current_episode = None
        if episode is None:
            return
        try:
            episode.close()
        except Exception as error:
            raise CampaignFailure(
                "cleanup_failure",
                f"episode {progress.episode_number} cleanup failed: {type(error).__name__}: {error}",
            ) from error

    # {
    #   責務: [close_after_run: run終了後に残っているepisodeを解放しcleanup failureを結果へ反映する]
    #   引数: [trace_path: cleanup failure event保存先, progress: run結果とactive episode]
    #   戻り値: []
    # }
    def close_after_run(self, trace_path: Path, progress: CampaignProgress) -> None:
        try:
            self.close_episode(progress)
        except CampaignFailure as error:
            if progress.success:
                progress.success = False
                progress.stop_reason = error.category
                progress.failure_detail = bounded_error_detail(error.detail)
            elif progress.failure_detail:
                progress.failure_detail = bounded_error_detail(
                    f"{progress.failure_detail}; cleanup failed: {error.detail}"
                )
            else:
                progress.failure_detail = bounded_error_detail(error.detail)
            append_campaign_event(
                trace_path,
                {"event": "cleanup_failure", "detail": bounded_error_detail(error.detail)},
            )

    # {
    #   責務: [_create_episode: Level-specific episode factoryを呼び、start失敗を分類済みexceptionへ変換する]
    #   引数: [episode_factory: episode番号から対象game windowを開始する関数, episode_number: 1から始まるcampaign内episode番号]
    #   戻り値: [CampaignEpisode: observe/act/outcome/close契約を実装した新しいepisode]
    #   エラー: [CampaignFailure: browser、capture、pipelineを新規開始できない場合]
    # }
    def _create_episode(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        episode_number: int,
    ) -> CampaignEpisode:
        try:
            return episode_factory(episode_number)
        except CampaignFailure:
            raise
        except Exception as error:
            raise CampaignFailure(
                "episode_start_failure",
                f"episode {episode_number} could not start: {type(error).__name__}: {error}",
            ) from error


def _outcome_for_state_change(
    state_changed: bool,
    terminal: bool,
) -> tuple[str, float, str]:
    # {
    #   責務: [_outcome_for_state_change: screen state差分とterminal判定からSafety outcome evidenceを作る]
    #   引数: [state_changed: action後にstate signatureが変わったか, terminal: Adapterがgame-overを検出したか]
    #   戻り値: [tuple[str, float, str]: status、confidence、画面根拠の説明]
    # }
    if terminal:
        return "failure", 0.9, "screen adapter detected the 2048 game-over state"
    if state_changed:
        return "ongoing", 0.9, "screen-derived state signature changed after live input"
    return "unknown", 0.25, "screen-derived state signature did not change after live input"
