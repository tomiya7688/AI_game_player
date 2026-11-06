"""resource_stability(判定core)の試験(Issue #787 T2-T13,T17,T18,T19,T28,T30)。"""

from __future__ import annotations

import copy
import json
import math
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_game_player.runtime import resource_stability as rs

PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "resource_stability_profile.json"
)
FAST_IDS = (
    "registry.active_leases",
    "registry.releasing_leases",
    "registry.failed_leases",
    "os.handle_count",
    "os.gdi_objects",
    "os.user_objects",
    "temp.file_count",
    "registry.max_cleanup_latency_ms",
)
BASE = {"os.handle_count": 100, "registry.max_cleanup_latency_ms": 10}
ValueFn = Callable[[int, int, str], float | None]


def load_profile() -> dict[str, Any]:
    return rs.validate_profile(json.loads(PROFILE_PATH.read_text(encoding="utf-8")))


# {
#   責務: [make_rows: 試験用のheader+sample行を作る(値は関数で与える)]
#   処理: [Sessionごとにpre_open・steps・closing・post_close行を並べる]
#   引数: [value_fn: (session, step, metric)->値, unsupported: 測れないmetricとreason, declarations: headerの宣言]
#   戻り値: [行の辞書列(footerなし)]
# }
def make_rows(
    value_fn: ValueFn | None = None,
    *,
    sessions: int = 5,
    steps: int = 1000,
    every: int = 10,
    ids: tuple[str, ...] = FAST_IDS,
    unsupported: dict[str, str] | None = None,
    declarations: dict[str, Any] | None = None,
    layer: str = "fast",
    run_profile: str = "fast",
    host: str = "linux",
) -> list[dict[str, Any]]:
    unsupported = unsupported or {}
    metrics = {}
    for metric in ids:
        unit, cls = rs.metric_traits(metric) or ("count", "count")
        info = {"source": "synthetic", "class": cls, "unit": unit, "status": "measured"}
        if metric in unsupported:
            info.update(status="unsupported", unsupported_reason=unsupported[metric])
        metrics[metric] = info
    decl = {
        "declared_retained": {},
        "declared_ceiling": {},
        "declared_fill_steps": 0,
        "exclusive_gpu": False,
    }
    decl.update(declarations or {})
    rows: list[dict[str, Any]] = [
        {
            "schema": rs.SAMPLE_SCHEMA,
            "kind": "header",
            "run_id": "t",
            "layer": layer,
            "run_profile": run_profile,
            "host_profile": host,
            "scenario": "test",
            "seed": 1,
            "metrics": metrics,
            "declarations": decl,
        }
    ]
    fn = value_fn or (lambda s, st, m: BASE.get(m, 0))
    clock = 0

    def add(session: int, phase: str, step: int) -> None:
        values = {m: (None if m in unsupported else fn(session, step, m)) for m in ids}
        rows.append(
            {
                "schema": rs.SAMPLE_SCHEMA,
                "kind": "sample",
                "run_id": "t",
                "session_index": session,
                "phase": phase,
                "step": step,
                "t_monotonic_ns": clock,
                "values": values,
            }
        )

    for session in range(sessions):
        add(session, "pre_open", 0)
        for step in range(steps):
            clock += 1_000_000
            if step % every == every - 1:
                add(session, "warmup" if step < 500 else "steady", step)
        add(session, "closing", steps)
        add(session, "post_close", steps + 1)
    return rows


def evaluate(
    rows: list[dict[str, Any]], profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    return rs.evaluate_rows(profile or load_profile(), rows)


def codes(report: dict[str, Any], metric: str | None = None) -> set[str]:
    return {
        r["code"]
        for r in report["reasons"]
        if metric is None or r.get("metric") == metric
    }


class StatisticsTest(unittest.TestCase):
    def test_nearest_rank_and_theil_sen(self) -> None:
        self.assertEqual(rs.nearest_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95), 10)
        self.assertEqual(rs.nearest_rank([3, 1, 2], 0.5), 2)
        self.assertTrue(math.isnan(rs.nearest_rank([], 0.5)))
        self.assertAlmostEqual(rs.theil_sen_slope([0, 1, 2, 3], [0, 2, 4, 6]), 2.0)
        self.assertTrue(math.isnan(rs.theil_sen_slope([1], [1])))


