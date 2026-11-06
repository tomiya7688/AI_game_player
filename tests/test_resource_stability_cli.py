"""resource_stability CLI・scenario・schemaの試験(Issue #787 T1-T4,T6-T10,T14,T15,T20-T22,T24,T25,T29)。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


from ai_game_player.runtime import resource_stability as rs
from tools import resource_stability_scenarios as sc
from tools import run_resource_stability as cli

HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
REPO = Path(__file__).resolve().parents[1]
REPORT_SCHEMA = json.loads(
    (REPO / "config" / "resource_stability_report.schema.json").read_text(
        encoding="utf-8"
    )
)
SAMPLE_SCHEMA = json.loads(
    (REPO / "config" / "resource_stability_sample.schema.json").read_text(
        encoding="utf-8"
    )
)


def validate_schema(instance: Any, schema: dict[str, Any]) -> None:
    # jsonschemaは開発依存。無い環境ではSchemaTestがskipされ、ここは何もしない
    if HAS_JSONSCHEMA:
        import jsonschema

        jsonschema.validate(instance, schema)


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = cli.load_profile(cli.DEFAULT_PROFILE)
        cls.cfg = cls.profile["layers"]["fast"]["run_profiles"]["fast"]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def report(
        self, name: str, behavior: sc.Behavior | None = None, seed: int = 1
    ) -> dict[str, Any]:
        rows = sc.build_rows(
            name,
            behavior or sc.get_behavior(name),
            self.cfg,
            "fast",
            seed,
            sc.host_profile_id(),
            self.work,
        )
        return rs.evaluate_rows(self.profile, rows, cli.environment_info())

    def test_pass_scenarios_pass_and_measure_all_required(self) -> None:
        for name in sc.SCENARIOS:
            with self.subTest(name):
                report = self.report(name)
                self.assertEqual(report["verdict"], "pass", report["reasons"])
                self.assertEqual(report["coverage"]["unsupported_required"], [])

    def test_every_canary_fails_with_expected_code(self) -> None:
        for name, behavior in sc.CANARIES.items():
            with self.subTest(name):
                report = self.report(name)
                self.assertEqual(report["verdict"], "fail")
                self.assertTrue(
                    set(behavior.expected_codes)
                    <= {r["code"] for r in report["reasons"]},
                    report["reasons"],
                )

    def test_deterministic_and_seed_independent_pass(self) -> None:
        def stable(report: dict[str, Any]) -> dict[str, Any]:
            return {
                k: v
                for k, v in report.items()
                if k not in ("generated_at_utc", "code", "runtime")
            }

        self.assertEqual(stable(self.report("clean")), stable(self.report("clean")))
        self.assertEqual(self.report("clean", seed=99)["verdict"], "pass")

    def test_bounded_cache_pass_but_same_final_value_leak_fails(self) -> None:
        report = self.report("bounded_cache_plateau")
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(
            report["metrics"]["registry.active_leases"]["declared_ceiling"], 400
        )
        leak = self.report(
            "bounded_cache_plateau",
            sc.with_behavior("bounded_cache_plateau", leak_lease_per_step=1),
        )
        self.assertIn("tail_growth", {r["code"] for r in leak["reasons"]})

    def test_pinned_cache_declared_passes_undeclared_or_excess_fails(self) -> None:
        self.assertEqual(self.report("pinned_model_cache")["verdict"], "pass")
        undeclared = self.report(
            "pinned_model_cache",
            sc.with_behavior("pinned_model_cache", declare_pinned=False),
        )
        self.assertIn("not_reclaimed", {r["code"] for r in undeclared["reasons"]})
        excess = self.report(
            "pinned_model_cache", sc.with_behavior("pinned_model_cache", pinned_extra=1)
        )
        self.assertIn("not_reclaimed", {r["code"] for r in excess["reasons"]})

    def test_stop_and_error_clean_pass_but_leaving_resources_is_not_reclaimed(
        self,
    ) -> None:
        for good, bad in (
            ("stop_mid_step_clean", "stop_leaves_resource"),
            ("error_mid_step_clean", "error_leaves_resource"),
        ):
            self.assertEqual(self.report(good)["verdict"], "pass")
            self.assertIn(
                "not_reclaimed", {r["code"] for r in self.report(bad)["reasons"]}
            )

    def test_report_matches_schema_and_has_issue_evidence(self) -> None:
        report = self.report("clean")
        validate_schema(report, REPORT_SCHEMA)
        for key in (
            "runtime",
            "code",
            "host",
            "run",
            "warmup",
            "sampling",
            "metrics",
            "sessions",
            "coverage",
            "reasons",
        ):
            self.assertIn(key, report)
        for metric in report["metrics"].values():
            self.assertIn("source", metric)
        self.assertEqual(report["metrics"]["os.handle_count"]["source"], "synthetic")
        rows = sc.build_rows(
            "clean", sc.get_behavior("clean"), self.cfg, "fast", 1, "linux", self.work
        )
        for row in rows:
            validate_schema(row, SAMPLE_SCHEMA)

    def test_report_and_samples_have_no_environment_values_or_paths(self) -> None:
        report = self.report("clean")
        text = json.dumps(report) + json.dumps(
            sc.build_rows(
                "clean",
                sc.get_behavior("clean"),
                self.cfg,
                "fast",
                1,
                "linux",
                self.work,
            )
        )
        for needle in (
            os.environ.get("USERNAME", ""),
            os.environ.get("USER", ""),
            str(REPO),
            str(REPO).replace("\\", "/"),
            os.environ.get("TEMP", ""),
        ):
            if len(needle) > 3:
                self.assertNotIn(needle, text)
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", text))

    def test_evaluate_round_trip_from_saved_samples(self) -> None:
        rows = sc.build_rows(
            "leak_handle",
            sc.get_behavior("leak_handle"),
            self.cfg,
            "fast",
            1,
            "linux",
            self.work,
        )
        with tempfile.TemporaryDirectory() as tmp:
            samples = Path(tmp) / "s.jsonl"
            cli._write_rows(samples, rows)
            out = Path(tmp) / "r.json"
            code, _ = run_cli(
                "evaluate", "--samples", str(samples), "--output", str(out)
            )
            saved = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        direct = rs.evaluate_rows(self.profile, rows)
        self.assertEqual(saved["verdict"], direct["verdict"])
        self.assertEqual(saved["reasons"], direct["reasons"])
        self.assertEqual(saved["metrics"]["os.handle_count"]["source"], "synthetic")

    def test_real_hardware_samples_use_same_judge_and_required_gpu_blocks(self) -> None:
        rows = sc.build_rows(
            "clean", sc.get_behavior("clean"), self.cfg, "fast", 1, "linux", self.work
        )
        rows[0].update(
            layer="real_hardware", run_profile="deep", scenario="harness_250"
        )
        rows[0]["metrics"]["gpu.process_vram_bytes"] = {
            "source": "external",
            "class": "bytes",
            "unit": "bytes",
            "status": "unsupported",
            "unsupported_reason": "tool_missing",
        }
        for row in rows[1:]:
            if row["kind"] == "sample":
                row["values"]["gpu.process_vram_bytes"] = None
        report = rs.evaluate_rows(
            cli.load_profile(cli.DEFAULT_PROFILE), rows, cli.environment_info()
        )
        validate_schema(report, REPORT_SCHEMA)
        self.assertEqual(report["run"]["layer"], "real_hardware")
        self.assertIn(
            "gpu.process_vram_bytes", report["coverage"]["unsupported_required"]
        )
        self.assertEqual(
            report["metrics"]["gpu.process_vram_bytes"]["unsupported_reason"],
            "tool_missing",
        )

    def test_header_declarations_drive_the_judgement(self) -> None:
        rows = sc.build_rows(
            "pinned_model_cache",
            sc.get_behavior("pinned_model_cache"),
            self.cfg,
            "fast",
            1,
            "linux",
            self.work,
        )
        self.assertEqual(
            rs.evaluate_rows(self.profile, rows, cli.environment_info())["verdict"],
            "pass",
        )
        rows[0]["declarations"]["declared_retained"] = {}
        self.assertEqual(
            rs.evaluate_rows(self.profile, rows, cli.environment_info())["verdict"],
            "fail",
        )


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is a development dependency")
class SchemaTest(unittest.TestCase):
    def test_schemas_are_valid_and_reject_broken_documents(self) -> None:
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(REPORT_SCHEMA)
        jsonschema.Draft202012Validator.check_schema(SAMPLE_SCHEMA)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {"schema": "resource-stability-report/v1"}, REPORT_SCHEMA
            )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"kind": "sample"}, SAMPLE_SCHEMA)
        header = sc.build_rows(
            "clean",
            sc.get_behavior("clean"),
            {
                "sessions": 4,
                "steps_per_session": 4,
                "sample_every_steps": 1,
                "warmup_steps": 0,
            },
            "fast",
            1,
            "linux",
            Path(tempfile.gettempdir()),
        )[0]
        header["metrics"]["os.handle_count"] = {
            "source": "synthetic",
            "class": "count",
            "unit": "count",
            "status": "unsupported",
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(header, SAMPLE_SCHEMA)


class SettleTest(unittest.TestCase):
    class Gauge:
        def __init__(self, values: list[dict[str, int | None]]) -> None:
            self.values = values

        def read(self) -> dict[str, int | None]:
            return self.values.pop(0) if len(self.values) > 1 else self.values[0]

    def test_stable_value_is_returned_without_timeout(self) -> None:
        gauge = self.Gauge(
            [{"temp.file_count": 9}, {"temp.file_count": 1}, {"temp.file_count": 1}]
        )
        value, timeout = cli._settle(gauge, 5.0)  # type: ignore[arg-type]
        self.assertEqual((value, timeout), ({"temp.file_count": 1}, False))

    def test_unstable_value_times_out_and_is_still_returned(self) -> None:
        counter = iter(range(100))

        class Moving:
            def read(self) -> dict[str, int | None]:
                return {"temp.file_count": next(counter)}

        value, timeout = cli._settle(Moving(), 0.35)  # type: ignore[arg-type]
        self.assertTrue(timeout)
        self.assertIsNotNone(value["temp.file_count"])


class SettleMissingValueTest(unittest.TestCase):
    def test_missing_values_are_not_a_stable_reading(self) -> None:
        readings = [
            {"temp.file_count": None},
            {"temp.file_count": None},
            {"temp.file_count": 2},
            {"temp.file_count": 2},
        ]

        class Recovering:
            def read(self) -> dict[str, int | None]:
                return readings.pop(0) if len(readings) > 1 else readings[0]

        value, timeout = cli._settle(Recovering(), 5.0)  # type: ignore[arg-type]
        self.assertEqual((value, timeout), ({"temp.file_count": 2}, False))

    def test_persistent_missing_values_time_out_as_missing(self) -> None:
        class Broken:
            def read(self) -> dict[str, int | None]:
                return {"temp.file_count": None}

        value, timeout = cli._settle(Broken(), 0.3)  # type: ignore[arg-type]
        self.assertEqual((value, timeout), ({"temp.file_count": None}, True))


class CliContractTest(unittest.TestCase):
    def test_exit_codes_for_invalid_inputs(self) -> None:
        self.assertEqual(run_cli("run", "--layer", "fast", "--run-profile", "pr")[0], 2)
        self.assertEqual(
            run_cli(
                "run", "--layer", "fast", "--run-profile", "fast", "--scenario", "nope"
            )[0],
            2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            bad = json.loads(cli.DEFAULT_PROFILE.read_text(encoding="utf-8"))
            bad["enforcement"]["fast"] = "observe"
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(
                run_cli(
                    "run",
                    "--layer",
                    "fast",
                    "--run-profile",
                    "fast",
                    "--profile-file",
                    str(path),
                )[0],
                2,
            )
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            self.assertEqual(
                run_cli(
                    "evaluate",
                    "--samples",
                    str(empty),
                    "--output",
                    str(Path(tmp) / "o.json"),
                )[0],
                2,
            )

    def test_no_relaxation_arguments_exist(self) -> None:
        with self.assertRaises(SystemExit):
            run_cli(
                "run", "--layer", "fast", "--run-profile", "fast", "--mode", "observe"
            )
        help_text = (
            cli._build_parser().format_help()
            + cli._build_parser()
            ._subparsers._group_actions[0]
            .choices["run"]
            .format_help()
        )
        self.assertNotIn("multiplier", help_text.lower())
        self.assertNotIn("--mode", help_text)
        self.assertNotIn("--budget", help_text)

    def test_list_scenarios_lists_canaries(self) -> None:
        code, out = run_cli("list-scenarios")
        self.assertEqual(code, 0)
        self.assertIn("canary\tleak_lease_per_step", out)
        self.assertIn("pass\tclean", out)

    def test_run_fast_writes_reports_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            code, text = run_cli(
                "run",
                "--layer",
                "fast",
                "--run-profile",
                "fast",
                "--scenario",
                "clean",
                "--scenario",
                "leak_lease_per_step",
                "--output",
                str(out),
                "--samples",
                str(Path(tmp) / "s.jsonl"),
            )
            self.assertEqual(code, 0, text)
            self.assertEqual(
                json.loads(out.read_text(encoding="utf-8"))["verdict"], "pass"
            )
            suite = json.loads((Path(tmp) / "suite.json").read_text(encoding="utf-8"))
            self.assertTrue(suite["ok"])

    def test_loosened_judge_is_reported_as_canary_not_detected(self) -> None:
        profile = cli.load_profile(cli.DEFAULT_PROFILE)
        real_judge = rs.evaluate_run

        def lenient(*args: Any, **kwargs: Any) -> dict[str, Any]:
            report = real_judge(*args, **kwargs)
            return {**report, "verdict": "pass", "reasons": []}

        with tempfile.TemporaryDirectory() as tmp:
            suite, _ = cli.run_fast(
                profile, "fast", ["leak_handle"], 1, Path(tmp), judge=lenient
            )
        self.assertFalse(suite["ok"])
        self.assertEqual(suite["scenarios"][0]["code"], "canary_not_detected")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rs, "evaluate_run", lenient),
        ):
            code, _ = run_cli(
                "run",
                "--layer",
                "fast",
                "--run-profile",
                "fast",
                "--scenario",
                "leak_handle",
                "--output",
                str(Path(tmp) / "r.json"),
                "--samples",
                str(Path(tmp) / "s.jsonl"),
            )
        self.assertEqual(code, 1)

    def test_exit_code_priority_and_observe_mode(self) -> None:
        fail = {
            "verdict": "fail",
            "reasons": [{"code": "tail_growth"}],
            "run": {"gate_effective": False},
        }
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli._exit_for(fail, 0), 0)
            self.assertIn("::warning::", buf.getvalue())
        self.assertEqual(cli._exit_for(fail, 3), 1)  # 観測対象の失敗はobserveでも1
        self.assertEqual(cli._exit_for({**fail, "run": {"gate_effective": True}}, 0), 1)
        self.assertEqual(cli._exit_for({**fail, "verdict": "pass"}, 0), 0)


@unittest.skipUnless(
    sys.platform == "win32", "integration tree observation is verified on Windows"
)
class IntegrationObservationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = cli.load_profile(cli.DEFAULT_PROFILE)
        # 短い試験用の実行profile(hard limitは変えない)。32標本 = 8秒 + warmup 1秒
        cfg = cls.profile["layers"]["integration"]["run_profiles"]["pr"]
        cfg.update(warmup_seconds=1.0, interval_seconds=0.25)
        cls.cfg = cfg
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def observe(self, script: str, *args: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.work / "child.py"
        path.write_text(textwrap.dedent(script), encoding="utf-8")
        rows, exit_code = cli.observe_command(
            self.cfg, "pr", [sys.executable, str(path), *args], self.work, 1, timeout=60
        )
        for row in rows:
            validate_schema(row, SAMPLE_SCHEMA)
        report = rs.evaluate_rows(self.profile, rows, cli.environment_info())
        validate_schema(report, REPORT_SCHEMA)
        self.assertEqual(exit_code, 0)
        return report, rows[-1]

    def test_steady_child_passes(self) -> None:
        report, _ = self.observe("""
            import time
            end = time.time() + 14
            while time.time() < end:
                time.sleep(0.1)
        """)
        self.assertEqual(report["verdict"], "pass", report["reasons"])
        self.assertEqual(report["metrics"]["os.handle_count"]["source"], "external")

    def test_handle_leaking_child_fails_and_observe_mode_exits_zero(self) -> None:
        report, _ = self.observe("""
            import os, time
            held = []
            end = time.time() + 14
            while time.time() < end:
                held += [open(os.devnull, "rb") for _ in range(3)]
                time.sleep(0.2)
        """)
        self.assertIn(
            "tail_growth",
            {
                r["code"]
                for r in report["reasons"]
                if r.get("metric") == "os.handle_count"
            },
        )
        self.assertFalse(report["run"]["gate_effective"])
        self.assertEqual(report["run"]["enforcement"], "observe")
        self.assertEqual(cli._exit_for(report, 0), 0)
        enforcing = {**report, "run": {**report["run"], "gate_effective": True}}
        self.assertEqual(cli._exit_for(enforcing, 0), 1)

    def test_descendant_left_after_exit_is_orphan(self) -> None:
        pid_file = self.work / "orphan.pid"
        script = """
            import subprocess, sys, time
            grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
            open(sys.argv[1], "w").write(str(grandchild.pid))
            time.sleep(3)
        """
        try:
            report, footer = self.observe(script, str(pid_file))
        finally:
            if pid_file.exists():
                with contextlib.suppress(OSError):
                    os.kill(int(pid_file.read_text()), 15)
        self.assertGreaterEqual(footer["orphan_count"], 1)
        self.assertIn("orphan_process", {r["code"] for r in report["reasons"]})


if __name__ == "__main__":
    unittest.main()
