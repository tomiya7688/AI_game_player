from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Callable

from ai_game_player.campaign_artifacts import (
    append_campaign_event,
    bounded_error_detail,
    classify_campaign_error,
    latency_summary,
)
from ai_game_player.campaign_contract import CampaignConfig, CampaignEpisode, CampaignFailure, CampaignState
from ai_game_player.campaign_loop import CampaignLoop, CampaignProgress


# {
#   責務: [CampaignRunner: Level固有episodeをdeadlineまで実行しfailure分類とJSON artifactsを生成する]
#   フィールド: [config: duration/restart/recovery上限, clock/sleeper: 再現可能なdeadlineと待機制御]
#   処理: [episode lifecycleをCampaignLoopへ委譲し、reportとtraceの保存を担当する]
# }
class CampaignRunner:
    # {
    #   責務: [__init__: campaign profileとdeadline測定用clockを設定する]
    #   引数: [config: duration/restart/recovery上限を含むcampaign設定, clock: monotonic時刻を返す関数, sleeper: 指定秒数だけ待つ関数]
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
        self.loop = CampaignLoop(config, clock=clock, sleeper=sleeper)

    # {
    #   責務: [run: campaign artifactsを準備しmilestoneまたはdeadlineまでepisode loopを実行して最終reportを保存する]
    #   処理: [1: 未使用の出力pathを検証する, 2: milestone付きloopを呼ぶ, 3: failureを分類しepisodeを閉じる, 4: reportとtrace summaryを保存する]
    #   引数: [episode_factory: episode番号からLevel固有runtimeを作る関数, trace_path: event JSONL保存先, report_path: summary JSON保存先, metadata: commit/source/profile等のJSON metadata, milestone_probe: action後のCampaignStateを受けて目標到達を判定する任意関数]
    #   戻り値: [dict[str, object]: success、stop reason、milestone状態、metricを含むcampaign report]
    #   エラー: [CampaignFailure: artifact開始eventを書けずrun結果を記録できない場合]
    # }
    def run(
        self,
        episode_factory: Callable[[int], CampaignEpisode],
        trace_path: Path,
        report_path: Path,
        *,
        metadata: dict[str, object] | None = None,
        milestone_probe: Callable[[CampaignState], bool] | None = None,
    ) -> dict[str, object]:
        self._prepare_output_paths(trace_path, report_path)
        progress = CampaignProgress(self.clock(), milestone_enabled=milestone_probe is not None)
        run_metadata = dict(metadata or {})
        try:
            append_campaign_event(
                trace_path,
                {
                    "event": "campaign_started",
                    "profile": self.config.profile,
                    "duration_seconds": self.config.duration_seconds,
                    "milestone_enabled": progress.milestone_enabled,
                    "metadata": run_metadata,
                },
            )
        except OSError as error:
            raise CampaignFailure(
                "artifact_write_failure",
                f"campaign start event could not be written: {error}",
            ) from error

        try:
            progress.milestone_reached = self.loop.execute(
                episode_factory,
                trace_path,
                progress,
                milestone_probe,
            )
            if not progress.live_input_verified:
                raise CampaignFailure(
                    "live_input_not_verified",
                    "campaign reached its deadline without verifying a live OS input action",
                )
            progress.success = True
            progress.stop_reason = "milestone_reached" if progress.milestone_reached else "duration_reached"
        except CampaignFailure as error:
            self._record_failure(trace_path, progress, error.category, error.detail)
        except Exception as error:
            self._record_failure(
                trace_path,
                progress,
                classify_campaign_error(error),
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.loop.close_after_run(trace_path, progress)

        report = self._build_report(progress, run_metadata, trace_path)
        self._write_report(trace_path, report_path, report)
        return report

    # {
    #   責務: [_prepare_output_paths: trace/reportの衝突と既存artifactを検査して新規出力を予約する]
    #   引数: [trace_path: event JSONL保存先, report_path: summary JSON保存先]
    #   戻り値: []
    #   エラー: [CampaignFailure: path衝突、既存file、directory作成またはtrace作成に失敗した場合]
    # }
    def _prepare_output_paths(self, trace_path: Path, report_path: Path) -> None:
        if trace_path.resolve() == report_path.resolve():
            raise CampaignFailure("campaign_configuration", "trace and report paths must be different")
        if trace_path.exists() or report_path.exists():
            raise CampaignFailure(
                "campaign_configuration",
                "campaign output already exists; choose unused trace and report paths to preserve the existing run",
            )
        try:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as error:
            raise CampaignFailure("campaign_configuration", "campaign trace already exists") from error
        except OSError as error:
            raise CampaignFailure(
                "artifact_write_failure",
                f"campaign output directory or trace could not be created: {error}",
            ) from error

    # {
    #   責務: [_record_failure: hard failureの分類codeと上限付き説明をprogressとJSONLへ保存する]
    #   引数: [trace_path: failure event保存先, progress: 最終結果を保持するcampaign状態, category: 既知failure code, detail: 原因を説明する文字列]
    #   戻り値: []
    # }
    def _record_failure(
        self,
        trace_path: Path,
        progress: CampaignProgress,
        category: str,
        detail: str,
    ) -> None:
        progress.stop_reason = category
        progress.failure_detail = bounded_error_detail(detail)
        append_campaign_event(
            trace_path,
            {"event": "campaign_failure", "category": category, "detail": progress.failure_detail},
        )

    # {
    #   責務: [_build_report: progressとmetadataからJSON化可能なcampaign summaryを作る]
    #   引数: [progress: 終了したrunのstate/counts/metrics, metadata: commit/source/profile情報, trace_path: trace artifact位置]
    #   戻り値: [dict[str, object]: report schema campaign-report/v1]
    # }
    def _build_report(
        self,
        progress: CampaignProgress,
        metadata: dict[str, object],
        trace_path: Path,
    ) -> dict[str, object]:
        return {
            "schema": "campaign-report/v1",
            "success": progress.success,
            "profile": self.config.profile,
            "duration_target_seconds": self.config.duration_seconds,
            "elapsed_seconds": round(max(0.0, self.clock() - progress.started_at), 3),
            "stop_reason": progress.stop_reason,
            "failure_category": None if progress.success else progress.stop_reason,
            "failure_detail": progress.failure_detail,
            "milestone_enabled": progress.milestone_enabled,
            "milestone_reached": progress.milestone_reached,
            "steps": progress.step_count,
            "actions_executed": progress.action_count,
            "episode_count": progress.episode_number,
            "terminal_restart_count": progress.terminal_restart_count,
            "recovery_count": progress.recovery_count,
            "live_input_verified": progress.live_input_verified,
            "last_score": progress.last_score,
            "peak_score": progress.peak_score,
            "score_samples": progress.score_samples,
            "max_tile": progress.max_tile,
            "latency_ms": latency_summary(progress.latencies_ms),
            "metadata": metadata,
            "trace_path": str(trace_path),
        }

    # {
    #   責務: [_write_report: campaign summaryをtraceと新規JSON reportへ保存する]
    #   引数: [trace_path: summary eventのJSONL保存先, report_path: 人と機械が読むsummary JSONのpath, report: _build_reportが作成したcampaign結果]
    #   戻り値: []
    #   エラー: [CampaignFailure: report pathが既存、またはartifact書き込みに失敗した場合]
    # }
    def _write_report(self, trace_path: Path, report_path: Path, report: dict[str, object]) -> None:
        try:
            append_campaign_event(trace_path, {"event": "campaign_summary", **report})
            with report_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        except FileExistsError as error:
            raise CampaignFailure(
                "campaign_configuration",
                "campaign report already exists; choose an unused report path to preserve the existing file",
            ) from error
        except OSError as error:
            raise CampaignFailure(
                "artifact_write_failure",
                f"campaign summary or report could not be written: {error}",
            ) from error
