# First-party and bundled player asset licenses

Repository root `LICENSE` applies to project code, documentation, and
first-party non-character software assets unless another license is declared.

## Development player: Wise Misk / 賢者ミスク

The current 0.1.0 development application displays Wise Misk in its Player Seat.
The bundled `src/ai_game_player/assets/players/wise_misk/pack.json` declares
**Tomiya Character License v1.0.1** for `wise_misk.png`, records its SHA-256,
and identifies upstream asset `tomiya.wise-misk.original` at commit
`2b036d9fc8d175e2bef17916d59385db6b548ad7`. The manifest's license-detail
URL follows upstream `main`; that URL is not pinned to the image source commit.

Canonical license/status repository:

https://github.com/tomiya7688/Tomiya_character_license

The development wheel/sdist and Windows bundle include this pack. The planned
1.0.0 MIT-focused release excludes Wise Misk and uses Super Icon instead;
release-specific asset replacement is not implemented yet. Current development
packages must not be described as already satisfying that release boundary.

## 1.0.0 release player: すーぱーあいこん

The 1.0.0 MIT release profile uses the **すーぱーあいこん** asset set instead
of Wise Misk.

Source:

https://github.com/tomiya7688/super_icon_license

License: **MIT License**

The upstream repository contains its own MIT `LICENSE.md`. Release packaging
must preserve the copyright/license notice together with the copied assets.

## Kadoka / Maru

Kadoka and Maru assets are governed by the **Obake License** and are not part of
the 1.0.0 MIT release asset payload.

Official Obake License source:

https://github.com/tomiya7688/Obake_Lisense

These characters may be added in later multi-license releases.

## Release boundary

### 1.0.0
- project code/docs: MIT
- bundled first-party player asset: すーぱーあいこん / MIT
- Wise Misk: development asset only; not bundled
- Kadoka / Maru: not bundled
- third-party runtime/model dependencies: retain upstream licenses and are
  acquired/recorded separately

### After 1.0.0
Later releases may include pre-trained/fine-tuned artifacts and additional
character packs. Those releases are expected to be **multi-license** and must
surface the license of every bundled model/asset/runtime.

See `THIRD_PARTY_NOTICES.md` and `doc/release_1_0.md`.


## Post-1.0 policy

The MIT-focused asset payload is a 1.0.0-only release choice.

Later releases may bundle Wise Misk (under its declared character license), Kadoka,
Maru, other characters, and official pre-trained/fine-tuned artifacts under a
multi-license distribution. There is no requirement to maintain a parallel
MIT-only asset set after 1.0.0.

Every bundled Player Asset Pack still carries its own license/source metadata.


## Human-readable intent

The reasons behind the 1.0 MIT profile, Wise Misk's declared character license, Super
Icon's role, and later multi-license releases are explained in
`LICENSE_INTENT.md`.

That document is explanatory only. The applicable license text remains the
source of legal permissions and conditions.


## Why this split exists

This file records which license applies. The project rationale for using a
MIT-focused 1.0 release profile, a different development player, and
multi-license later releases is documented in:

`doc/licensing_rationale.md`
