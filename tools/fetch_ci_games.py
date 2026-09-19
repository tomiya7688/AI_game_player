from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "e2e_reference_games.json"
DEFAULT_DEST = ROOT / "build" / "ci-games"


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def materialize(game: dict[str, object], dest_root: Path, clean: bool) -> dict[str, object]:
    game_id = str(game["id"])
    repository = str(game["repository"])
    expected_commit = str(game["commit"])
    target = dest_root / game_id

    if clean and target.exists():
        shutil.rmtree(target)

    if not target.exists():
        target.mkdir(parents=True)
        run("git", "init", cwd=target)
        run("git", "remote", "add", "origin", f"https://github.com/{repository}.git", cwd=target)
        run("git", "fetch", "--depth=1", "origin", expected_commit, cwd=target)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=target)

    actual_commit = run("git", "rev-parse", "HEAD", cwd=target)
    if actual_commit != expected_commit:
        raise RuntimeError(
            f"{game_id}: expected {expected_commit}, got {actual_commit}. "
            "Use --clean to rematerialize the pinned source."
        )

    missing_licenses = [
        str(path)
        for path in game.get("license_paths", [])
        if not (target / str(path)).is_file()
    ]
    if missing_licenses:
        raise RuntimeError(f"{game_id}: missing declared license files: {missing_licenses}")

    return {
        "level": int(game["level"]),
        "id": game_id,
        "repository": repository,
        "commit": actual_commit,
        "license_spdx": str(game["license_spdx"]),
        "license_paths": list(game.get("license_paths", [])),
        "path": str(target.relative_to(ROOT)).replace("\\", "/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize pinned OSS games used by Kadoka CI/E2E validation."
    )
    parser.add_argument("--all", action="store_true", help="Fetch all configured levels.")
    parser.add_argument("--level", type=int, action="append", default=[], help="Fetch one level; repeatable.")
    parser.add_argument("--clean", action="store_true", help="Remove existing checkout before fetching.")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    levels = list(manifest["levels"])
    selected = levels if args.all or not args.level else [
        game for game in levels if int(game["level"]) in set(args.level)
    ]
    if not selected:
        raise SystemExit("No reference games selected.")

    dest_root = args.dest if args.dest.is_absolute() else ROOT / args.dest
    dest_root.mkdir(parents=True, exist_ok=True)
    lock = [materialize(game, dest_root, args.clean) for game in selected]
    lock_path = dest_root / "manifest-lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schema": "kadoka.reference-games-lock/1",
                "source_manifest": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"),
                "games": lock,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
