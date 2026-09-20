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
- Kadoka/Maru character assets: Obake License
- third-party artifacts: their original licenses and required notices

See `LICENSE`, `ASSET_LICENSES.md`, and `THIRD_PARTY_NOTICES.md`.
