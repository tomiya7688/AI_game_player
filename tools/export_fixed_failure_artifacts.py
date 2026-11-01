"""Temporary, offline exporter for TWO pinned GHA failure artifacts only.

No network, archive extraction, recursive discovery, arbitrary path selection or
free-text output. Download/extraction belongs to actions/download-artifact@v4.
Expected ZIP digests are provenance expectations, NOT recomputed ZIP evidence.
"""

from __future__ import annotations
import argparse
import base64
from collections import deque
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
import zlib

MAX_RAW = 16 * 1024 * 1024
MAX_PNG = 128 * 1024
MAX_JSON = 1024 * 1024
MAX_LINE = 64 * 1024
MAX_OUTPUT = 224 * 1024
PNG_LIMIT = 640
FIXED_IMAGE = Path(
    "2048-campaign-20261101T205724Z/runtime/episode-0002/recognition-failure-800px04c"
)
FIXED_LEASE = Path("2048-campaign-20261101T210246Z")
ARTIFACTS = (
    (
        41349184700,
        13100932060,
        "e410f359a8a340e7d69a907b4629c3b14e9332c7c35521d2168fc32d093ec53d",
    ),
    (
        41349710044,
        13100474424,
        "7aa24c2d89400c109093360072b9113f056f3377bc146116e1a0e9e785dea1f8",
    ),
)
STATES = {
    "safe_idle",
    "active",
    "stopping",
    "recovery_required",
    "closed",
    "SAFE_IDLE",
    "ACTIVE",
    "STOPPING",
    "RECOVERY_REQUIRED",
    "CLOSED",
    "emergency_stop",
    "EMERGENCY_STOP",
}
CODES = {
    "lease_expired",
    "runtime_not_active",
    "observation_stale",
    "invalid_lease_timestamp",
    "safety_block",
    "owner_process_lost",
    "target_process_lost",
    "target_window_lost",
    "emergency_stop",
    "closed",
    "initial_start",
    "duration_reached",
    "milestone_reached",
    "armed",
    "observation_received",
    "command_completed",
    "stopped",
    "lease_unreadable",
    "critical_storage_failure",
    "allowed",
    "initial_lease",
    "explicit_rearm",
}
REASON_MAP = {"runtime closed": "runtime_closed"}
EVENTS = {
    "campaign_started",
    "episode_started",
    "step",
    "campaign_failure",
    "campaign_summary",
    "action_not_executed",
    "episode_restarted",
    "terminal_observed",
    "recovery_started",
    "cleanup_failure",
}
NUMERIC = {
    "timestamp",
    "updated_at",
    "lease_expires_at",
    "observation_at",
    "observation_expires_at",
    "last_sequence",
    "inflight",
    "watchdog_poll_seconds",
    "owner_pid",
    "target_pid",
    "target_handle",
    "step",
    "episode",
    "steps",
    "actions_executed",
    "elapsed_seconds",
    "duration_target_seconds",
    "episode_count",
    "recovery_count",
    "terminal_restart_count",
    "max_tile",
    "score_samples",
    "latency_ms",
    "action_generation",
    "captured_at",
    "frame_id",
}
BOOLEANS = {
    "success",
    "state_changed",
    "terminal",
    "executed",
    "live_input_verified",
    "milestone_enabled",
    "milestone_reached",
    "allowed",
}


class Rejected(ValueError):
    pass


def read_fixed(root: Path, relative: Path, limit: int) -> bytes:
    # Each component must be a real directory/file; reject Windows reparse points.
    if relative.is_absolute() or ".." in relative.parts:
        raise Rejected("path_policy")
    root = root.absolute()
    current = root
    for part in ("", *relative.parts):
        if part:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise Rejected("linked_path")
        if current != root / relative and not stat.S_ISDIR(info.st_mode):
            raise Rejected("non_directory")
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        raise Rejected("file_bound")
    # Metadata is from a completed, read-only artifact. Still reject identity/size
    # changes between lstat/open and never follow a final symlink on POSIX.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(current, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise Rejected("file_changed")
        data = stream.read(limit + 1)
    if len(data) != info.st_size or len(data) > limit:
        raise Rejected("file_bound")
    return data


