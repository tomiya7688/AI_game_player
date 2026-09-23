# License Intent and Distribution Policy

This document explains **why** the project uses different licenses and release
profiles. It is written for humans and does not replace any license text.

If this explanation and an applicable license ever disagree, the actual license
text for that code, model, or asset controls.

## Why 1.0.0 is intentionally simple

Version 1.0.0 is intended to be the easiest release to download, inspect,
modify, redistribute, and use as a base for experimentation.

For that reason, the project-owned 1.0.0 payload is intentionally kept close to
a single MIT-licensed core.

The 1.0.0 release uses the MIT-licensed Super Icon / すーぱーあいこん player
asset instead of forcing the licensing decisions for later character assets
into the first release.

This is not a statement that every future project asset should use MIT.

It is a deliberate usability choice for the first stable release.

## Why later releases may be multi-license

Later releases are expected to contain things such as:

- official pre-trained or fine-tuned model artifacts,
- additional character assets,
- optional runtimes,
- model adapters,
- external or third-party components.

Artificially forcing all of those things into one license would either make
some features impossible to distribute or force the project to choose licenses
for assets before their intended usage is understood.

Therefore 1.1+ may be a multi-license distribution.

The goal is not to make later releases harder to use. The goal is to let each
artifact use an appropriate license while making the applicable license obvious.

## Wise Misk / 賢者ミスク

Wise Misk is used as the development player while the 1.0.0 application is
being built.

Its final artwork license is intentionally **undecided**.

This is not because the project wants to prevent people from using Wise Misk.
It is because a character identity, artwork, model lineage, and community
fine-tunes have different concerns from ordinary source code, and we do not
want to lock those questions into MIT merely to finish 1.0.0.

Wise Misk is therefore excluded from the 1.0.0 release asset payload.

A future character-oriented open/free license may be designed separately.

## Super Icon / すーぱーあいこん

Super Icon exists specifically as a broadly reusable MIT-licensed visual asset.

For 1.0.0 it is the default bundled Player Asset Pack because it lets the first
stable release remain easy to redistribute and remix without requiring a
character-specific license decision.

## Kadoka / Maru

Kadoka and Maru use the Obake License.

They are not included in the 1.0.0 release payload.

This is a release-scope decision, not a relicensing decision.

## Third-party models and runtimes

Third-party artifacts keep their upstream licenses.

The 1.0.0 root executable may automatically acquire required pinned model or
runtime artifacts, verify their hashes/provenance, and present their license
information.

The project does not pretend those artifacts are MIT merely because the
application itself is MIT-licensed.

## What users should see

Users should not need to read a full license every time they choose an asset.

Selection UI should normally show:

- asset/model name,
- license name,
- source/publisher when useful,
- a **Details** action.

Details should show:

- applicable license,
- source,
- version/hash when relevant,
- required notices,
- this human-readable intent explanation when available.

## What this policy is trying to optimize

The policy has four goals:

1. Make 1.0.0 easy to download, redistribute, modify, and learn from.
2. Avoid prematurely forcing character/model assets into unsuitable licenses.
3. Keep later feature development free from an artificial MIT-only constraint.
4. Make every license boundary understandable instead of surprising.

## 1.0 users who remain on 1.0

The project does not promise a parallel MIT-only build for every later release.

Instead, the 1.0 Core is kept deliberately small and extensible. Later releases
publish Update Content Sheets describing feature contracts, dependencies,
licenses, and possible MOD/Feature Pack backport paths.

That lets a user remain on the simple 1.0 base and selectively add later
functionality if they want to do so.
