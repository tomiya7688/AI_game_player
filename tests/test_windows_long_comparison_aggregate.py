import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools import aggregate_2048_windows_long as aggregator
from tools import compare_2048_windows_long as runner
from tools import windows_long_comparison_stats as stats

COMMIT = "c" * 40
SEEDS = list(range(479, 487))


def episode(score, terminal=True, actions=20, status="completed"):
    return {
        "episode": 1,
        "seed": 479,
        "episode_status": status,
        "terminal": terminal,
        "stop_reason": None,
        "actions": actions,
        "attempts": actions + 1,
        "holds": 1,
        "invalid_no_op_actions": 0,
        "board_changes": actions,
        "policy_reobservations": 0,
        "nodes": None,
        "score": score,
        "max_tile": 64,
        "latency_ms": [1.0, 2.0],
        "resources": None,
        "wall_seconds": 10.0,
        "seed_fixture": {"seed": 479},
        "planner_readiness": None,
        "failure_stage": None,
        "failure": None,
        "cleanup_failure": None,
        "stopped_input_rejected": True,
        "stale_generation_rejected": True,
    }


def run_report(provider, seed, score, status="completed", actions=20):
    episodes = [
        episode(
            score,
            actions=actions,
            status="completed" if status == "completed" else "failed",
        )
    ]
    report = {
        "schema": runner.RUN_SCHEMA,
        "track": runner.TRACK,
        "provider": provider,
        "base_seed": seed,
        "provider_order_index": 0,
        "episodes": episodes,
    }
    runner.finalize_report(report, "wall_budget_reached")
    if status == "failed":
        report["run_status"] = "failed"
    return report


def block(seed, scores, *, fingerprint="f" * 64, statuses=None, **overrides):
    statuses = statuses or {}
    data = {
        "schema": runner.BLOCK_SCHEMA,
        "track": runner.TRACK,
        "ref_class": "manual",
        "git_commit": COMMIT,
        "base_seed": seed,
        "block_index": seed - 479,
        "provider_order": list(runner.PROVIDERS),
        "condition_fingerprint": fingerprint,
        "hardware": {"cpu_model": "x"},
        "budget": runner.budget_record(),
        "consistency": {"violations": []},
        "block_status": "valid",
        "runs": {
            p: run_report(p, seed, scores[p], statuses.get(p, "completed"))
            for p in runner.PROVIDERS
        },
    }
    # 実際のRunnerと同じく、各runはblockと同じ条件(fingerprint・hardware・budget)を持つ。
    for run in data["runs"].values():
        run.update(
            condition_fingerprint=data["condition_fingerprint"],
            hardware=data["hardware"],
            budget=data["budget"],
        )
    data.update(overrides)
    return data


def manifest(seed_count=8):
    return aggregator.build_manifest(479, seed_count, "manual", COMMIT)


