"""Compare finite loopback accept queues with HTTP clients; no browser or game input."""

import argparse
import errno
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import platform
import selectors
import socket
import subprocess
import threading
import time
from unittest.mock import patch
from urllib.request import urlopen

from tools.browser_probe_support import publish_validation_report
from tools.reference_browser_diagnostics import RESOURCE_NAMES
from tools.reference_game_2048_launcher import LoopbackStaticServer
from tools.reference_game_2048_source import PinnedGameSource
from tools.reference_game_http_server import ReferenceGameHTTPServer


def queue_capacity(backlog):
    """Pause accepts for 150ms; count completed TCP handshakes, not browser failures."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(backlog)
    selector = selectors.DefaultSelector()
    connections = []
    completed = errors = 0
    try:
        for index in range(12):
            stream = socket.socket()
            stream.setblocking(False)
            connections.append(stream)
            code = stream.connect_ex(listener.getsockname())
            if code not in {
                0,
                errno.EINPROGRESS,
                errno.EWOULDBLOCK,
                getattr(errno, "WSAEWOULDBLOCK", 10035),
            }:
                errors += 1
                continue
            selector.register(stream, selectors.EVENT_WRITE, index)
        deadline = time.monotonic() + 0.15
        while selector.get_map() and time.monotonic() < deadline:
            for key, _ in selector.select(max(0, deadline - time.monotonic())):
                selector.unregister(key.fileobj)
                if key.fileobj.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    errors += 1
                else:
                    completed += 1
        return {
            "backlog": backlog,
            "attempted_connections": 12,
            "accept_pause_ms": 150,
            "completed_before_accept": completed,
            "pending_before_accept": len(selector.get_map()),
            "socket_error_count": errors,
        }
    finally:
        selector.close()
        for stream in connections:
            stream.close()
        listener.close()


def http_bursts(source, backlog):
    class ProbeServer(ReferenceGameHTTPServer):
        request_queue_size = backlog

    paths = [path for path in RESOURCE_NAMES if path.startswith("/js/")]
    expected = {path: (source / path.lstrip("/")).read_bytes() for path in paths}
    server = LoopbackStaticServer()
    server.test_seed = 481
    server.post_failure_render_diagnostics = True
    with patch(
        "tools.reference_game_2048_launcher.ReferenceGameHTTPServer", ProbeServer
    ):
        url = server.start(source)
    rows = []
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            for index in range(4):
                barrier = threading.Barrier(10)

                def read(path):
                    barrier.wait(3)
                    started = time.perf_counter_ns()
                    with urlopen(url + path.lstrip("/"), timeout=3) as response:
                        body = response.read(65537)
                        status = response.status
                    return {
                        "body_matches": body == expected[path],
                        "status": status,
                        "elapsed_ms": (time.perf_counter_ns() - started) / 1e6,
                    }

                results = list(executor.map(read, paths))
                rows.append(
                    {
                        "index": index,
                        "request_count": len(results),
                        "all_bytes_match": all(
                            value["body_matches"] for value in results
                        ),
                        "all_status_200": all(
                            value["status"] == 200 for value in results
                        ),
                        "max_elapsed_ms": max(value["elapsed_ms"] for value in results),
                    }
                )
        return {
            "backlog": backlog,
            "client": "Python urllib HTTP clients; not Chromium",
            "batches": rows,
            "server_counts": server.resource_response_summary(),
        }
    finally:
        server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("build/reference-http-queue/report.json")
    )
    args = parser.parse_args()
    source = args.source.resolve()
    PinnedGameSource(source.parent)._verify_source(source)
    root = Path(__file__).resolve().parents[1]
    report = {
        "schema_version": 1,
        "source_base_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "probe_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_file_sha256": {
            name: sha256((root / "tools" / name).read_bytes()).hexdigest()
            for name in (
                "reference_game_http_server.py",
                "reference_game_2048_launcher.py",
                "reference_browser_diagnostics.py",
            )
        },
        "platform": platform.platform(),
        "python": platform.python_version(),
        "scope": "HTTP-client and TCP queue probes only; no completed Chromium playback, Windows causality or permanent resource-loss claim",
        "timeouts": "Probe-local bounds only; no production startup/observation/planner budget changes",
        "capacity": [queue_capacity(value) for value in (5, 32, 5, 32, 5, 32)],
        "http": [http_bursts(source, value) for value in (5, 32)],
    }
    archived = publish_validation_report(args.output, report)
    print(
        json.dumps(
            {
                "latest": str(args.output),
                "archive": str(archived),
                "capacity": report["capacity"],
                "http_max_ms": {
                    str(value["backlog"]): max(
                        row["max_elapsed_ms"] for row in value["batches"]
                    )
                    for value in report["http"]
                },
            },
            indent=2,
        )
    )
    return (
        0
        if all(
            row["all_bytes_match"] and row["all_status_200"]
            for value in report["http"]
            for row in value["batches"]
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
