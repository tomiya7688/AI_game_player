from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "check_gpu_vram_budget.py"
SPEC = importlib.util.spec_from_file_location("check_gpu_vram_budget", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeProcess:
    def __init__(self, polls: list[int | None], returncode: int = 0) -> None:
        self._polls = iter(polls)
        self.returncode = returncode

    def poll(self):
        try:
            value = next(self._polls)
        except StopIteration:
            value = self.returncode
        if value is not None:
            self.returncode = value
        return value


class VramBudgetTest(unittest.TestCase):
    def test_parse_memory_used_mib(self):
        self.assertEqual(MODULE.parse_memory_used_mib("1024\n2048\n"), [1024, 2048])
        self.assertIsNone(MODULE.parse_memory_used_mib("N/A\n"))

    @patch.object(MODULE.time, "sleep", lambda _: None)
    @patch.object(MODULE.subprocess, "Popen")
    @patch.object(MODULE, "query_total_vram_used_mib")
    def test_fails_when_peak_increment_exceeds_limit(self, query, popen):
        query.side_effect = [1000, 5000, 7200, 7100]
        popen.return_value = FakeProcess([None, None, 0])

        exit_code, report = MODULE.run_with_vram_budget(
            ["fake-command"],
            limit_mib=6144,
            sample_seconds=0.01,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["peak_increment_mib"], 6200)
        self.assertTrue(report["budget_exceeded"])

    @patch.object(MODULE.time, "sleep", lambda _: None)
    @patch.object(MODULE.subprocess, "Popen")
    @patch.object(MODULE, "query_total_vram_used_mib")
    def test_measurement_unavailable_does_not_mask_functional_success(self, query, popen):
        query.return_value = None
        popen.return_value = FakeProcess([0])

        exit_code, report = MODULE.run_with_vram_budget(
            ["fake-command"],
            limit_mib=6144,
            sample_seconds=0.01,
        )

        self.assertEqual(exit_code, 0)
        self.assertFalse(report["measurement_available"])
        self.assertIsNone(report["peak_increment_mib"])


if __name__ == "__main__":
    unittest.main()
