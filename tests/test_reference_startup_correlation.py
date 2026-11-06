"""reference-startup-correlation/v1 の分類関数（純粋関数）の試験。Issue #791 T1〜T3。"""

import copy
import unittest

from tools.reference_startup_correlation import (
    RESOURCE_NAMES,
    SCHEMA,
    SCRIPT_NAMES,
    classify_startup_failure,
)

CORRELATION_ID = "0123456789abcdef0123456789abcdef"
ACTUATOR = "html_actuator_js"
GRID = "grid_js"


def digest(name):
    return (name.encode().hex() * 8)[:64]


def entry(name, **changes):
    value = {
        "responses": 1,
        "completed": 1,
        "bytes_written": 10,
        "unknown_write_counts": 0,
        "requests_started": 1,
        "headers_completed": 1,
        "failed_requests": 0,
        "last_failed_phase": None,
        "failed_request_phase": 0,
        "failed_headers_phase": 0,
        "failed_body_phase": 0,
        "first_started_ms": 1.0,
        "first_headers_ms": 2.0,
        "first_completed_ms": 3.0,
        "first_failed_ms": None,
        "last_status": 200,
        "last_body_sha256": digest(name),
        "last_write_count_known": True,
    }
    value.update(changes)
    return value


def absent(**changes):
    """一度も要求されなかった資源（0初期化のentry）。changesで開始・失敗を足せる。"""
    base = {
        "responses": 0,
        "completed": 0,
        "bytes_written": 0,
        "requests_started": 0,
        "headers_completed": 0,
        "first_started_ms": None,
        "first_headers_ms": None,
        "first_completed_ms": None,
        "last_status": None,
        "last_body_sha256": None,
        "last_write_count_known": None,
    }
    base.update(changes)
    return entry("x", **base)


def render(**changes):
    value = {
        "ready_state": "complete",
        "visibility": "visible",
        "focused": True,
        "game_manager_constructor": True,
        "actuator_constructor": True,
        "input_constructor": True,
        "storage_constructor": True,
        "grid_constructor": True,
        "tile_constructor": True,
        "tile_count": 2,
        "tile_boxes_visible": True,
        "tile_opacity_visible": True,
        "resource_timing": {
            "state": "available",
            "resources": {"html_actuator_js": {"entry_count": 1}},
        },
    }
    value.update(changes)
    return value


def make_record(
    changes=None,
    *,
    without=0,
    awaiting=0,
    errors=0,
    frame=None,
    cdp_render=None,
    cdp_status="observed",
):
    resources = {name: entry(name) for name in RESOURCE_NAMES}
    for name, value in (changes or {}).items():
        resources[name] = value
    parsed = sum(r["requests_started"] for r in resources.values())
    return {
        "schema": SCHEMA,
        "correlation_id": CORRELATION_ID,
        "episode": 1,
        "seed": 481,
        "server": {
            "connections_accepted": parsed + without + awaiting + errors,
            "connections_awaiting_request_line": awaiting,
            "connections_without_request_line": without,
            "request_line_errors": errors,
            "requests_parsed": parsed,
            "requests_in_flight": 0,
            "resources": resources,
            "recording_incomplete": False,
            "snapshots_equal": True,
        },
        "expected_body_sha256": {name: digest(name) for name in RESOURCE_NAMES},
        "frame": {
            "frame_sha256": None,
            "png_sha256": None,
            "captured_ms": 100.0,
            "preserved_ms": 110.0,
            "empty_board": False,
            "board_accepted": True,
            **(frame or {}),
        },
        "cdp": {
            "status": cdp_status,
            "started_ms": 120.0,
            "finished_ms": 130.0,
            **({"render": cdp_render or render()} if cdp_status == "observed" else {}),
        },
        "target_matches_generation": True,
        "browser_version": "1.2.3.4",
        "ci_context": {"event": None, "run_id": None, "run_attempt": None, "sha": None},
    }


