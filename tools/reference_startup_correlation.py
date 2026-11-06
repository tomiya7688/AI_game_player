"""2048参照ゲームの起動失敗を、境界順の原因カテゴリへ分類する純粋関数（Issue #791）。

入力は辞書だけで、I/O・時計・乱数を使わない。証拠が欠けたり矛盾したりしたら
カテゴリを推測せず `evidence_incomplete` を返す（fail-closed）。判定の記録だけを行い、
入力・Planner・lease・Stop・失敗判定には戻さない。
"""

from __future__ import annotations

from math import isfinite

SCHEMA = "reference-startup-correlation/v1"
MAX_COUNTER = 2**31 - 2  # 2**31-1は飽和値で、判別不能として拒否する。
MAX_TIME_MS = 3_600_000.0
SCRIPT_NAMES = (
    "bind_polyfill_js",
    "classlist_polyfill_js",
    "animframe_polyfill_js",
    "keyboard_input_manager_js",
    "html_actuator_js",
    "grid_js",
    "tile_js",
    "local_storage_manager_js",
    "game_manager_js",
    "application_js",
)
# 欠落判定の対象（ページと10 script）。main_cssは他resourceとして別に記録する。
TARGET_NAMES = ("page", *SCRIPT_NAMES)
RESOURCE_NAMES = (*TARGET_NAMES, "main_css")
CONSTRUCTOR_FIELDS = (
    "game_manager_constructor",
    "actuator_constructor",
    "input_constructor",
    "storage_constructor",
    "grid_constructor",
    "tile_constructor",
)
# 境界の固定名。categoryの順序がそのまま判定順になる。
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("connection_without_request", "tcp_accept_to_request_line"),
    ("request_not_observed_by_server", "browser_send_to_server_accept"),
    ("server_failed_before_headers", "server_to_headers"),
    ("server_failed_in_body", "server_to_body"),
    ("served_response_invalid", "served_content"),
    ("served_constructor_missing", "served_to_constructor_defined"),
    ("constructors_defined_no_dom_tiles", "constructor_defined_to_dom"),
    ("dom_present_frame_empty_candidate", "dom_to_compositor_capture"),
    ("frame_nonempty_not_recognized", "capture_to_recognizer"),
)
CONTROL_REQUIRED = frozenset(
    ("connection_without_request", "dom_present_frame_empty_candidate")
)
NEXT_CHECK = {
    "connection_without_request": "compare_success_cohort_request_less_connections",
    "request_not_observed_by_server": "capture_os_listen_queue_or_browser_side_error",
    "server_failed_before_headers": "inspect_handler_send_path_before_headers",
    "server_failed_in_body": "inspect_handler_send_path_body",
    "served_response_invalid": "inspect_served_status_and_body_digest",
    "served_constructor_missing": "add_console_or_load_error_observation",
    "constructors_defined_no_dom_tiles": "observe_game_manager_construction_and_dom",
    "dom_present_frame_empty_candidate": "observe_dom_at_frame_capture_time",
    "frame_nonempty_not_recognized": "inspect_recognizer_on_preserved_frame",
    "unclassified": "collect_more_evidence",
    "evidence_incomplete": "fix_missing_evidence_before_classifying",
}


