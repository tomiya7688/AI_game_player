"""Bounded developer-probe output and explicitly exported static game assets."""

from pathlib import Path, PurePosixPath
import subprocess
from uuid import uuid4

from ai_game_player.atomic_json import stage_json_write


STATIC_ASSET_SUFFIXES = frozenset(
    (
        ".html",
        ".htm",
        ".js",
        ".css",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".ogg",
        ".mp3",
        ".wav",
    )
)


def export_game_assets(source: Path, commit: str, destination: Path) -> None:
    """Export only committed, non-hidden regular static assets, never checkout data."""
    listing = subprocess.check_output(
        ["git", "-C", str(source), "ls-tree", "-r", "-z", commit], timeout=10
    )
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        path = PurePosixPath(raw_path.decode("utf-8"))
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or path.is_absolute()
            or any(p.startswith(".") for p in path.parts)
            or path.suffix.lower() not in STATIC_ASSET_SUFFIXES
        ):
            continue
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            # Arguments are passed without a shell; the blob ID comes from Git itself.
            subprocess.run(
                ["git", "-C", str(source), "cat-file", "blob", object_id],
                stdout=stream,
                check=True,
                timeout=10,
            )
    if not (destination / "index.html").is_file():
        raise ValueError("the committed static asset allowlist must contain index.html")


def new_probe_directory(root: Path) -> Path:
    directory = root / ("run-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def publish_validation_report(path: Path, report: dict) -> Path:
    """Preserve an immutable per-run report, then atomically update the latest alias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    archived = path.with_name(f"{path.stem}-{uuid4().hex}{path.suffix}")
    for target in (archived, path):
        pending = stage_json_write(target, report)
        try:
            pending.publish()
        finally:
            pending.discard()
    return archived