class ClassificationTests(unittest.TestCase):
    def test_every_category_has_its_minimal_record(self):
        missing_actuator = {ACTUATOR: absent()}
        cases = {
            "connection_without_request": make_record(missing_actuator, without=1),
            "request_not_observed_by_server": make_record(missing_actuator),
            "server_failed_before_headers": make_record(
                {
                    ACTUATOR: absent(
                        requests_started=1,
                        failed_requests=1,
                        failed_headers_phase=1,
                        last_failed_phase="headers",
                        first_started_ms=1.0,
                        first_failed_ms=2.0,
                    )
                }
            ),
            "server_failed_in_body": make_record(
                {
                    ACTUATOR: absent(
                        requests_started=1,
                        headers_completed=1,
                        failed_requests=1,
                        failed_body_phase=1,
                        last_failed_phase="body",
                        first_started_ms=1.0,
                        first_headers_ms=2.0,
                        first_failed_ms=3.0,
                    )
                }
            ),
            "served_response_invalid": make_record(
                {
                    ACTUATOR: entry(
                        ACTUATOR,
                        completed=0,
                        last_status=404,
                        last_body_sha256=None,
                        first_completed_ms=None,
                    )
                }
            ),
            "served_constructor_missing": make_record(
                cdp_render=render(actuator_constructor=False, tile_count=0)
            ),
            "constructors_defined_no_dom_tiles": make_record(
                cdp_render=render(tile_count=0)
            ),
            "dom_present_frame_empty_candidate": make_record(
                frame={"empty_board": True, "board_accepted": False}
            ),
            "frame_nonempty_not_recognized": make_record(
                frame={"empty_board": False, "board_accepted": False}
            ),
            "unclassified": make_record(),
        }
        boundaries = {}
        for expected, record in cases.items():
            with self.subTest(expected):
                result = classify_startup_failure(record)
                self.assertEqual(result["category"], expected, result)
                self.assertTrue(result["evidence_complete"])
                self.assertEqual(result["missing"], [])
                boundaries[expected] = result["boundary"]
                self.assertEqual(
                    result["control_required"],
                    expected
                    in (
                        "connection_without_request",
                        "dom_present_frame_empty_candidate",
                    ),
                )
        self.assertEqual(len(set(boundaries.values())), len(boundaries))

    def test_sha_mismatch_with_status_200_is_invalid_served_content(self):
        record = make_record({GRID: entry(GRID, last_body_sha256=digest("other"))})
        self.assertEqual(
            classify_startup_failure(record)["category"], "served_response_invalid"
        )

    def test_earlier_boundary_wins_and_later_matches_stay_visible(self):
        record = make_record(
            {
                ACTUATOR: absent(),
                GRID: absent(
                    requests_started=1,
                    failed_requests=1,
                    failed_headers_phase=1,
                    last_failed_phase="headers",
                    first_started_ms=1.0,
                    first_failed_ms=2.0,
                ),
            },
            without=1,
        )
        result = classify_startup_failure(record)
        self.assertEqual(result["category"], "connection_without_request")
        self.assertEqual(
            result["all_matching"],
            ["connection_without_request", "server_failed_before_headers"],
        )

    def test_fail_then_header_failure_in_one_resource_keeps_the_earlier_boundary(self):
        record = make_record(
            {
                ACTUATOR: absent(
                    requests_started=2,
                    failed_requests=2,
                    failed_headers_phase=1,
                    failed_body_phase=1,
                    last_failed_phase="body",
                    first_started_ms=1.0,
                    first_failed_ms=2.0,
                )
            }
        )
        result = classify_startup_failure(record)
        self.assertEqual(result["category"], "server_failed_before_headers")
        self.assertIn("server_failed_in_body", result["all_matching"])

    def test_css_only_failure_is_recorded_and_classification_continues(self):
        record = make_record(
            {
                "main_css": absent(
                    requests_started=1,
                    failed_requests=1,
                    failed_body_phase=1,
                    last_failed_phase="body",
                    first_started_ms=1.0,
                    first_failed_ms=2.0,
                )
            },
            frame={"empty_board": False, "board_accepted": False},
        )
        result = classify_startup_failure(record)
        self.assertEqual(result["other_resource_failures"], ["main_css"])
        self.assertEqual(result["category"], "frame_nonempty_not_recognized")

    def test_missing_cdp_never_turns_into_a_guess(self):
        record = make_record(cdp_status="unavailable")
        result = classify_startup_failure(record)
        self.assertEqual(result["category"], "evidence_incomplete")
        self.assertEqual(result["missing"], ["cdp_unavailable"])

    def test_absent_resource_timing_is_only_a_note(self):
        record = make_record(
            cdp_render=render(
                tile_count=0,
                resource_timing={"state": "unavailable", "resources": {}},
            )
        )
        result = classify_startup_failure(record)
        self.assertEqual(result["category"], "constructors_defined_no_dom_tiles")
        self.assertIn("resource_timing_absence_is_not_evidence", result["next_check"])

    def test_scripts_are_the_fixed_ten(self):
        self.assertEqual(len(SCRIPT_NAMES), 10)
        self.assertEqual(len(RESOURCE_NAMES), 12)


