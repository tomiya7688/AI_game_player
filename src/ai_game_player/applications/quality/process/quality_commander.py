from pathlib import Path

from ai_game_player.applications.quality.process.bug_processing import run_bug_checks
from ai_game_player.applications.quality.process.performance_processing import run_performance_checks
from ai_game_player.applications.quality.process.quality_messenger import send_report


def run_performance_quality(
    budgets: dict[str, float],
    output_path: Path | None = None,
) -> dict[str, object]:
    result = run_performance_checks(budgets)
    if output_path is not None:
        send_report(result, output_path)
    return result


def run_bug_quality(
    paths: list[Path],
    output_path: Path | None = None,
) -> dict[str, object]:
    result = run_bug_checks(paths)
    if output_path is not None:
        send_report(result, output_path)
    return result
