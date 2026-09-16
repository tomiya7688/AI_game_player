from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_game_player.applications.quality.process.quality_commander import run_bug_quality


def main() -> int:
    parser = argparse.ArgumentParser(description="Run high-confidence static bug checks")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path("src"), Path("tools"), Path("tests")],
    )
    parser.add_argument("--output", type=Path, default=Path("build/bug-check/report.json"))
    args = parser.parse_args()
    report = run_bug_quality(list(args.paths), args.output)
    for finding in report["findings"]:
        print(f"E {finding['code']} {finding['path']}:{finding['line']} {finding['message']}")
    if report["passed"]:
        print(f"BUG CHECK OK files={report['scanned_files']}")
        return 0
    print(f"BUG CHECK FAILED findings={len(report['findings'])}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
