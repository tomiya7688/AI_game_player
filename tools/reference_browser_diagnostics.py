"""One bounded, opt-in post-failure inspection of an owned reference browser.

This observes the renderer after the failed screenshot was preserved. Connecting
DevTools may affect scheduling; these observations never replace the screenshot
or repair the episode. No game values, storage, console, URLs or endpoint tokens
are returned. Optional dependency: websockets==15.0.1 (validation only).
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
import threading
from typing import Protocol
from urllib.parse import urlsplit

DEADLINE_SECONDS = 1.0
MAX_HTTP_BYTES = 16384
MAX_MESSAGE_BYTES = 16384
MAX_TOTAL_BYTES = 65536
MAX_EVENTS = 8
MAX_FIELDS = 256
RESOURCE_NAMES = {
    "/": "page",
    "/index.html": "page",
    "/style/main.css": "main_css",
    **{
        f"/js/{name}.js": f"{name}_js"
        for name in (
            "animframe_polyfill",
            "bind_polyfill",
            "classlist_polyfill",
            "application",
            "game_manager",
            "grid",
            "html_actuator",
            "keyboard_input_manager",
            "local_storage_manager",
            "tile",
        )
    },
}


class ResourceResponseCounts:
    """Bounded allowlist response writes, not browser receipt or rendering evidence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resources: dict[str, dict[str, object]] = {}
        self._recording_incomplete = False

    def _entry(self, name: str) -> dict[str, object]:
        # Caller holds _lock. Only fixed allowlist names reach this helper.
        return self._resources.setdefault(
            name,
            {
                "responses": 0,
                "completed": 0,
                "bytes_written": 0,
                "unknown_write_counts": 0,
                "requests_started": 0,
                "headers_completed": 0,
                "failed_requests": 0,
                "last_failed_phase": None,
            },
        )

    def _mark(self, name: str, field: str, phase: str | None = None) -> None:
        # Diagnostic counters must not replace a handler's original exception.
        try:
            if name not in RESOURCE_NAMES.values():
                return
            with self._lock:
                value = self._entry(name)
                current = value[field]
                value[field] = min(
                    2**31 - 1, (current if isinstance(current, int) else 0) + 1
                )
                if phase is not None:
                    value["last_failed_phase"] = phase
        except Exception:
            self._recording_incomplete = True

    def started(self, name: str) -> None:
        self._mark(name, "requests_started")

    def headers_completed(self, name: str) -> None:
        self._mark(name, "headers_completed")

    def failed(self, name: str, phase: str) -> None:
        if phase in {"request", "headers", "body"}:
            self._mark(name, "failed_requests", phase)

    def record(
        self,
        name: str,
        status: int,
        written: int,
        digest: str | None,
        complete: bool,
        *,
        written_known: bool = True,
    ) -> None:
        try:
            if name not in RESOURCE_NAMES.values():
                return
            with self._lock:
                value = self._entry(name)
                for field, increment in (
                    ("responses", 1),
                    ("completed", int(complete)),
                    ("bytes_written", written),
                    ("unknown_write_counts", int(not written_known)),
                ):
                    current = value[field]
                    value[field] = min(
                        2**31 - 1,
                        (current if isinstance(current, int) else 0) + increment,
                    )
                value["last_status"] = status
                value["last_body_sha256"] = digest if complete else None
                value["last_write_count_known"] = written_known
        except Exception:
            # Preserve the original I/O exception, but never claim complete recording.
            self._recording_incomplete = True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            values = {key: dict(value) for key, value in self._resources.items()}
        return {
            "resources": values,
            "server_write_only": True,
            "recording_incomplete": self._recording_incomplete,
            "summary_sha256": hashlib.sha256(
                json.dumps(values, sort_keys=True).encode()
            ).hexdigest(),
        }


