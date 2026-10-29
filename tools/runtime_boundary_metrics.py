"""言語境界の各シナリオで共通に使う時間計測と集計。warmupは結果に含めません。"""

from __future__ import annotations

import statistics
import time
from typing import Callable


def _percentile(values: list[float], fraction: float) -> float:
    """昇順の計測値から、切捨てインデックス方式の分位点を返します。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def measure_operation(
    name: str, operation: Callable[[], None], samples: int
) -> dict[str, object]:
    """warmup後に操作を指定回数実行し、us/opの分位点と平均を返します。"""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    values: list[float] = []
    for _ in range(max(10, samples // 20)):
        operation()
    for _ in range(samples):
        started = time.perf_counter_ns()
        operation()
        values.append((time.perf_counter_ns() - started) / 1000.0)
    return {
        "name": name,
        "unit": "us/op",
        "samples": samples,
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "mean": round(statistics.fmean(values), 3),
    }
