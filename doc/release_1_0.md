# 1.0.0 Release Scope and Acceptance

Issue #19 is the priority/source-of-truth. This document records the repository
side of the accepted 1.0.0 product boundary.

## Product scope

The normal/default player character is **Wise Misk (賢者ミスク)**. The UI provides a dedicated player seat, and Wise Misk occupies it by default while the game-playing session is active or ready.

1.0.0 targets Windows x64 and local-first game playing.

The normal user path is:

```text
Download release
 -> run the executable at the distribution root
 -> required runtime/model/package assets are resolved automatically
 -> choose a game/window
 -> Start / Stop
```

A normal user must not need to install or operate Python, pip, CMake, a compiler,
Ollama, or another model server manually.

The final Product/Engine name is tracked by Issue #154. Do not treat "Kadoka" as
the final Product name until that Issue records the previously chosen name or a
replacement decision.

## Default inference

- Bundled local inference is the default.
- Remote inference is optional and OFF by default.
- The standard-small profile is a release candidate until real-hardware
  acceptance passes.
- smoke-tiny is a CI/endurance profile, not a release-quality baseline.
- NoisyLanguageProvider is a permanent bad-AI robustness fixture, not a
  production fallback.

## Privacy defaults

- telemetry: OFF
- Remote Provider: OFF
- raw-frame persistence: OFF
- replay persistence: OFF
- trace: local-only with bounded retention
- diagnostic export: explicit user action
- remote data transmission: explicit Provider opt-in and data-class disclosure
- credentials: never in Decision Context, Trace, Replay, or normal logs

## Routine CI

- smoke-tiny: approximately 600 seconds of Level 1 real-game endurance when the
  runner is available
- standard-small: at least one real inference for relevant changes
- measured NVIDIA VRAM peak increase: at most 6144 MiB when telemetry is
  available
- NoisyLanguageProvider: permanent synthetic closed-loop robustness and
  non-LLM Core performance regression gate

## Pre-1.0.0 release acceptance

Issue #179 owns the executable harness.

Automated release testing runs a reference game for roughly 30 minutes or a
clear game milestone.

The local real-game gate runs:

- GTX 1080 8 GB reference hardware
- standard-small
- Capture / Recognition / Decision / Safety / Input / Outcome concurrently
- a locally and legally usable game; it does not need to be MIT/OSS
- 30 minutes or one clearly defined gameplay segment
- no human candidate edits or mid-run rescue

Hard failures include crash, OOM, Safety bypass, invalid candidate execution,
unbounded loop, and unrecoverable stall.

The report records at least peak VRAM/RAM, decision latency p50/p95, action and
recovery counts, stability, duration, and stop reason.

If the standard-small candidate cannot satisfy practical playability on the
reference machine, it is rejected as the default model candidate.

## Licensing

- first-party code/docs/non-character software assets: MIT
- Wise Misk character assets: MIT
- Obake License assets (including Kadoka/Maru): **not included in the 1.0.0 product scope; target 1.1+**
- third-party artifacts: their original licenses and required notices

See `LICENSE`, `ASSET_LICENSES.md`, and `THIRD_PARTY_NOTICES.md`.


## 1.0.0 licensing boundary

Version 1.0.0 intentionally keeps the shipped first-party product scope MIT-only.

Included in 1.0.0:
- first-party code and documentation under MIT
- Wise Misk assets under MIT
- third-party components only under their own redistribution-compatible upstream licenses, with notices

Deferred to 1.1 or later:
- Kadoka assets under the Obake License
- Maru assets under the Obake License
- Obake-license-dependent Character Mode/UI integrations

This does not relicense Kadoka/Maru. It only keeps those assets/features out of the 1.0.0 distribution scope.


## 1.0.0 quality bar

Version 1.0.0 is the first release intended to be easy to download and use
under an MIT-first-party scope. It is therefore not released merely because a
technical demo works.

A release candidate must pass the complete normal-user lifecycle:

```text
Download / unpack
 -> launch distribution-root executable
 -> first-run runtime/model setup
 -> choose game/window
 -> Start
 -> sustained autonomous play
 -> Stop
 -> clean shutdown
 -> relaunch
 -> start another session
```

Release-blocking failures include:

- crash or OOM,
- Safety bypass,
- invalid Action execution,
- unbounded loop or unrecoverable stall,
- held input remaining after Stop,
- orphan Local Inference/runtime process after shutdown,
- first-run setup requiring manual Python/Ollama/compiler/model-server work,
- failure that is only visible as an internal traceback with no user-readable
  error,
- inability to relaunch into a usable state,
- default model failing the real-game playability acceptance.

The 30-minute-class automated/reference test and GTX 1080 real-game test are
necessary but not sufficient; startup, setup, stop, shutdown, cleanup, and
relaunch are part of the same 1.0.0 acceptance.


## 1.0.0 learning requirement

Version 1.0.0 must not ship as a set of permanently fixed model artifacts.

Each bundled learnable component must expose at least one supported improvement
path through the common Learning Runtime:

```text
Play Experience
 -> versioned Dataset
 -> component-specific Trainer / Update Adapter
 -> Challenger Artifact
 -> before/after Evaluation
 -> Promote or Reject
 -> Rollback
```

The minimum 1.0 scope includes:

- Decision LLM: lightweight local update such as LoRA/adapter
- OCR: correction-driven update path
- UI recognition/detection: labeled sample/prototype/classifier update path
- Embedding: prototype/memory update, with trainer extension where supported
- Outcome/Reliability/Evaluator: calibration/threshold/lightweight-model update

Learning does not require full foundation-model training. Prototype updates,
calibration, adapters, and other bounded versioned updates count when they can
be evaluated and rolled back.

Release requirements:

- local-first and user-triggered by default
- normal gameplay inference and training runtime are isolated
- Champion is never modified in-place
- Challenger is evaluated before promotion
- regression rejects promotion
- rollback is available
- dataset/artifact provenance is recorded
- training failure cannot break the normal gameplay path
- the clean 1.0 distribution can reach the Learning UI/workflow without manual
  Python/compiler/trainer-environment setup

Implementation tracking: Issues #180-#188 plus the Experience/Learning
infrastructure in #36/#37/#166-#173.


## Player profile customization

The default 1.0 player profile is Wise Misk / 賢者ミスク, but the visible
player identity is not the same thing as the technical model identity.

Basic UI must expose an obvious Player Profile control near the Player Seat for:

- display name change,
- player image change,
- reset to the default Wise Misk profile.

Changing the player name/image must not rename or mutate the underlying model,
provider, adapter, artifact hash, publisher, or provenance.

Conversely, switching or fine-tuning a model must not overwrite a user's chosen
player name/image unless the user explicitly requests it.

Community fine-tunes are encouraged to use distinct technical model names while
describing their origin separately, for example "fine-tuned from Wise Misk".
