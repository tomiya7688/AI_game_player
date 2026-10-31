from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath


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
        timeout=120,
    )
    return result.stdout.strip()


def validate_game_target(game: dict[str, object], dest_root: Path) -> Path:
    """削除・取得前に識別子と固定SHAを検証し、保存先直下だけを許可します。"""
    for field, pattern in (
        ("id", r"[a-z0-9][a-z0-9_-]*"),
        ("repository", r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"),
        ("commit", r"[0-9a-f]{40}"),
    ):
        value = game.get(field)
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise ValueError(
                f"invalid reference-game {field}: expected a pinned source declaration"
            )
    paths = game.get("license_paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("reference-game license_paths must be a nonempty list")
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or ":" in path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or path == "."
        ):
            raise ValueError(
                "reference-game license paths must stay inside the checkout"
            )
    root = dest_root.resolve()
    expected = root / game["id"]
    target = expected.resolve()
    if target != expected or target.parent != root:
        raise ValueError("reference-game target redirects outside its named directory")
    if target.exists() and not target.is_dir():
        raise ValueError("reference-game target exists but is not a directory")
    return target


def materialize(
    game: dict[str, object], dest_root: Path, clean: bool
) -> dict[str, object]:
    game_id = str(game["id"])
    repository = str(game["repository"])
    expected_commit = str(game["commit"])
    target = validate_game_target(game, dest_root)

    if target.exists():
        repository_root = Path(
            run("git", "rev-parse", "--show-toplevel", cwd=target)
        ).resolve()
        remote = run("git", "remote", "get-url", "origin", cwd=target)
        if (
            repository_root != target
            or remote != f"https://github.com/{repository}.git"
        ):
            raise ValueError(
                f"{game_id}: existing directory is not the expected source checkout"
            )
        if run(
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored",
            cwd=target,
        ):
            raise ValueError(
                f"{game_id}: checkout has local changes; preserve them before fetching"
            )
        if clean:
            if run("git", "rev-parse", "HEAD", cwd=target) != expected_commit or run(
                "git",
                "rev-list",
                "--all",
                "--reflog",
                "HEAD",
                "--not",
                expected_commit,
                cwd=target,
            ):
                raise ValueError(
                    f"{game_id}: checkout has other local history; preserve it before cleaning"
                )
            shutil.rmtree(target)

    if not target.exists():
        target.mkdir(parents=True)
        run("git", "init", cwd=target)
        run(
            "git",
            "remote",
            "add",
            "origin",
            f"https://github.com/{repository}.git",
            cwd=target,
        )
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
        if not (target / str(path)).resolve().is_relative_to(target)
        or not (target / str(path)).is_file()
    ]
    if missing_licenses:
        raise RuntimeError(
            f"{game_id}: missing declared license files: {missing_licenses}"
        )

    return {
        "level": int(game["level"]),
        "id": game_id,
        "repository": repository,
        "commit": actual_commit,
        "license_spdx": str(game["license_spdx"]),
        "license_paths": list(game.get("license_paths", [])),
        "path": str(
            target.relative_to(ROOT) if target.is_relative_to(ROOT) else target
        ).replace("\\", "/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize pinned OSS games used by Kadoka CI/E2E validation."
    )
    parser.add_argument(
        "--all", action="store_true", help="Fetch all configured levels."
    )
    parser.add_argument(
        "--level",
        type=int,
        action="append",
        default=[],
        help="Fetch one level; repeatable.",
    )
    parser.add_argument(
        "--clean", action="store_true", help="Remove existing checkout before fetching."
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    levels = list(manifest["levels"])
    unknown = set(args.level) - {int(game["level"]) for game in levels}
    if unknown:
        parser.error(f"unknown reference-game levels: {sorted(unknown)}")
    selected = (
        levels
        if args.all or not args.level
        else [game for game in levels if int(game["level"]) in set(args.level)]
    )
    if not selected:
        raise SystemExit("No reference games selected.")

    dest_root = args.dest if args.dest.is_absolute() else ROOT / args.dest
    dest_root.mkdir(parents=True, exist_ok=True)
    lock_path = dest_root / "manifest-lock.json"
    try:
        lock_file = lock_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to overwrite existing lock file: {lock_path}"
        ) from exc
    with lock_file:
        lock = [materialize(game, dest_root, args.clean) for game in selected]
        lock_file.write(
            json.dumps(
                {
                    "schema": "kadoka.reference-games-lock/1",
                    "source_manifest": str(MANIFEST.relative_to(ROOT)).replace(
                        "\\", "/"
                    ),
                    "games": lock,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    print(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
