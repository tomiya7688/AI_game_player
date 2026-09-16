from pathlib import Path

from ai_game_player.applications.quality.data.quality_commander import save_report


def receive_report(report: dict[str, object], output_path: Path) -> None:
    save_report(report, output_path)