class CleanAndDeterminismTest(unittest.TestCase):
    def test_clean_passes_with_all_required_measured(self) -> None:
        report = evaluate(make_rows())
        self.assertEqual(report["verdict"], "pass", report["reasons"])
        for metric in ("registry.active_leases", "os.handle_count", "temp.file_count"):
            self.assertEqual(report["metrics"][metric]["status"], "measured")
            self.assertEqual(report["metrics"][metric]["verdict"], "pass")

    def test_same_samples_give_same_report(self) -> None:
        self.assertEqual(evaluate(make_rows()), evaluate(make_rows()))


class GrowthJudgementTest(unittest.TestCase):
    def check(
        self, fn: ValueFn, metric: str = "os.handle_count"
    ) -> tuple[dict[str, Any], set[str]]:
        report = evaluate(make_rows(fn))
        return report, codes(report, metric)

    def test_persistent_small_leak_is_detected(self) -> None:
        # 10stepごとに1つ解放しない(Session内の後半の傾きが正)
        def leak(session: int, step: int, metric: str) -> float | None:
            return (
                100 + (session * 1000 + step) // 10
                if metric == "os.handle_count"
                else BASE.get(metric, 0)
            )

        _, found = self.check(leak)
        self.assertIn("tail_growth", found)

    def test_leak_inside_a_session_released_at_close_is_detected(self) -> None:
        # 全Sessionで同じ鋸歯状に増えるため、Sessionを連結すると区間中央値が揃って見逃す
        def sawtooth(session: int, step: int, metric: str) -> float | None:
            if metric == "os.handle_count":
                return 100 + (step // 10 if 0 < step <= 1000 else 0)
            return BASE.get(metric, 0)

        report, found = self.check(sawtooth)
        self.assertIn("tail_growth", found)
        self.assertNotIn("not_reclaimed", found)  # closeで回収されても成長は検出する

    def test_single_spike_is_not_growth_but_ceiling_applies(self) -> None:
        def spike(session: int, step: int, metric: str) -> float | None:
            if metric == "os.handle_count" and session == 4 and step == 799:
                return 100 + 160  # 許容(16)の10倍
            return BASE.get(metric, 0)

        report, found = self.check(spike)
        self.assertNotIn("tail_growth", found)
        self.assertEqual(report["metrics"]["os.handle_count"]["peak"], 260)
        # 宣言した上限があればpeakで落ちる
        rows = make_rows(
            spike, declarations={"declared_ceiling": {"os.handle_count": 200}}
        )
        self.assertIn("ceiling_exceeded", codes(evaluate(rows), "os.handle_count"))

    def test_stepwise_increase_in_second_half_is_detected(self) -> None:
        def stair(session: int, step: int, metric: str) -> float | None:
            return (
                100 + (60 if step >= 800 else 0)
                if metric == "os.handle_count"
                else BASE.get(metric, 0)
            )

        _, found = self.check(stair)
        self.assertIn("tail_growth", found)

    def test_warmup_and_declared_fill_do_not_count_as_growth(self) -> None:
        def fill(session: int, step: int, metric: str) -> float | None:
            return (
                100 + min(step, 300) // 10 * 5
                if metric == "os.handle_count"
                else BASE.get(metric, 0)
            )

        report = evaluate(make_rows(fill, declarations={"declared_fill_steps": 300}))
        self.assertNotIn("tail_growth", codes(report, "os.handle_count"))
        self.assertEqual(report["warmup"]["boundary_step"], 500)


class RecoveryAndBaselineTest(unittest.TestCase):
    def test_lease_left_after_close_is_not_reclaimed(self) -> None:
        def left(session: int, step: int, metric: str) -> float | None:
            return (
                1
                if metric == "registry.active_leases" and step == 1001 and session == 2
                else BASE.get(metric, 0)
            )

        self.assertIn(
            "not_reclaimed", codes(evaluate(make_rows(left)), "registry.active_leases")
        )

    def test_declared_retained_is_subtracted_but_excess_fails(self) -> None:
        def held(count: int) -> ValueFn:
            # 保持はSession 0のopen後に生じる(pre_openでは未保持)
            return lambda s, st, m: (
                count
                if m == "registry.active_leases" and (s, st) != (0, 0)
                else BASE.get(m, 0)
            )

        declared = {"declared_retained": {"registry.active_leases": 1}}
        self.assertEqual(
            evaluate(make_rows(held(1), declarations=declared))["verdict"], "pass"
        )
        self.assertIn(
            "not_reclaimed", codes(evaluate(make_rows(held(2), declarations=declared)))
        )
        self.assertIn("not_reclaimed", codes(evaluate(make_rows(held(1)))))

    def test_prime_session_growth_is_reported_not_judged(self) -> None:
        def prime(session: int, step: int, metric: str) -> float | None:
            if metric == "os.handle_count":
                return 100 if session == 0 and step < 1001 else 200
            return BASE.get(metric, 0)

        report = evaluate(make_rows(prime))
        self.assertNotIn("not_reclaimed", codes(report, "os.handle_count"))
        self.assertEqual(
            report["sessions"][0]["first_session_growth"]["os.handle_count"], 100
        )

    def test_baseline_staircase_across_sessions(self) -> None:
        def stair(session: int, step: int, metric: str) -> float | None:
            if metric == "os.handle_count":
                return 100 + 4 * (session + (1 if step > 1000 else 0))
            return BASE.get(metric, 0)

        report = evaluate(make_rows(stair))
        self.assertIn("baseline_staircase", codes(report, "os.handle_count"))
        self.assertEqual(report["baseline_drift"]["os.handle_count"]["verdict"], "fail")


class ExitAndSamplingTest(unittest.TestCase):
    def footer(self, **fields: Any) -> dict[str, Any]:
        return {"schema": rs.SAMPLE_SCHEMA, "kind": "footer", "run_id": "t", **fields}

    def test_nonzero_command_exit_is_a_scenario_error_in_the_report(self) -> None:
        report = evaluate([*make_rows(), self.footer(command_exit_code=3)])
        self.assertIn("scenario_error", codes(report))
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["run"]["command_exit_code"], 3)

    def test_sampling_limit_reached_is_reported(self) -> None:
        report = evaluate([*make_rows(), self.footer(sampling_limit_reached=True)])
        self.assertIn("scenario_error", codes(report))

    def test_unknown_step_count_is_null_not_the_sample_count(self) -> None:
        report = evaluate(make_rows())
        self.assertIsNone(report["run"]["steps"])
        self.assertGreater(report["sampling"]["samples_total"], 0)

    def test_total_steps_is_reported_and_runtime_has_required_fields(self) -> None:
        report = evaluate([*make_rows(), self.footer(total_steps=5000)])
        self.assertEqual(report["run"]["steps"], 5000)
        self.assertEqual(
            sorted(report["runtime"]), ["implementation", "machine", "os", "python"]
        )

    def test_each_session_needs_enough_steady_samples(self) -> None:
        # 1 Sessionだけ十分で、他は端点しかない場合は合格させない
        rows = make_rows()
        kept = [
            r for r in rows[1:] if r["session_index"] == 0 or r["phase"] != "steady"
        ]
        report = evaluate([rows[0], *kept])
        self.assertIn("insufficient_samples", codes(report))