def _is_counter(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _is_time(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= MAX_TIME_MS


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# {
#   責務: [_validate: 分類の前に全fieldを検証し、違反を固定名のmissing一覧にする]
#   処理: [schema/ID/世代/latch/snapshot/接続収支/範囲/時刻順/結果収支/Frame型を順に確認する]
#   引数: [record: correlation節の辞書]
#   戻り値: [list[str]: 固定名の違反一覧。空なら分類へ進める]
# }
def _validate(record: dict) -> list[str]:
    missing: list[str] = []
    server = _dict(record.get("server"))
    frame = _dict(record.get("frame"))
    cdp = _dict(record.get("cdp"))
    resources = _dict(server.get("resources"))

    if record.get("schema") != SCHEMA:
        missing.append("schema")
    identifier = record.get("correlation_id")
    if (
        not isinstance(identifier, str)
        or len(identifier) != 32
        or any(c not in "0123456789abcdef" for c in identifier)
        or ("correlation_id" in cdp and cdp["correlation_id"] != identifier)
        or ("correlation_id" in frame and frame["correlation_id"] != identifier)
    ):
        missing.append("correlation_id")
    if record.get("target_matches_generation") is not True:
        missing.append("target_generation")
    if server.get("recording_incomplete") is not False:
        missing.append("recording_incomplete")
    if (
        server.get("snapshots_equal") is not True
        or server.get("requests_in_flight") != 0
    ):
        missing.append("snapshot_changed")

    counters = (
        "connections_accepted",
        "connections_awaiting_request_line",
        "connections_without_request_line",
        "request_line_errors",
        "requests_parsed",
        "requests_in_flight",
    )
    if not all(_is_counter(server.get(name)) for name in counters):
        missing.append("field_range")
    elif server["connections_accepted"] != (
        server["connections_without_request_line"]
        + server["connections_awaiting_request_line"]
        + server["request_line_errors"]
        + server["requests_parsed"]
    ):
        missing.append("connection_balance")

    counter_fields = (
        "responses",
        "completed",
        "bytes_written",
        "requests_started",
        "headers_completed",
        "failed_requests",
        "failed_request_phase",
        "failed_headers_phase",
        "failed_body_phase",
    )
    time_fields = (
        "first_started_ms",
        "first_headers_ms",
        "first_completed_ms",
        "first_failed_ms",
    )
    range_bad = set(resources) != set(RESOURCE_NAMES)
    order_bad = False
    outcome_bad = False
    for name in RESOURCE_NAMES:
        entry = _dict(resources.get(name))
        if not all(_is_counter(entry.get(field)) for field in counter_fields):
            range_bad = True
            continue
        for field in time_fields:
            if entry.get(field) is not None and not _is_time(entry[field]):
                order_bad = True
        started, headers, done = (
            entry.get("first_started_ms"),
            entry.get("first_headers_ms"),
            entry.get("first_completed_ms"),
        )
        chain = [value for value in (started, headers, done) if value is not None]
        if chain != sorted(chain):
            order_bad = True
        failures = (
            entry["failed_request_phase"]
            + entry["failed_headers_phase"]
            + entry["failed_body_phase"]
        )
        # 同一resourceの完了と失敗の同時成立は再要求として正当。開始数だけを上限にする。
        if entry["completed"] + failures > entry["requests_started"]:
            outcome_bad = True
    if range_bad and "field_range" not in missing:
        missing.append("field_range")

    stamps = [frame.get("captured_ms"), frame.get("preserved_ms")]
    stamps += [cdp.get("started_ms"), cdp.get("finished_ms")]
    if (
        order_bad
        or not all(_is_time(value) for value in stamps)
        or not stamps[0] <= stamps[1] <= stamps[2] <= stamps[3]
    ):
        missing.append("time_order")
    if outcome_bad:
        missing.append("outcome_balance")
    if (
        type(frame.get("empty_board")) is not bool
        or type(frame.get("board_accepted")) is not bool
    ):
        missing.append("frame_result")
    return missing


def _entry(record: dict, name: str) -> dict:
    return _dict(_dict(_dict(record.get("server")).get("resources")).get(name))


# {
#   責務: [classify_startup_failure: 起動失敗1件の相関記録を境界順の原因カテゴリへ分類する]
#   処理: [1: 全fieldを検証し違反があればevidence_incomplete, 2: 欠落対象を求める, 3: 全categoryの条件を独立に評価し最初の該当を返す。後段の該当もall_matchingに残す]
#   引数: [record: reference-startup-correlation/v1の辞書]
#   戻り値: [dict: category/boundary/evidence_complete/missing/all_matching/control_required/next_check/other_resource_failures]
# }
def classify_startup_failure(record: object) -> dict:
    if not isinstance(record, dict):
        record = {}
    missing = _validate(record)
    if missing:
        return {
            "category": "evidence_incomplete",
            "boundary": "undetermined",
            "evidence_complete": False,
            "missing": missing,
            "all_matching": [],
            "control_required": False,
            "next_check": NEXT_CHECK["evidence_incomplete"],
            "other_resource_failures": [],
        }

    server = record["server"]
    frame = record["frame"]
    cdp = _dict(record.get("cdp"))
    render = _dict(cdp.get("render")) if cdp.get("status") == "observed" else {}
    entries = {name: _entry(record, name) for name in RESOURCE_NAMES}
    expected = _dict(record.get("expected_body_sha256"))

    def failures(entry: dict) -> int:
        return (
            entry["failed_request_phase"]
            + entry["failed_headers_phase"]
            + entry["failed_body_phase"]
        )

    targets = [name for name in TARGET_NAMES if entries[name]["completed"] == 0]
    other = [
        name
        for name in RESOURCE_NAMES
        if name not in targets
        and name != "page"
        and (entries[name]["completed"] == 0 or failures(entries[name]) > 0)
    ]
    request_less = (
        server["connections_without_request_line"]
        + server["connections_awaiting_request_line"]
    )

    def invalid_served(name: str) -> bool:
        entry = entries[name]
        if entry.get("last_status") is None:
            return False
        digest = expected.get(name)
        return entry["last_status"] != 200 or (
            digest is not None
            and entry["completed"] > 0
            and entry.get("last_body_sha256") != digest
        )

    served_ok = all(
        entries[name]["completed"] > 0 and not invalid_served(name)
        for name in TARGET_NAMES
    )
    constructors_missing = bool(render) and any(
        render.get(f) is not True for f in CONSTRUCTOR_FIELDS
    )
    constructors_defined = bool(render) and not constructors_missing
    tile_count = render.get("tile_count")
    matches = {
        "connection_without_request": any(
            entries[n]["requests_started"] == 0 and entries[n]["failed_requests"] == 0
            for n in targets
        )
        and request_less > 0,
        "request_not_observed_by_server": any(
            entries[n]["requests_started"] == 0 for n in targets
        )
        and request_less == 0
        and server["request_line_errors"] == 0
        and server["connections_accepted"] == server["requests_parsed"],
        "server_failed_before_headers": any(
            entries[n]["failed_request_phase"] + entries[n]["failed_headers_phase"] > 0
            for n in targets
        ),
        "server_failed_in_body": any(
            entries[n]["failed_body_phase"] > 0 for n in targets
        ),
        "served_response_invalid": any(invalid_served(n) for n in TARGET_NAMES),
        "served_constructor_missing": served_ok and constructors_missing,
        "constructors_defined_no_dom_tiles": constructors_defined and tile_count == 0,
        "dom_present_frame_empty_candidate": constructors_defined
        and type(tile_count) is int
        and tile_count > 0
        and render.get("tile_boxes_visible") is True
        and render.get("tile_opacity_visible") is True
        and frame["empty_board"] is True,
        "frame_nonempty_not_recognized": frame["empty_board"] is False
        and frame["board_accepted"] is False,
    }
    boundaries = dict(CATEGORIES)
    matching = [name for name, _ in CATEGORIES if matches[name]]
    if matching:
        category = matching[0]
        boundary = boundaries[category]
        result_missing: list[str] = []
        complete = True
    elif not render:
        # CDPを観測できていないため、constructor/DOM境界は判定できない。
        category, boundary = "evidence_incomplete", "undetermined"
        result_missing, complete = ["cdp_unavailable"], False
    else:
        category, boundary = "unclassified", "unknown"
        result_missing, complete = [], True

    note = NEXT_CHECK[category]
    timing = _dict(render.get("resource_timing"))
    if timing.get("state") != "available" or all(
        _dict(item).get("entry_count") == 0
        for item in _dict(timing.get("resources")).values()
    ):
        # entry無し・API無しは成功や原因の根拠にしない。
        note += ";resource_timing_absence_is_not_evidence"
    return {
        "category": category,
        "boundary": boundary,
        "evidence_complete": complete,
        "missing": result_missing,
        "all_matching": matching,
        "control_required": category in CONTROL_REQUIRED,
        "next_check": note,
        "other_resource_failures": other,
    }
