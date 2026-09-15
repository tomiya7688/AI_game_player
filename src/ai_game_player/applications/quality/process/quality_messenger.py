from pathlib import Path

from ai_game_player.applications.quality.data.quality_messenger import receive_report


def send_report(report: dict[str, object], output_path: Path) -> None:
    receive_report(report, output_path)