def write_blocks(directory, blocks):
    for data in blocks:
        path = (
            Path(directory)
            / f"windows-long-block-{data['base_seed']}"
            / "block-report.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def good_scores(seed):
    offset = seed - 479
    return {
        "cycling": 1000 + offset,
        "baseline": 1100 + 2 * offset,
        "planner": 1500 + 3 * offset,
    }


class AggregateTests(unittest.TestCase):
    def aggregate(self, blocks, seed_count=8):
        with tempfile.TemporaryDirectory() as directory:
            write_blocks(directory, blocks)
            return aggregator.aggregate(manifest(seed_count), Path(directory))

    def test_complete_accounting_with_all_completed_exits_zero_and_gives_verdict(self):
        report, code = self.aggregate([block(s, good_scores(s)) for s in SEEDS])
        self.assertEqual(code, 0)
        primary = report["statistics"]["contrasts"]["planner-cycling"]["primary"]
        self.assertEqual((primary["n_pairs"], primary["verdict"]), (8, "better"))
        self.assertEqual(len(report["comparison_table"]), 24)
        self.assertIn("planner-cycling", aggregator.render_markdown(report))

    def test_missing_artifact_stays_in_the_denominator_as_missing(self):
        report, code = self.aggregate([block(s, good_scores(s)) for s in SEEDS[:-1]])
        self.assertEqual(code, 2)
        rows = [r for r in report["comparison_table"] if r["seed"] == SEEDS[-1]]
        self.assertEqual({r["status"] for r in rows}, {"missing"})
        self.assertEqual(report["run_counts"]["planner"]["missing"], 1)
        primary = report["statistics"]["contrasts"]["planner-cycling"]["primary"]
        self.assertEqual((primary["n_pairs"], primary["n_excluded_unknown"]), (7, 1))
        self.assertEqual(primary["verdict"], "inconclusive")

    def test_partial_and_failed_with_input_are_in_primary_and_completed_only_sensitivity_is_separate(
        self,
    ):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        blocks[0] = block(
            SEEDS[0], good_scores(SEEDS[0]), statuses={"planner": "failed"}
        )
        report, code = self.aggregate(blocks)
        self.assertEqual(code, 2)
        contrast = report["statistics"]["contrasts"]["planner-cycling"]
        self.assertEqual(contrast["primary"]["n_pairs"], 8)
        self.assertEqual(contrast["sensitivity_completed_only"]["n_pairs"], 7)
        self.assertNotIn("verdict", contrast["sensitivity_completed_only"])
        self.assertNotIn("verdict", contrast["sensitivity_missing_score_as_zero"])

    def test_unknown_score_pairs_are_excluded_and_zero_imputation_is_sensitivity_only(
        self,
    ):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        blocks[0]["runs"]["planner"] = run_report("planner", SEEDS[0], None)
        blocks[0]["runs"]["planner"].update(
            condition_fingerprint=blocks[0]["condition_fingerprint"],
            hardware=blocks[0]["hardware"],
            budget=blocks[0]["budget"],
        )
        report, _ = self.aggregate(blocks)
        contrast = report["statistics"]["contrasts"]["planner-cycling"]
        self.assertEqual(contrast["primary"]["n_pairs"], 7)
        self.assertEqual(contrast["primary"]["n_excluded_unknown"], 1)
        self.assertEqual(contrast["sensitivity_missing_score_as_zero"]["n_pairs"], 8)
        self.assertEqual(
            contrast["sensitivity_missing_score_as_zero"]["n_imputed_zero"], 1
        )

    def test_condition_fingerprint_mismatch_is_a_contract_violation_without_statistics(
        self,
    ):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        blocks[3] = block(SEEDS[3], good_scores(SEEDS[3]), fingerprint="0" * 64)
        report, code = self.aggregate(blocks)
        self.assertEqual(code, 1)
        self.assertIsNone(report["statistics"])
        row = next(r for r in report["comparison_table"] if r["seed"] == SEEDS[3])
        self.assertEqual(row["status"], "invalid")

    def test_mixed_track_ref_class_or_commit_is_rejected(self):
        for key, value in (
            ("track", "windows_short"),
            ("ref_class", "pr"),
            ("git_commit", "d" * 40),
        ):
            blocks = [block(s, good_scores(s)) for s in SEEDS]
            blocks[0] = block(SEEDS[0], good_scores(SEEDS[0]), **{key: value})
            report, code = self.aggregate(blocks)
            self.assertEqual(code, 1, key)
            self.assertIsNone(report["statistics"])

    def test_block_that_failed_its_own_hardware_check_is_invalid(self):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        blocks[1] = block(
            SEEDS[1],
            good_scores(SEEDS[1]),
            block_status="invalid",
            consistency={"violations": ["hardware_equal"]},
        )
        report, code = self.aggregate(blocks)
        self.assertEqual(code, 1)
        self.assertIsNone(report["statistics"])

    def test_block_header_and_run_contract_gaps_are_invalid_not_a_crash(self):
        def corrupt(mutate):
            blocks = [block(s, good_scores(s)) for s in SEEDS]
            mutate(blocks[0])
            report, code = self.aggregate(blocks)
            self.assertEqual(code, 1)
            self.assertIsNone(report["statistics"])
            first = next(r for r in report["comparison_table"] if r["seed"] == SEEDS[0])
            self.assertEqual(first["status"], "invalid")

        corrupt(lambda b: b.pop("condition_fingerprint"))
        corrupt(lambda b: b.update(consistency={"violations": ["hardware_equal"]}))
        corrupt(lambda b: b["consistency"].pop("violations"))
        corrupt(lambda b: b["runs"]["planner"].update(condition_fingerprint="0" * 64))
        corrupt(lambda b: b["runs"]["planner"].update(hardware={"cpu_model": "other"}))
        corrupt(lambda b: b["runs"]["cycling"]["metrics"].pop("episode_mean_score"))
        corrupt(lambda b: b["runs"]["cycling"]["metrics"].update(actions_total="12"))
        corrupt(lambda b: b["runs"]["cycling"]["episodes"][0].pop("latency_ms"))

    def test_mixed_runner_images_never_produce_a_verdict(self):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        for index, data in enumerate(blocks):
            image = "20251101.1" if index < 4 else "20251201.1"
            data["hardware"] = {**data["hardware"], "image_version": image}
            for run in data["runs"].values():
                run["hardware"] = data["hardware"]
        report, code = self.aggregate(blocks)
        self.assertEqual(code, 0)
        self.assertTrue(report["statistics"]["mixed_runner_images"])
        for contrast in report["statistics"]["contrasts"].values():
            self.assertNotIn(contrast["primary"]["verdict"], ("better", "worse"))
        self.assertIn("runner image", aggregator.render_markdown(report))

    def test_block_outside_the_manifest_or_duplicated_is_a_violation(self):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        _, code = self.aggregate([*blocks, block(9999, good_scores(0))])
        self.assertEqual(code, 1)
        with tempfile.TemporaryDirectory() as directory:
            write_blocks(directory, blocks)
            copy = Path(directory) / "other" / "block-report.json"
            copy.parent.mkdir()
            copy.write_text(json.dumps(blocks[0]), encoding="utf-8")
            _, code = aggregator.aggregate(manifest(), Path(directory))
        self.assertEqual(code, 1)

    def test_measured_metric_with_null_value_is_schema_violation(self):
        blocks = [block(s, good_scores(s)) for s in SEEDS]
        blocks[0]["runs"]["cycling"]["metrics"]["max_tile"] = {
            "value": None,
            "status": "measured",
        }
        _, code = self.aggregate(blocks)
        self.assertEqual(code, 1)

    def test_no_artifacts_at_all_is_a_contract_violation(self):
        report, code = self.aggregate([])
        self.assertEqual(code, 1)
        self.assertEqual({r["status"] for r in report["comparison_table"]}, {"missing"})

    def test_three_blocks_report_insufficient_samples_without_verdict_claims(self):
        report, code = self.aggregate(
            [block(s, good_scores(s)) for s in SEEDS[:3]], seed_count=3
        )
        self.assertEqual(code, 0)
        primary = report["statistics"]["contrasts"]["planner-cycling"]["primary"]
        self.assertEqual(
            (primary["status"], primary["verdict"]),
            ("insufficient_samples", "inconclusive"),
        )
        self.assertIsNone(primary["interval"])
        self.assertNotIn("高性能", aggregator.render_markdown(report))

    def test_provider_summary_has_wilson_interval_and_order_counts(self):
        report, _ = self.aggregate([block(s, good_scores(s)) for s in SEEDS])
        summary = report["statistics"]["provider_summary"]["cycling"]
        self.assertEqual(summary["completed_rate"]["successes"], 8)
        self.assertEqual(summary["metrics"]["episode_mean_score"]["n"], 8)
        self.assertEqual(summary["latency_pooled"]["samples"], 16)
        self.assertEqual(summary["metrics"]["nodes"]["n"], 0)


class ManifestTests(unittest.TestCase):
    def test_manifest_validation_and_ranges(self):
        self.assertEqual(aggregator.manifest_problems(manifest()), [])
        self.assertEqual(len(manifest()["base_seeds"]), 8)
        for count in (0, 33):
            with self.assertRaises(ValueError):
                aggregator.build_manifest(479, count, "manual", COMMIT)
        with self.assertRaises(ValueError):
            aggregator.build_manifest(0xFFFFFFFF, 2, "manual", COMMIT)
        with self.assertRaises(ValueError):
            aggregator.build_manifest(479, 8, "release", COMMIT)
        with self.assertRaises(ValueError):
            aggregator.build_manifest(479, 8, "manual", "HEAD")
        bad = {**manifest(), "base_seeds": [1, 1]}
        self.assertTrue(aggregator.manifest_problems(bad))

    def test_cli_plan_then_aggregate_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = aggregator.main(
                    [
                        "plan",
                        "--seed-count",
                        "8",
                        "--ref-class",
                        "manual",
                        "--git-commit",
                        COMMIT,
                        "--output",
                        str(base / "plan" / "manifest.json"),
                    ]
                )
            self.assertEqual(code, 0)
            matrix = json.loads(captured.getvalue())
            self.assertEqual([i["index"] for i in matrix["include"]], list(range(8)))
            write_blocks(base / "artifacts", [block(s, good_scores(s)) for s in SEEDS])
            with contextlib.redirect_stdout(io.StringIO()):
                code = aggregator.main(
                    [
                        "aggregate",
                        "--manifest",
                        str(base / "plan" / "manifest.json"),
                        "--artifacts",
                        str(base / "artifacts"),
                        "--output",
                        str(base / "out"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue((base / "out" / "report.json").is_file())
            self.assertTrue((base / "out" / "report.md").is_file())

    def test_cli_rejects_an_invalid_plan_and_an_invalid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    aggregator.main(
                        [
                            "plan",
                            "--seed-count",
                            "99",
                            "--git-commit",
                            COMMIT,
                            "--output",
                            str(base / "m.json"),
                        ]
                    ),
                    1,
                )
                (base / "m.json").write_text("{}", encoding="utf-8")
                self.assertEqual(
                    aggregator.main(
                        [
                            "aggregate",
                            "--manifest",
                            str(base / "m.json"),
                            "--artifacts",
                            str(base),
                            "--output",
                            str(base / "o"),
                        ]
                    ),
                    1,
                )

    def test_primary_threshold_is_not_reachable_from_the_cli(self):
        parser_help = io.StringIO()
        with contextlib.redirect_stdout(parser_help), self.assertRaises(SystemExit):
            aggregator.main(["aggregate", "--help"])
        for word in ("alpha", "resample", "n-min", "threshold"):
            self.assertNotIn(word, parser_help.getvalue())
        self.assertEqual(aggregator.N_MIN_VERDICT, stats.N_MIN_VERDICT)


if __name__ == "__main__":
    unittest.main()
