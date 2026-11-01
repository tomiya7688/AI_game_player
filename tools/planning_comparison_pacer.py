"""Common headless fixture pacing; existing SafetyGuard limits remain unchanged."""

import math
import time


class ComparisonPacer:
    def __init__(self, period_seconds=0.125, *, clock=time.monotonic, sleep=time.sleep):
        if (
            isinstance(period_seconds, bool)
            or not isinstance(period_seconds, (int, float))
            or not math.isfinite(period_seconds)
            or not 0.125 <= period_seconds <= 1
        ):
            raise ValueError("fixture pacing must be .125..1 second")
        self.period_seconds = period_seconds
        self.clock, self.sleep = clock, sleep
        self.next_at = clock()

    def wait(self):
        remaining = self.next_at - self.clock()
        if remaining > 0:
            self.sleep(remaining)
        self.next_at = self.clock() + self.period_seconds
