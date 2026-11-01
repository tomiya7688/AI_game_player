"""Explicit developer-only real-Chromium resource-fault fixtures; never a player input.

Requires optional Playwright/Pillow and an already materialized locked source.
No download, game input, recovery retry, console collection or state-value export.
"""

from __future__ import annotations

import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ai_game_player.game_2048_board import Game2048BoardRecognizer
from ai_game_player.screen_capture import ScreenFrame
from tools.reference_browser_diagnostics import (
    EXPRESSION_SHA256,
    RENDER_EXPRESSION,
    _field_count,
    _sanitize,
)
from tools.reference_game_2048_source import PinnedGameSource

CASES = (
    "normal",
    "network_abort",
    "http404",
    "wrong_bytes_200",
    "unknown_timing",
    "overflow_timing",
    "timing_read_error",
    "missing_entry",
    "duplicate_entry",
    "unknown_timing_fields",
)
FAULT_PATHS = frozenset(("/js/html_actuator.js", "/js/grid.js", "/js/tile.js"))
FAULT_NAMES = ("html_actuator_js", "grid_js", "tile_js")
INIT_SCRIPTS = {
    "unknown_timing": "Object.defineProperty(performance,'getEntriesByType',{value:undefined});",
    "overflow_timing": "Object.defineProperty(performance,'getEntriesByType',{value:()=>Array(129).fill({})});",
    "timing_read_error": "Object.defineProperty(performance,'getEntriesByType',{value:()=>[{get name(){throw Error('fixture')}}]});",
    "missing_entry": "const original=performance.getEntriesByType.bind(performance);Object.defineProperty(performance,'getEntriesByType',{value:type=>original(type).filter(e=>!e.name.endsWith('/js/html_actuator.js'))});",
    "duplicate_entry": "const original=performance.getEntriesByType.bind(performance);Object.defineProperty(performance,'getEntriesByType',{value:type=>{const entries=original(type);return entries.concat(entries.filter(e=>e.name.endsWith('/js/html_actuator.js')))}});",
    "unknown_timing_fields": "Object.defineProperty(performance,'getEntriesByType',{value:()=>[{name:location.origin+'/js/html_actuator.js',responseStart:NaN,responseEnd:Infinity}]});",
}


class ResourceFixtureServer(ThreadingHTTPServer):
    """Private fault fixture, not the production server/backlog experiment."""

    request_queue_size = 64


def _handler(source: Path, case: str) -> Any:
    """Serve locked bytes unchanged except the three explicitly declared faults."""

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.partition("?")[0]
            if path in FAULT_PATHS and case == "network_abort":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if path in FAULT_PATHS and case == "http404":
                self.send_error(404)
                return
            if path in FAULT_PATHS and case == "wrong_bytes_200":
                body = b"/* isolated resource fixture: intentionally omitted module */"
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    return partial(Handler, directory=str(source))


def _check(case: str, value: dict[str, Any], empty: bool) -> None:
    """Require every fixed expectation; never turn an unknown into a loader cause."""
    failed = case in ("network_abort", "http404", "wrong_bytes_200")
    conditions = [
        value[name] is not failed
        for name in ("actuator_constructor", "grid_constructor", "tile_constructor")
    ]
    conditions.extend(
        value[name] is True
        for name in (
            "game_manager_constructor",
            "input_constructor",
            "storage_constructor",
        )
    )
    conditions.extend((value["tile_count"] == (0 if failed else 2), empty == failed))
    timing = value["resource_timing"]
    item = timing["resources"]["html_actuator_js"]
    if case in ("unknown_timing", "timing_read_error", "overflow_timing"):
        conditions.append(
            timing["state"]
            == ("overflow" if case == "overflow_timing" else "unavailable")
        )
        conditions.extend(
            r["entry_count"] is None for r in timing["resources"].values()
        )
    elif case == "unknown_timing_fields":
        conditions.extend(
            (
                item["entry_count"] == 1,
                item["response_start"] is None,
                item["response_end"] is None,
                item["response_status"] is None,
            )
        )
    elif case == "missing_entry":
        conditions.extend(
            (
                timing["state"] == "available",
                item["entry_count"] == 0,
                item["dom_script_present"] is True,
                item["response_status"] is None,
            )
        )
    elif case == "duplicate_entry":
        conditions.extend(
            (
                item["entry_count"] == 2,
                item["response_status"] is None,
                item["decoded_body_size"] is None,
            )
        )
    else:
        for name in FAULT_NAMES:
            r = timing["resources"][name]
            conditions.extend(
                (
                    r["entry_count"] == 1,
                    r["dom_script_present"] is True,
                    r["response_status"]
                    == (
                        0
                        if case == "network_abort"
                        else 404
                        if case == "http404"
                        else 200
                    ),
                )
            )
            if case == "network_abort":
                conditions.append(r["decoded_body_size"] == 0)
    if not all(conditions):
        raise AssertionError("fixed resource fixture expectation failed")