def parse_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Rejected("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(Rejected("nonfinite")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Rejected("invalid_json") from exc


def number(value):
    # Range first avoids huge-int conversion overflow. Clock units stay named.
    return (
        type(value) in (int, float) and -1e18 <= value <= 1e18 and math.isfinite(value)
    )


def project(value):
    if not isinstance(value, dict):
        raise Rejected("object_required")
    result = {}
    for key in NUMERIC:
        if key in value and number(value[key]):
            result[key] = value[key]
    for key in BOOLEANS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if value.get("state") in STATES:
        result["state"] = value["state"]
    for key in ("epoch", "session_id"):
        opaque = value.get(key)
        if isinstance(opaque, str) and re.fullmatch("[a-f0-9]{32}", opaque):
            result[key + "_ref_sha256"] = sha256(opaque.encode("ascii")).hexdigest()
    for key in ("reason", "code", "category", "failure_category", "stop_reason"):
        if isinstance(value.get(key), str) and value[key] in REASON_MAP:
            result[key] = REASON_MAP[value[key]]
        elif value.get(key) in CODES:
            result[key] = value[key]
    if value.get("event") in EVENTS:
        result["event"] = value["event"]
    # NO arbitrary error text, URLs, session/epoch strings, labels, paths or keys.
    for key in ("failure_detail", "detail"):
        text = value.get(key)
        if isinstance(text, str) and len(text) <= MAX_LINE:
            result["known_codes"] = sorted(
                code
                for code in CODES
                if re.search(r"(?<![A-Za-z0-9_])" + code + r"(?![A-Za-z0-9_])", text)
            )
            match = re.search(r"observation_timing=(\{.{1,8192}\})", text)
            if match:
                try:
                    timing = parse_json(match.group(1).encode())
                    if not isinstance(timing, dict):
                        raise Rejected("object_required")
                except Rejected:
                    result["observation_timing_status"] = "invalid_optional_json"
                    continue
                out = {}
                if number(timing.get("elapsed_ms")):
                    out["elapsed_ms"] = timing["elapsed_ms"]
                stage = timing.get("stage_ms", {})
                if isinstance(stage, dict):
                    out["stage_ms"] = {
                        k: stage[k]
                        for k in (
                            "capture",
                            "frame_analysis",
                            "board_adaptation",
                            "observation_refresh",
                            "evidence_retention",
                        )
                        if number(stage.get(k))
                    }
                result["observation_timing"] = out
    return result


def image_records(root: Path):
    from PIL import Image

    manifest_data = read_fixed(root, FIXED_IMAGE / "manifest.json", 64 * 1024)
    manifest = parse_json(manifest_data)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "2048-recognition-evidence/v1"
    ):
        raise Rejected("manifest_schema")
    frame = manifest.get("frames", {}).get("last_capture")
    if (
        not isinstance(frame, dict)
        or frame.get("png") != "last-captured.png"
        or frame.get("bgra") != "last-captured.bgra"
    ):
        raise Rejected("frame_names")
    width, height = frame.get("width"), frame.get("height")
    if (width, height) not in {(892, 642), (904, 648)}:
        raise Rejected("owned_frame_geometry")
    bgra = read_fixed(root, FIXED_IMAGE / "last-captured.bgra", MAX_RAW)
    expected = frame.get("bgra_sha256")
    if not isinstance(expected, str) or re.fullmatch("[a-f0-9]{64}", expected) is None:
        raise Rejected("invalid_sha")
    if len(bgra) != width * height * 4 or sha256(bgra).hexdigest() != expected:
        raise Rejected("raw_sha_mismatch")
    png = read_fixed(root, FIXED_IMAGE / "last-captured.png", MAX_PNG)
    # RecognitionFailureEvidence writes exactly these three RGB chunks. Reject
    # ancillary text/ICC/EXIF and appended bytes rather than forwarding metadata.
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise Rejected("png_signature")
    offset = 8
    for expected_chunk in (b"IHDR", b"IDAT", b"IEND"):
        if offset + 12 > len(png):
            raise Rejected("png_chunk_bound")
        length = struct.unpack_from(">I", png, offset)[0]
        end = offset + 12 + length
        if end > len(png) or png[offset + 4 : offset + 8] != expected_chunk:
            raise Rejected("png_chunk_policy")
        if (
            zlib.crc32(png[offset + 4 : end - 4])
            != struct.unpack_from(">I", png, end - 4)[0]
        ):
            raise Rejected("png_crc")
        offset = end
    if offset != len(png):
        raise Rejected("png_trailing_bytes")
    with Image.open(BytesIO(png)) as decoded:
        if decoded.format != "PNG" or decoded.size != (width, height):
            raise Rejected("png_geometry")
        decoded.load()
        rgb = decoded.convert("RGB")
    raw_rgb = Image.frombytes("RGBA", (width, height), bgra, "raw", "BGRA").convert(
        "RGB"
    )
    if rgb.tobytes() != raw_rgb.tobytes():
        raise Rejected("png_raw_mismatch")
    state = frame.get("game_state", {})
    bbox = state.get("board_bbox") if isinstance(state, dict) else None
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(type(v) is not int for v in bbox)
    ):
        raise Rejected("board_crop_missing")
    x, y, w, h = bbox
    # Pinned owned-window only. Exclude browser/title/address UI entirely; no
    # resizing or pixel alteration. Retain board + at most 160px own game header.
    if not (
        150 <= x
        and 160 <= y
        and 300 <= w <= 450
        and w == h
        and x + w <= width
        and y + h <= height
    ):
        raise Rejected("board_crop_policy")
    crop = (x, max(120, y - 160), x + w, y + h)
    if crop[2] - crop[0] > PNG_LIMIT or crop[3] - crop[1] > PNG_LIMIT:
        raise Rejected("crop_bound")
    out = BytesIO()
    cropped = rgb.crop(crop)
    Image.frombytes("RGB", cropped.size, cropped.tobytes()).save(
        out, format="PNG", optimize=False
    )
    encoded = out.getvalue()
    if len(encoded) > MAX_PNG:
        raise Rejected("png_output_bound")
    records = [
        {
            "kind": "reference_game_crop",
            "artifact_id": ARTIFACTS[0][1],
            "manifest_sha256": sha256(manifest_data).hexdigest(),
            "raw_bgra_sha256": expected,
            "original_png_sha256": sha256(png).hexdigest(),
            "png_sha256": sha256(encoded).hexdigest(),
            "png_bytes": len(encoded),
            "width": crop[2] - crop[0],
            "height": crop[3] - crop[1],
            "crop_xyxy": crop,
            "source_width": width,
            "source_height": height,
            "captured_clock": "legacy_frame_monotonic_seconds",
            **{
                k: frame[k] for k in ("captured_at", "frame_id") if number(frame.get(k))
            },
        }
    ]
    b64 = base64.b64encode(encoded).decode("ascii")
    chunks = [b64[i : i + 3500] for i in range(0, len(b64), 3500)]
    records.extend(
        {
            "kind": "reference_game_png_chunk",
            "index": i,
            "total": len(chunks),
            "base64": s,
        }
        for i, s in enumerate(chunks)
    )
    return records


