from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_game_player.artifact_store import FileSystemArtifactStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure local content-addressed artifact store throughput")
    parser.add_argument("--bytes", type=int, default=1_048_576, help="payload size per unique artifact")
    parser.add_argument("--iterations", type=int, default=8)
    args = parser.parse_args()
    if args.bytes < 8 or args.iterations <= 0:
        parser.error("--bytes must be at least 8 and --iterations must be positive")

    with tempfile.TemporaryDirectory(prefix="ai-game-player-artifact-bench-") as directory:
        store = FileSystemArtifactStore(Path(directory))
        payloads = [index.to_bytes(8, "big") + bytes([index % 251]) * (args.bytes - 8) for index in range(args.iterations)]
        start = time.perf_counter()
        references = [store.put(payload, media_type="application/octet-stream") for payload in payloads]
        write_seconds = time.perf_counter() - start
        start = time.perf_counter()
        for reference, expected in zip(references, payloads, strict=True):
            if store.read(reference) != expected:
                raise RuntimeError("artifact read did not match written bytes")
        read_seconds = time.perf_counter() - start

        repeated_payload = payloads[0]
        start = time.perf_counter()
        repeated_refs = [
            store.put(repeated_payload, media_type="application/octet-stream")
            for _ in range(args.iterations)
        ]
        dedup_seconds = time.perf_counter() - start
        report = {
            "payload_bytes": args.bytes,
            "unique_artifacts": len(references),
            "write_artifacts_per_second": round(args.iterations / write_seconds, 2),
            "read_artifacts_per_second": round(args.iterations / read_seconds, 2),
            "duplicate_references_reused": len(set(repeated_refs)) == 1,
            "dedup_puts_per_second": round(args.iterations / dedup_seconds, 2),
        }
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
