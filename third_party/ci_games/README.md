# CI Reference Games

Kadoka keeps the Reference Game definitions in `config/e2e_reference_games.json`.

The upstream games are **not copied into the main source tree**. CI materializes only the pinned commit into `build/ci-games/` by running:

```powershell
python tools/fetch_ci_games.py --all --clean
```

This avoids accidental upstream drift, keeps third-party source out of Kadoka's language/source discovery, and preserves each upstream repository's own license files.

Current levels:

1. 2048 — MIT
2. Memory Game — MIT
3. RuggRogue — MIT code with additional upstream asset license notices
4. Pygame Tetris — MIT

Changing a pinned commit is a reviewed source update and requires re-checking license/provenance.
