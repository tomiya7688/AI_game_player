# CI Reference Games

Kadoka keeps the Reference Game definitions in `config/e2e_reference_games.json`.

The upstream games are **not copied into the main source tree**. CI materializes only the pinned commit into `build/ci-games/` by running:

```powershell
python tools/fetch_ci_games.py --all --clean
```

This avoids accidental upstream drift, keeps third-party source out of Kadoka's language/source discovery, and preserves each upstream repository's own license files.

The command refuses to overwrite an existing `build/ci-games/manifest-lock.json`, including when `--clean` is specified. To run it again, first inspect and preserve any needed lock data, then remove that lock file explicitly. The command reserves the lock path before fetching. An interrupted or failed fetch can leave an empty or incomplete lock, which must also be inspected and removed explicitly before retrying. The game checkout checks still reject local changes before `--clean` removes an existing checkout.

Current levels:

1. 2048 — MIT
2. Memory Game — MIT
3. RuggRogue — MIT code with additional upstream asset license notices
4. Pygame Tetris — MIT

Changing a pinned commit is a reviewed source update and requires re-checking license/provenance.