# A fixed read-only expression. Never accept expressions from the caller or page.
RENDER_EXPRESSION = """(() => {
const root = document.querySelector('.tile-container');
const limitedChildren = (node, limit) => {
  const selected = [], count = Math.min(node.children.length, limit);
  for (let i=0; i<count; i++) selected.push(node.children[i]);
  return selected;
};
const tiles = root ? limitedChildren(root, 32) : [];
const shown = e => { const r=e.getBoundingClientRect();
  return r.width>0 && r.height>0; };
const resources = {
  main_css: 'style/main.css', bind_polyfill_js: 'js/bind_polyfill.js',
  classlist_polyfill_js: 'js/classlist_polyfill.js', animframe_polyfill_js: 'js/animframe_polyfill.js',
  keyboard_input_manager_js: 'js/keyboard_input_manager.js', html_actuator_js: 'js/html_actuator.js',
  grid_js: 'js/grid.js', tile_js: 'js/tile.js', local_storage_manager_js: 'js/local_storage_manager.js',
  game_manager_js: 'js/game_manager.js', application_js: 'js/application.js'
};
const timing = {state: 'unavailable', script_count_capped: Math.min(document.scripts.length, 32),
  script_overflow: document.scripts.length>32, resource_count_capped: null, resources: {}};
const names = Object.keys(resources);
for (const name of names) timing.resources[name] = {
  dom_script_present: timing.script_overflow || name==='main_css' ? null : false,
  entry_count: null, response_status: null, transfer_size: null,
  encoded_body_size: null, decoded_body_size: null, response_start: null, response_end: null};
for (let i=0; i<timing.script_count_capped; i++) {
  const source = document.scripts[i].src;
  for (const name of names) if (source===location.origin+'/'+resources[name])
    timing.resources[name].dom_script_present = true;
}
const boundedInteger = (value, maximum) =>
  Number.isInteger(value) && value>=0 && value<=maximum ? value : null;
const timestampPresent = value => Number.isFinite(value) && value>=0 ? value>0 : null;
try {
  if (typeof performance.getEntriesByType==='function') {
    const entries = performance.getEntriesByType('resource');
    timing.resource_count_capped = Math.min(entries.length, 128);
    timing.state = entries.length>128 ? 'overflow' : 'available';
    if (timing.state==='available') {
      for (const name of names) timing.resources[name].entry_count = 0;
      for (let i=0; i<entries.length; i++) {
        const entry = entries[i];
        for (const name of names) if (entry.name===location.origin+'/'+resources[name]) {
          const result = timing.resources[name];
          result.entry_count++;
          if (result.entry_count===1) {
            result.response_status = boundedInteger(entry.responseStatus, 599);
            result.transfer_size = boundedInteger(entry.transferSize, 16777216);
            result.encoded_body_size = boundedInteger(entry.encodedBodySize, 16777216);
            result.decoded_body_size = boundedInteger(entry.decodedBodySize, 16777216);
            result.response_start = timestampPresent(entry.responseStart);
            result.response_end = timestampPresent(entry.responseEnd);
          } else {
            for (const field of ['response_status','transfer_size','encoded_body_size',
              'decoded_body_size','response_start','response_end']) result[field] = null;
          }
        }
      }
    }
  }
} catch (_) {
  timing.state = 'unavailable'; timing.resource_count_capped = null;
  for (const name of names) for (const field of ['entry_count','response_status','transfer_size',
    'encoded_body_size','decoded_body_size','response_start','response_end']) timing.resources[name][field] = null;
}
return {ready_state: document.readyState,
 visibility: document.visibilityState, focused: document.hasFocus(),
 game_manager_constructor: typeof window.GameManager === 'function',
 actuator_constructor: typeof window.HTMLActuator === 'function',
 input_constructor: typeof window.KeyboardInputManager === 'function',
 storage_constructor: typeof window.LocalStorageManager === 'function',
 grid_constructor: typeof window.Grid === 'function', tile_constructor: typeof window.Tile === 'function',
 grid_present: !!document.querySelector('.grid-container'),
 tile_root_present: !!root, tile_count: tiles.length,
 tile_count_overflow: !!root && root.children.length>32,
 tile_boxes_visible: tiles.length>0 && tiles.every(shown),
 tile_opacity_visible: tiles.length>0 && tiles.every(e =>
   Number(getComputedStyle(e).opacity)>0 &&
   limitedChildren(e, 4).every(c => Number(getComputedStyle(c).opacity)>0)), resource_timing: timing};
})()"""
EXPRESSION_SHA256 = hashlib.sha256(RENDER_EXPRESSION.encode()).hexdigest()
BOOLEAN_FIELDS = frozenset(
    (
        "focused",
        "game_manager_constructor",
        "actuator_constructor",
        "input_constructor",
        "storage_constructor",
        "grid_constructor",
        "tile_constructor",
        "grid_present",
        "tile_root_present",
        "tile_count_overflow",
        "tile_boxes_visible",
        "tile_opacity_visible",
    )
)
TIMING_RESOURCE_NAMES = frozenset(RESOURCE_NAMES.values()) - {"page"}
TIMING_FIELDS = frozenset(
    (
        "dom_script_present",
        "entry_count",
        "response_status",
        "transfer_size",
        "encoded_body_size",
        "decoded_body_size",
        "response_start",
        "response_end",
    )
)


