# First-party and bundled player asset licenses

Repository root `LICENSE` applies to project code, documentation, and
first-party non-character software assets unless another license is declared.

## Development player: Wise Misk / 賢者ミスク

Wise Misk is the default **development** player profile while 1.0.0 is being
built.

The final artwork license for Wise Misk is currently **undecided**.

Canonical license/status repository:

https://github.com/tomiya7688/Tomiya_character_lisence

Wise Misk artwork is therefore not part of the 1.0.0 release asset payload.
Development builds may use locally provided Wise Misk assets while its final
license is being decided.

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
