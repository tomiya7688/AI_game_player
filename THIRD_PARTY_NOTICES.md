# Third-party notices

Third-party code, runtimes, models, games, fonts, and assets keep their
upstream licenses. The repository MIT license does **not** relicense them.

This file is the human-facing index. Machine-readable source/version/license
metadata is kept as close as possible to each external artifact.

## Reference games

Pinned CI game sources are declared in `config/e2e_reference_games.json`.

Current declared sources:

| Artifact | Source | License |
|---|---|---|
| 2048 | `gabrielecirulli/2048` | MIT |
| Memory Game | `sen-ltd/memory-game` | MIT |
| RuggRogue code | `tung/ruggrogue` | MIT |
| RuggRogue tile graphics | upstream asset | CC0 |
| RuggRogue GohuFont | upstream asset | WTFPL |
| Pygame Tetris | `Owsap/pygame-tetris` | MIT |

CI materializes pinned commits only for testing. Upstream license files and
asset notices must be preserved in the materialized artifact.

## Default model candidates

Model source, file, SHA-256, and license metadata are declared in
`config/default_models.json`.

Current declared candidates:

| Profile | Source model | License |
|---|---|---|
| smoke-tiny | `HuggingFaceTB/SmolLM2-135M-Instruct` | Apache-2.0 |
| standard-small | `Qwen/Qwen3.5-2B` | Apache-2.0 |

Model weights keep their upstream licenses and are not relicensed by this
project.

## Bundled inference/runtime dependencies

Any runtime shipped in a release, including the Local Inference Service backend
and native libraries, must have its upstream license and required notices
included in the release bundle.

Runtime candidates are not listed as shipped dependencies until they are
actually selected and bundled.

## Release requirement

Before 1.0.0, the release pipeline must generate or verify a complete inventory
of the artifacts actually included in the distribution. If an artifact requires
attribution, NOTICE text, source offer, or other redistribution material, the
release must include it before publication.

Kadoka/Maru first-party character assets are not third-party; see
`ASSET_LICENSES.md`.


## 1.0.0 acquisition boundary

The 1.0.0 project release artifact does not treat third-party model weights as
MIT project assets.

Current model candidates such as SmolLM2 and Qwen retain Apache-2.0 and are
intended to be acquired automatically on first run for the strict 1.0 release
profile. The application must show the dependency/model license name, verify
the pinned SHA-256/provenance, and keep the upstream license information
available from the UI.

Later distributions may directly bundle official pre-trained/fine-tuned model
artifacts. Such distributions are explicitly multi-license.