class OwnedProcess(Protocol):
    """The already launched process; never search for another browser."""

    pid: int

    def poll(self) -> int | None: ...


def _owned_window(process: OwnedProcess, hwnd: int) -> bool:
    """Check the original process is alive and still owns the selected HWND."""
    if os.name != "nt" or process.poll() is not None or hwnd <= 0:
        return False
    from ctypes import wintypes

    api = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindow.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    return (
        bool(
            api.IsWindow(hwnd) and api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        )
        and pid.value == process.pid
    )


def _field_count(value: object, depth: int = 0) -> int:
    """Reject excessively nested/large JSON even within the transport byte cap."""
    if depth > 8:
        raise ValueError("shape")
    if isinstance(value, dict):
        count = len(value) + sum(_field_count(v, depth + 1) for v in value.values())
    elif isinstance(value, list):
        count = len(value) + sum(_field_count(v, depth + 1) for v in value)
    else:
        count = 0
    if count > MAX_FIELDS:
        raise ValueError("fields")
    return count


def _decode(raw: str | bytes) -> object:
    value: object = json.loads(raw)
    _field_count(value)
    return value


def _expected_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or parsed.path != "/"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("url")


def _port(profile: Path) -> int:
    # wait_for cannot interrupt OS synchronous file I/O. Reject links, reparse
    # points and non-regular files before opening; NONBLOCK also rejects a FIFO
    # substituted between lstat and open on POSIX. The profile is session-private.
    profile_stat = profile.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat.S_ISDIR(profile_stat.st_mode)
        or getattr(profile_stat, "st_file_attributes", 0) & reparse
    ):
        raise ValueError("profile_type")
    path = profile / "DevToolsActivePort"
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or not 0 < before.st_size <= 1024
        or getattr(before, "st_file_attributes", 0) & reparse
    ):
        raise ValueError("port_file_type")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not 0 < opened.st_size <= 1024
            or getattr(opened, "st_file_attributes", 0) & reparse
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("port_file_changed")
        content = os.read(descriptor, 1025)
    finally:
        os.close(descriptor)
    if len(content) > 1024:
        raise ValueError("port_file")
    lines = content.decode("ascii").splitlines()
    if (
        len(lines) != 2
        or not lines[0].isdigit()
        or re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{1,128}", lines[1]) is None
    ):
        raise ValueError("port_file")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise ValueError("port")
    return port