class EvidenceTests(unittest.TestCase):
    def test_legacy_37bb_record_without_new_fields_is_evidence_incomplete(self):
        # 37bb seed481の公開済みcounterだけ。page・css・7 scriptが完了、3 scriptの行が無い。
        legacy_resources = {
            name: {
                "responses": 1,
                "completed": 1,
                "bytes_written": 10,
                "unknown_write_counts": 0,
                "last_status": 200,
            }
            for name in RESOURCE_NAMES
            if name not in (ACTUATOR, GRID, "tile_js")
        }
        legacy = {
            "event": "reference_game_failure_diagnostics",
            "server": {"resources": legacy_resources, "recording_incomplete": False},
        }
        result = classify_startup_failure(legacy)
        self.assertEqual(result["category"], "evidence_incomplete")
        self.assertIn("schema", result["missing"])
        self.assertIn("field_range", result["missing"])
        self.assertEqual(result["all_matching"], [])

    def test_each_violation_is_a_fixed_missing_name(self):
        def set_path(path, value):
            def apply(record):
                target = record
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

            return apply

        mutations = {
            "schema": set_path(("schema",), "old/v0"),
            "correlation_id": set_path(("correlation_id",), "xyz"),
            "target_generation": set_path(("target_matches_generation",), False),
            "recording_incomplete": set_path(("server", "recording_incomplete"), True),
            "snapshot_changed": set_path(("server", "snapshots_equal"), False),
            "field_range": set_path(
                ("server", "resources", GRID, "requests_started"), 2**31 - 1
            ),
            "time_order": set_path(("cdp", "started_ms"), 50.0),
            "frame_result": set_path(("frame", "empty_board"), None),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected):
                record = make_record()
                mutate(record)
                result = classify_startup_failure(record)
                self.assertEqual(result["category"], "evidence_incomplete")
                self.assertIn(expected, result["missing"])

    def test_in_flight_request_means_snapshot_changed(self):
        record = make_record()
        record["server"]["requests_in_flight"] = 1
        self.assertIn("snapshot_changed", classify_startup_failure(record)["missing"])

    def test_unbalanced_connections_are_rejected(self):
        record = make_record()
        record["server"]["connections_accepted"] += 1
        self.assertIn("connection_balance", classify_startup_failure(record)["missing"])

    def test_times_must_be_finite_and_in_range(self):
        for bad in (-1.0, float("nan"), float("inf"), 3_600_001.0):
            with self.subTest(bad):
                record = make_record()
                record["server"]["resources"][GRID]["first_started_ms"] = bad
                self.assertIn("time_order", classify_startup_failure(record)["missing"])

    def test_null_capture_time_and_resource_time_inversion_are_rejected(self):
        record = make_record(frame={"captured_ms": None})
        self.assertIn("time_order", classify_startup_failure(record)["missing"])
        record = make_record()
        record["server"]["resources"][GRID]["first_headers_ms"] = 0.5
        self.assertIn("time_order", classify_startup_failure(record)["missing"])

    def test_outcomes_may_not_exceed_started_requests(self):
        record = make_record()
        record["server"]["resources"][GRID]["failed_body_phase"] = 1
        record["server"]["resources"][GRID]["failed_requests"] = 1
        self.assertIn("outcome_balance", classify_startup_failure(record)["missing"])

    def test_one_completed_and_one_failed_for_two_requests_is_a_valid_re_request(self):
        record = make_record(
            {
                GRID: entry(
                    GRID,
                    requests_started=2,
                    failed_requests=1,
                    failed_headers_phase=1,
                    last_failed_phase="headers",
                    first_failed_ms=2.5,
                )
            }
        )
        result = classify_startup_failure(record)
        self.assertNotEqual(result["category"], "evidence_incomplete")

    def test_non_dictionary_input_is_fail_closed(self):
        self.assertEqual(
            classify_startup_failure(None)["category"], "evidence_incomplete"
        )

    def test_classifier_does_not_mutate_its_input(self):
        record = make_record()
        before = copy.deepcopy(record)
        classify_startup_failure(record)
        self.assertEqual(record, before)


if __name__ == "__main__":
    unittest.main()