def _one_case(
    browser: Any, source: Path, case: str, output: Path, result: dict[str, Any]
) -> None:
    """Use a fresh private context and loopback page; retain evidence before checks."""
    from PIL import Image

    server = ResourceFixtureServer(("127.0.0.1", 0), _handler(source, case))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    context = None
    server_started = False
    try:
        thread.start()
        server_started = True
        context = browser.new_context(viewport={"width": 650, "height": 900})
        origin = f"http://127.0.0.1:{server.server_port}"

        def restrict(route: Any) -> None:
            parsed = urlsplit(route.request.url)
            if (
                parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port == server.server_port
            ):
                route.continue_()
            else:
                route.abort()

        context.route("**/*", restrict)
        if case in INIT_SCRIPTS:
            context.add_init_script(INIT_SCRIPTS[case])
        page = context.new_page()
        page.goto(origin + "/", wait_until="load", timeout=10000)
        time.sleep(0.2)  # Host settling only; never request an extra game RAF callback.
        diagnostic_started = time.perf_counter()
        value = page.evaluate(RENDER_EXPRESSION)
        result["elapsed_ms"] = (time.perf_counter() - diagnostic_started) * 1000
        pixels = page.screenshot(timeout=5000)
        (output / f"{case}.png").write_bytes(pixels)
        image = Image.open(io.BytesIO(pixels)).convert("RGBA")
        frame = ScreenFrame(image.width, image.height, image.tobytes("raw", "BGRA"))
        board = Game2048BoardRecognizer().recognize(frame)
        empty = board is not None and all(
            cell == 0 for row in board.cells for cell in row
        )
        result.update(
            {
                "response_field_count": _field_count(value),
                "response_bytes": len(json.dumps(value).encode()),
                "empty_board_pixels": empty,
                "image_sha256": hashlib.sha256(pixels).hexdigest(),
                "image_size": [image.width, image.height],
            }
        )
        _sanitize(value)
        result["diagnostic"] = value
        _check(case, value, empty)
        result["success"] = True
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if server_started:
                    server.shutdown()
            finally:
                server.server_close()
                if server_started:
                    thread.join(timeout=1)


# {
#   責務: [main: 既存固定sourceだけで全10fault fixtureを明示実行し全結果を保存する]
#   処理: [downloadせずlock照合。失敗caseも保持・後続を実行し最後に非zero判定]
#   引数: [argv: source/browser/outputの明示CLI指定] 戻り値: [全成功0/未達1]
# }
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--browser", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    source = args.source_dir.resolve()
    output_root = args.output.resolve()
    if output_root == source or source in output_root.parents:
        raise ValueError("probe output must stay outside the verified source directory")
    verifier = PinnedGameSource(source.parent)
    if source.name != verifier.lock["commit"] or not source.is_dir():
        raise ValueError("an existing materialized locked source directory is required")
    verifier._verify_source(
        source
    )  # No materialize(), archive download or remote request.
    output = output_root / f"run-{uuid4().hex}"
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "expression_sha256": EXPRESSION_SHA256,
        "source_commit": verifier.lock["commit"],
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Controlled private browser resource faults; not the Windows root cause or AI input",
        "success": False,
        "cases": [],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=str(args.browser.resolve()), headless=True
        )
        report["browser_version"] = browser.version
        try:
            for case in CASES:
                result: dict[str, Any] = {"case": case, "success": False}
                try:
                    _one_case(browser, source, case, output, result)
                except Exception as error:
                    result["success"] = False
                    result["failure_category"] = (
                        "assertion"
                        if isinstance(error, AssertionError)
                        else "os"
                        if isinstance(error, OSError)
                        else "probe_failure"
                    )
                report["cases"].append(result)
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                print(
                    json.dumps({"case": case, "success": result["success"]}), flush=True
                )
        finally:
            try:
                browser.close()
            except Exception:
                report["browser_cleanup_failed"] = True
            try:
                verifier._verify_source(source)
            except Exception:
                report["source_integrity_failed"] = True
    report["all_cases_attempted"] = len(report["cases"]) == len(CASES)
    report["success"] = (
        report["all_cases_attempted"]
        and all(c["success"] for c in report["cases"])
        and not report.get("browser_cleanup_failed")
        and not report.get("source_integrity_failed")
    )
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