def lease_records(root: Path):
    episode = FIXED_LEASE / "runtime/episode-0001"
    selected = (
        (FIXED_LEASE / "report.json", "report", "campaign-report/v1"),
        (episode / "fail_safe/lease.json", "lease", "kadoka-control-lease/v1"),
        (
            episode / "fail_safe/journal.json",
            "watchdog_journal",
            "kadoka-failsafe-journal/v1",
        ),
    )
    records = []
    for path, kind, schema in selected:
        original = read_fixed(root, path, MAX_JSON)
        value = parse_json(original)
        if not isinstance(value, dict) or value.get("schema") != schema:
            raise Rejected("fixed_schema")
        projected = project(value)
        if isinstance(value.get("details"), dict):
            projected["details"] = project(value["details"])
        records.append(
            {
                "kind": kind,
                "source_sha256": sha256(original).hexdigest(),
                "clock": "legacy_failsafe_wall_seconds"
                if kind != "report"
                else "campaign_elapsed_monotonic_seconds",
                **projected,
            }
        )
    ledger = parse_json(
        read_fixed(root, episode / "fail_safe/input_ledger.json", MAX_JSON)
    )
    if not isinstance(ledger, dict) or ledger.get("schema") != "kadoka-input-ledger/v1":
        raise Rejected("ledger_schema")
    summary = {"kind": "input_ledger", "clock": "legacy_failsafe_wall_seconds"}
    if number(ledger.get("updated_at")):
        summary["updated_at"] = ledger["updated_at"]
    for field in ("held_keys", "held_mouse"):
        held = ledger.get(field)
        if (
            not isinstance(held, dict)
            or len(held) > 256
            or any(not number(v) for v in held.values())
        ):
            raise Rejected("ledger_bound")
        # Never expose pressed key/button identities, free labels, or file paths.
        summary[field + "_count"] = len(held)
        summary[field + "_expires_min"] = min(held.values(), default=None)
        summary[field + "_expires_max"] = max(held.values(), default=None)
    records.append(summary)
    for path, kind in (
        (FIXED_LEASE / "trace.jsonl", "campaign_trace"),
        (episode / "safety_guard.jsonl", "safety_trace"),
    ):
        data = read_fixed(root, path, MAX_RAW)
        if data.count(b"\n") > 5000:
            raise Rejected("trace_line_count")
        tail = deque(maxlen=8)
        for line in data.splitlines():
            if not line or len(line) > MAX_LINE:
                raise Rejected("trace_line_bound")
            value = parse_json(line)
            projected = project(value)
            if projected:
                tail.append(
                    {
                        "kind": kind,
                        "clock": "legacy_failsafe_wall_seconds"
                        if kind == "safety_trace"
                        else "campaign_trace_mixed_named_fields",
                        **projected,
                    }
                )
        records.extend(tail)
        records.append(
            {
                "kind": kind + "_bound",
                "source_sha256": sha256(data).hexdigest(),
                "source_bytes": len(data),
                "emitted_tail_records": len(tail),
            }
        )
    return records


