# Third-party notices

Third-party code, runtimes, models, games, fonts, and assets keep their
upstream licenses. The repository MIT license does **not** relicense them.

This file is the human-facing index. Machine-readable source/version/license
metadata is kept as close as possible to each external artifact.

## Reference games

Pinned CI game sources and their license files are declared in:

- `config/e2e_reference_games.json`
- `third_party/ci_games/README.md`

CI materializes the pinned sources only for testing. Upstream license files
must be preserved in the materialized artifact.

## Default model candidates

Model source, file, SHA-256, and license metadata are declared in:

- `config/default_models.json`

Model weights keep their upstream licenses and are not relicensed by this
project.

## Bundled inference/runtime dependencies

Any runtime shipped in a release, including the Local Inference Service backend
and native libraries, must have its upstream license and required notices
included in the release bundle.

## Release requirement

Before 1.0.0, the release pipeline must generate or verify a complete inventory
of actually bundled third-party artifacts. If an artifact requires attribution,
NOTICE text, source offer, or other redistribution material, the release must
include it before publication.

Kadoka/Maru first-party character assets are not third-party; see
`ASSET_LICENSES.md`.