async def _targets(port: int) -> object:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=4096)
    try:
        writer.write(
            f"GET /json/list HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
        )
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 4096 or not header.startswith(b"HTTP/1.1 200 "):
            raise ValueError("http")
        headers = header.decode("ascii").split("\r\n")[1:]
        lengths = [
            line.partition(":")[2].strip()
            for line in headers
            if line.lower().startswith("content-length:")
        ]
        if (
            len(lengths) != 1
            or not lengths[0].isdigit()
            or any(line.lower().startswith("transfer-encoding:") for line in headers)
        ):
            raise ValueError("http")
        size = int(lengths[0])
        if not 0 < size <= MAX_HTTP_BYTES:
            raise ValueError("http_size")
        return _decode(await reader.readexactly(size))
    finally:
        writer.close()


def _target_socket(targets: object, url: str, port: int) -> str:
    if not isinstance(targets, list) or len(targets) > 8:
        raise ValueError("targets")
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    if len(pages) != 1 or pages[0].get("url") != url:
        raise ValueError("target_identity")
    endpoint = pages[0].get("webSocketDebuggerUrl")
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        raise ValueError("endpoint")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.fullmatch(r"/devtools/page/[A-Za-z0-9-]{1,128}", parsed.path) is None
    ):
        raise ValueError("endpoint")
    return endpoint


