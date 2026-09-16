from pathlib import Path

from ai_game_player.applications.quality.data.report_processing import save_json_report


def save_report(report: dict[str, object], output_path: Path) -> None:
    save_json_report(report, output_path)