class CoverageTest(unittest.TestCase):
    def test_required_unsupported_fails_and_is_listed(self) -> None:
        report = evaluate(make_rows(unsupported={"os.handle_count": "tool_missing"}))
        self.assertIn("metric_unsupported", codes(report, "os.handle_count"))
        self.assertIn("os.handle_count", report["coverage"]["unsupported_required"])
        self.assertEqual(
            report["metrics"]["os.handle_count"]["unsupported_reason"], "tool_missing"
        )

    def test_none_is_not_read_as_zero(self) -> None:
        zero = evaluate(
            make_rows(lambda s, st, m: 0 if m == "temp.file_count" else BASE.get(m, 0))
        )
        none = evaluate(
            make_rows(
                lambda s, st, m: None if m == "temp.file_count" else BASE.get(m, 0)
            )
        )
        self.assertNotIn("metric_unreliable", codes(zero, "temp.file_count"))
        self.assertIn("metric_unreliable", codes(none, "temp.file_count"))

    def test_optional_unsupported_is_listed_without_blocking(self) -> None:
        rows = make_rows(
            ids=(*FAST_IDS, "native.alloc_count"),
            unsupported={"native.alloc_count": "no_counter"},
        )
        report = evaluate(rows)
        self.assertEqual(report["verdict"], "pass", report["reasons"])
        self.assertIn("native.alloc_count", report["coverage"]["unsupported_optional"])

    def test_all_unsupported_never_passes(self) -> None:
        report = evaluate(make_rows(unsupported={m: "platform" for m in FAST_IDS}))
        self.assertIn("no_judged_metrics", codes(report))
        self.assertEqual(report["verdict"], "fail")

    def test_missing_ratio_over_five_percent_is_unreliable(self) -> None:
        def sparse(period: int) -> ValueFn:
            def fn(session: int, step: int, metric: str) -> float | None:
                if (
                    metric == "os.handle_count"
                    and 500 < step < 1000
                    and (step // 10) % period == 0
                ):
                    return None
                return BASE.get(metric, 0)

            return fn

        # period 10は約8%、period 25は約4%(steady標本のNone)
        self.assertIn(
            "metric_unreliable",
            codes(evaluate(make_rows(sparse(10))), "os.handle_count"),
        )
        self.assertNotIn(
            "metric_unreliable",
            codes(evaluate(make_rows(sparse(25))), "os.handle_count"),
        )

    def test_insufficient_data_fails(self) -> None:
        self.assertIn("insufficient_samples", codes(evaluate(make_rows(steps=520))))
        self.assertIn("insufficient_sessions", codes(evaluate(make_rows(sessions=3))))

    def test_tail_slope_unavailable_is_unreliable(self) -> None:
        # 後半の有効標本が16未満なら傾きを計算せずfail(tail_growthだけで合格させない)
        def holes(session: int, step: int, metric: str) -> float | None:
            if metric == "os.handle_count" and session >= 2 and step % 100 != 9:
                return None
            return BASE.get(metric, 0)

        report = evaluate(make_rows(holes))
        self.assertIn("metric_unreliable", codes(report, "os.handle_count"))

    def test_must_judge_not_judged_fails(self) -> None:
        profile = load_profile()
        # must_judgeのmetricを診断扱いにしても素通りさせない
        profile["layers"]["fast"]["metrics"]["os.handle_count"]["judge"] = False
        self.assertIn("no_judged_metrics", codes(evaluate(make_rows(), profile)))


class ProfileValidationTest(unittest.TestCase):
    def mutated(self, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        profile: dict[str, Any] = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        change(profile)
        return profile

    def test_default_profile_is_valid(self) -> None:
        self.assertEqual(load_profile()["schema"], rs.PROFILE_SCHEMA)

    def test_hard_limits_are_enforced(self) -> None:
        layers = "layers"
        cases: dict[str, Callable[[dict[str, Any]], None]] = {
            "bytes rel": lambda p: p[layers]["integration"]["metrics"][
                "mem.resident_bytes"
            ]["tolerance"].update(rel=0.06),
            "gdi": lambda p: p[layers]["integration"]["metrics"]["os.gdi_objects"][
                "tolerance"
            ].update({"abs": 9}),
            "registry": lambda p: p[layers]["fast"]["metrics"][
                "registry.active_leases"
            ]["tolerance"].update({"abs": 1}),
            "temp": lambda p: p[layers]["integration"]["metrics"]["temp.file_count"][
                "recovery"
            ].update({"abs": 17}),
            "vocabulary": lambda p: p[layers]["fast"]["metrics"].__setitem__(
                "storage.bytes", {"required_on": [], "class": "bytes"}
            ),
            "layer x profile": lambda p: p[layers]["fast"]["run_profiles"].__setitem__(
                "pr", p[layers]["fast"]["run_profiles"]["fast"]
            ),
            "fast observe": lambda p: p["enforcement"].update(fast="observe"),
            "max_samples": lambda p: p[layers]["fast"]["run_profiles"]["fast"].update(
                sample_every_steps=1
            ),
            "segments": lambda p: p[layers]["fast"]["run_profiles"]["fast"].update(
                segments=3
            ),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(rs.ProfileError):
                rs.validate_profile(self.mutated(change))

    def test_samples_header_is_validated(self) -> None:
        rows = make_rows()
        with self.assertRaises(rs.ProfileError):
            rs.parse_samples(rows[1:])
        rows[0]["schema"] = "resource-stability-sample/v9"
        with self.assertRaises(rs.ProfileError):
            rs.parse_samples(rows)
        bad = make_rows()
        bad[0]["metrics"]["storage.bytes"] = {
            "source": "synthetic",
            "class": "bytes",
            "unit": "bytes",
            "status": "measured",
        }
        with self.assertRaises(rs.ProfileError):
            rs.parse_samples(bad)

    def test_malformed_samples_are_rejected_not_raised(self) -> None:
        footer = {"schema": rs.SAMPLE_SCHEMA, "kind": "footer", "run_id": "t"}
        cases: dict[str, Callable[[list[dict[str, Any]]], None]] = {
            "nan value": lambda r: r[1]["values"].update(
                {"os.handle_count": float("nan")}
            ),
            "negative step": lambda r: r[1].update(step=-1),
            "string step": lambda r: r[1].update(step="1"),
            "missing key": lambda r: r[1].pop("t_monotonic_ns"),
            "unknown metric": lambda r: r[1]["values"].update({"storage.bytes": 1}),
            "string gpu flag": lambda r: r[0]["declarations"].update(
                exclusive_gpu="false"
            ),
            "bad ceiling": lambda r: r[0]["declarations"].update(
                declared_ceiling={"os.handle_count": float("inf")}
            ),
            "two footers": lambda r: r.extend([footer, footer]),
            "sample after footer": lambda r: r.extend([footer, copy.deepcopy(r[1])]),
            "orphan none": lambda r: r.append({**footer, "orphan_count": None}),
        }
        for name, change in cases.items():
            rows = make_rows()
            change(rows)
            with self.subTest(name), self.assertRaises(rs.ProfileError):
                rs.parse_samples(rows)

    def test_input_order_does_not_change_the_report(self) -> None:
        rows = make_rows()
        shuffled = [rows[0], *reversed(rows[1:])]
        self.assertEqual(evaluate(rows), evaluate(shuffled))

    def test_must_judge_cannot_be_emptied(self) -> None:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profile["layers"]["fast"]["must_judge"] = {"any": []}
        with self.assertRaises(rs.ProfileError):
            rs.validate_profile(profile)
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profile["layers"]["fast"]["metrics"]["registry.max_cleanup_latency_ms"][
            "ceiling"
        ] = 0
        with self.assertRaises(rs.ProfileError):
            rs.validate_profile(profile)

    def test_profile_type_and_count_rel_hard_limits(self) -> None:
        layers = "layers"
        cases: dict[str, Callable[[dict[str, Any]], None]] = {
            "count rel": lambda p: p[layers]["integration"]["metrics"][
                "os.handle_count"
            ]["tolerance"].update(rel=0.5),
            "max_samples null": lambda p: p[layers]["fast"]["run_profiles"][
                "fast"
            ].update(max_samples=None),
            "max_samples str": lambda p: p[layers]["fast"]["run_profiles"][
                "fast"
            ].update(max_samples="9"),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(rs.ProfileError):
                rs.validate_profile(self.mutated(change))

    def test_header_types_are_validated(self) -> None:
        for key, bad in (
            ("metrics", None),
            ("layer", ["fast"]),
            ("host_profile", 3),
            ("scenario", ""),
        ):
            rows = make_rows()
            rows[0][key] = bad
            with self.subTest(key), self.assertRaises(rs.ProfileError):
                rs.parse_samples(rows)

    def test_deepcopy_independence(self) -> None:
        profile = load_profile()
        other = copy.deepcopy(profile)
        other["layers"]["fast"]["metrics"]["os.handle_count"]["judge"] = False
        self.assertNotIn(
            "judge", profile["layers"]["fast"]["metrics"]["os.handle_count"]
        )


if __name__ == "__main__":
    unittest.main()