def _sanitize(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != BOOLEAN_FIELDS | {
        "ready_state",
        "visibility",
        "tile_count",
        "resource_timing",
    }:
        raise ValueError("result_fields")
    if any(type(value[k]) is not bool for k in BOOLEAN_FIELDS):
        raise ValueError("result_type")
    if value["ready_state"] not in ("loading", "interactive", "complete") or value[
        "visibility"
    ] not in ("visible", "hidden", "prerender"):
        raise ValueError("result_enum")
    if type(value["tile_count"]) is not int or not 0 <= value["tile_count"] <= 32:
        raise ValueError("result_count")
    _sanitize_timing(value["resource_timing"])
    return dict(value)


# {
#   責務: [_sanitize_timing: 固定allowlistの数値/boolだけを受け入れ未知と欠落を分離する]
#   処理: [field集合/enum/上限を検証し、非取得時のNoneをmissingやnetwork原因へ変換しない]
#   引数: [value: 同一固定CDP式のResourceTiming出力] 戻り値: [なし]
#   エラー: [ValueError: 余分なURL/自由文字/非有限値/型/状態整合違反を拒否]
# }
def _sanitize_timing(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "state",
        "script_count_capped",
        "script_overflow",
        "resource_count_capped",
        "resources",
    }:
        raise ValueError("timing_fields")
    if value["state"] not in ("available", "unavailable", "overflow"):
        raise ValueError("timing_state")
    if (
        type(value["script_overflow"]) is not bool
        or type(value["script_count_capped"]) is not int
        or not 0 <= value["script_count_capped"] <= 32
    ):
        raise ValueError("timing_scripts")
    count = value["resource_count_capped"]
    if count is not None and (type(count) is not int or not 0 <= count <= 128):
        raise ValueError("timing_count")
    if (
        value["state"] == "unavailable"
        and count is not None
        or value["state"] == "available"
        and count is None
        or value["state"] == "overflow"
        and count != 128
    ):
        raise ValueError("timing_count_state")
    if value["script_overflow"] and value["script_count_capped"] != 32:
        raise ValueError("timing_script_overflow")
    resources = value["resources"]
    if not isinstance(resources, dict) or set(resources) != TIMING_RESOURCE_NAMES:
        raise ValueError("timing_allowlist")
    for item in resources.values():
        if not isinstance(item, dict) or set(item) != TIMING_FIELDS:
            raise ValueError("timing_resource_fields")
        for field in ("dom_script_present", "response_start", "response_end"):
            if item[field] is not None and type(item[field]) is not bool:
                raise ValueError("timing_boolean")
        for field, maximum in (
            ("entry_count", 128),
            ("response_status", 599),
            ("transfer_size", 16777216),
            ("encoded_body_size", 16777216),
            ("decoded_body_size", 16777216),
        ):
            if item[field] is not None and (
                type(item[field]) is not int or not 0 <= item[field] <= maximum
            ):
                raise ValueError("timing_number")
        if value["state"] != "available" and any(
            item[field] is not None for field in TIMING_FIELDS - {"dom_script_present"}
        ):
            raise ValueError("timing_unknown")
        if value["state"] == "available" and item["entry_count"] is None:
            raise ValueError("timing_missing_count")
        if item["entry_count"] != 1 and any(
            item[field] is not None
            for field in TIMING_FIELDS - {"dom_script_present", "entry_count"}
        ):
            raise ValueError("timing_ambiguous")


async def _inspect(
    profile: Path, url: str, process: OwnedProcess, hwnd: int
) -> dict[str, object]:
    from websockets.asyncio.client import connect

    class NoRedirectConnect(connect):
        # Even a redirect from the loopback port may not select another endpoint.
        def process_redirect(self, exc: Exception) -> Exception:
            return exc

    # Library debug/exception logs can contain the private handshake path.
    # Keep this connection silent even when the caller globally enables DEBUG.
    quiet_logger = logging.Logger("reference_browser_diagnostic_private")
    quiet_logger.disabled = True

    if not _owned_window(process, hwnd):
        raise ValueError("ownership")
    _expected_url(url)
    port = _port(profile)
    endpoint = _target_socket(await _targets(port), url, port)
    if not _owned_window(process, hwnd):
        raise ValueError("ownership")
    async with NoRedirectConnect(
        endpoint,
        proxy=None,
        compression=None,
        open_timeout=DEADLINE_SECONDS,
        close_timeout=0.02,
        max_size=MAX_MESSAGE_BYTES,
        max_queue=1,
        ping_interval=None,
        user_agent_header=None,
        logger=quiet_logger,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": RENDER_EXPRESSION,
                        "returnByValue": True,
                        "silent": True,
                        "awaitPromise": False,
                        "disableBreaks": True,
                        "timeout": 500,
                    },
                }
            )
        )
        received = 0
        fields = 0
        for _ in range(MAX_EVENTS + 1):
            raw = await socket.recv()
            received += len(raw.encode("utf-8") if isinstance(raw, str) else raw)
            if received > MAX_TOTAL_BYTES:
                raise ValueError("total_bytes")
            response = _decode(raw)
            fields += _field_count(response)
            if fields > MAX_FIELDS:
                raise ValueError("total_fields")
            if not isinstance(response, dict):
                raise ValueError("response")
            if response.get("id") != 1:
                continue
            result = response.get("result")
            if not isinstance(result, dict) or "exceptionDetails" in result:
                raise ValueError("response")
            remote = result.get("result")
            if not isinstance(remote, dict) or remote.get("type") != "object":
                raise ValueError("response")
            value = _sanitize(remote.get("value"))
            if not _owned_window(process, hwnd):
                raise ValueError("ownership")
            return value
        raise ValueError("event_limit")


def inspect_failed_browser(
    profile: Path, url: str, process: OwnedProcess, hwnd: int
) -> dict[str, object]:
    """One common one-second soft budget, not an OS I/O/scheduling hard bound.

    Errors have fixed labels, never free text. The owner keeps the original failure.
    """
    started = time.monotonic()
    try:
        value = asyncio.run(
            asyncio.wait_for(_inspect(profile, url, process, hwnd), DEADLINE_SECONDS)
        )
        result: dict[str, object] = {"status": "observed", "render": value}
    except asyncio.TimeoutError:
        result = {"status": "unavailable", "reason": "deadline"}
    except ImportError:
        result = {"status": "unavailable", "reason": "optional_dependency"}
    except Exception:
        result = {"status": "unavailable", "reason": "validation_or_transport"}
    result.update(
        {
            "expression_sha256": EXPRESSION_SHA256,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "post_failure_only": True,
            "may_affect_renderer_scheduling": True,
        }
    )
    return result
