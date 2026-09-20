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
