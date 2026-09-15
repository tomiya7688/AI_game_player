from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_game_player.applications.quality.process.quality_commander import run_performance_quality


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AI game player CPU hot-path performance budgets")
    parser.add_argument("--budgets", type=Path, default=Path("config/performance_budgets.json"))
    parser.add_argument("--output", type=Path, default=Path("build/performance/report.json"))
    parser.add_argument("--budget-multiplier", type=float, default=1.0)
    args = parser.parse_args()
    if args.budget_multiplier <= 0:
        raise ValueError("budget multiplier must be positive")
    raw = json.loads(args.budgets.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("performance budget file must contain an object")
    budgets = {
        str(name): float(value) * args.budget_multiplier
        for name, value in raw.items()
        if isinstance(value, (int, float))
    }
    report = run_performance_quality(budgets, args.output)
    for case in report["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        print(
            f"{status} {case['name']}: p95-batch/op={case['p95_batch_ms_per_op']:.6f}ms "
            f"budget={case['budget_ms']:.6f}ms"
        )
    if report["passed"]:
        print("PERFORMANCE OK")
        return 0
    print("PERFORMANCE REGRESSION: " + ", ".join(report["failed_cases"]), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