def export_sections(download_root: Path):
    records = [
        {
            "kind": "pinned_artifact",
            "run_id": r,
            "artifact_id": a,
            "expected_zip_sha256": d,
            "zip_digest_recomputed": False,
        }
        for r, a, d in ARTIFACTS
    ]
    failed = False
    for section, reader, path in (
        ("image", image_records, download_root / "known-8dc"),
        ("lease", lease_records, download_root / "known-d954"),
    ):
        try:
            valid = reader(path)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ImportError,
        ) as exc:
            failed = True
            code = (
                str(exc)
                if isinstance(exc, Rejected)
                else "missing_fixed_file"
                if isinstance(exc, FileNotFoundError)
                else "artifact_validation_failed"
            )
            records.append(
                {
                    "kind": "fixed_artifact_section_error",
                    "section": section,
                    "code": code,
                }
            )
        else:
            records.extend(valid)
            records.append(
                {"kind": "fixed_artifact_section_complete", "section": section}
            )
    lines = [
        json.dumps(
            v, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
        for v in records
    ]
    if (
        any(len(line.encode()) > 4096 for line in lines)
        or sum(len(s) + 1 for s in lines) > MAX_OUTPUT
    ):
        raise Rejected("stdout_bound")
    return "\n".join(lines) + "\n", failed


def export(download_root: Path):
    return export_sections(download_root)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        output, failed = export_sections(args.download_root)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ImportError,
    ) as exc:
        # Never print filesystem paths or attacker-controlled exception text.
        code = str(exc) if isinstance(exc, Rejected) else "artifact_validation_failed"
        print(json.dumps({"kind": "fixed_artifact_export_rejected", "code": code}))
        return 1
    sys.stdout.write(output)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
