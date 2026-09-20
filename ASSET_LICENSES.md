# First-party asset licenses

Repository root `LICENSE` applies to first-party code, documentation, and
non-character software assets unless a file or directory declares another
license.

## Kadoka / Maru character assets

Any first-party asset depicting **Kadoka** or **Maru**, including derived poses,
animations, expressions, and equivalent character artwork, is excluded from
the repository MIT license and is governed by the **Obake License**.

Official Obake License source:

https://github.com/tomiya7688/Obake_Lisense

When Kadoka/Maru assets are added to this repository or a release bundle, keep
their license metadata adjacent to the asset or in the generated release
license inventory.

## Other first-party assets

First-party assets that are not Kadoka/Maru character assets use the repository
MIT license by default unless they carry an explicit different license.


## Wise Misk / 賢者ミスク

Wise Misk character assets use the **MIT License**, matching the AI Game Player
project code license.

Canonical character-license repository:

https://github.com/tomiya7688/Tomiya_character_lisence

Wise Misk is the default player character/seat occupant for the application.
This does not change the separate Obake License applied to Kadoka/Maru assets.


## 1.0.0 distribution boundary

The 1.0.0 release intentionally ships only the MIT-licensed first-party character scope.

- Wise Misk: eligible for 1.0.0
- Kadoka: Obake License, deferred to 1.1+
- Maru: Obake License, deferred to 1.1+

Kadoka/Maru remain documented here so their license boundary is explicit, but their assets are not part of the 1.0.0 product distribution.
