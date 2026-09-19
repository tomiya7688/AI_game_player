# Inference Provider API v1

## Goal

Kadoka Core must not depend directly on one model server or vendor API.

```text
Kadoka Core
  -> Kadoka Inference Provider API v1
      -> Bundled Local Service
      -> Ollama Adapter
      -> OpenAI-compatible Adapter
      -> Custom Adapter / Converter
```

The public contract is intentionally explicit. Adapter code may be thin, while the Core keeps one stable request/response model.

## Protocol

Initial HTTP endpoints:

- `GET /kadoka/v1/health`
- `GET /kadoka/v1/capabilities`
- `GET /kadoka/v1/models`
- `POST /kadoka/v1/inference`
- `POST /kadoka/v1/cancel` when cancellation is advertised

Streaming is intentionally not standardized in protocol v1. A v1 Provider MUST advertise `streaming=false`; a future wire-format change requires a protocol revision.

The machine-readable source of truth is:

- `schemas/inference_provider/v1/provider_manifest.schema.json`
- `schemas/inference_provider/v1/inference_request.schema.json`
- `schemas/inference_provider/v1/inference_response.schema.json`
- `schemas/inference_provider/v1/health_response.schema.json`
- `schemas/inference_provider/v1/capabilities_response.schema.json`
- `schemas/inference_provider/v1/models_response.schema.json`
- `schemas/inference_provider/v1/cancel_request.schema.json`
- `schemas/inference_provider/v1/cancel_response.schema.json`

Protocol version and Application version are independent. v1 stays v1 until this contract changes.

## Decision safety boundary

For `operation=decision`, the request carries:

- structured `decision_context`
- the complete `allowed_action_ids`
- a `candidate_selection` response format

The response may return only `selected_action_id`, optional confidence, and an explanation. Kadoka Core MUST reject a returned ID that is not present in `allowed_action_ids`.

Provider output never bypasses Action Safety, SafetyGuard, target validation, lease/watchdog, or Executor validation. A remote model does not receive unrestricted coordinate/input authority.

## Operations

v1 defines three normalized operations.

### decision

Select one already validated ActionCandidate ID.

### outcome_assessment

Return `success / failure / ongoing / unknown` with confidence and evidence.

### text_generation

General normalized text/JSON generation for adapters and future non-action tasks. This operation does not grant execution authority.

## Errors and lifecycle

Responses distinguish invalid request, unsupported protocol/operation, model unavailable, schema violation, deadline, rate limit, authentication, provider unavailable, provider error, and cancellation.

Timeout/cancel/error is data, not an implicit permission to relax Safety or send more private data.

## Privacy

Bundled Local Service is the default. It binds to localhost and keeps normal inference on-device.

Remote adapters MUST use `privacy.remote_transmission_allowed=true` and declare the transmitted data classes. The current classes are:

- decision_context
- ocr_text
- cropped_image
- raw_frame
- history
- trace

Credentials never belong in Decision Context, Trace, Replay, or normal logs.

See Issue #123 for the full privacy policy.

## Bundled local model profiles

`config/default_models.json` is the machine-readable candidate list.

### smoke-tiny

SmolLM2-135M-Instruct Q4_K_M class. The current pinned artifact is about 105 MB and exists to verify startup, Provider API, structured decision parsing, and CI. It is not a quality baseline.

### standard-small

Qwen3.5-2B Q4_K_M class. The current pinned artifact is about 1.40 GB. It is the default local Decision candidate only after GTX 1080 8 GB acceptance is measured while the rest of Kadoka is also running.

The default path is text-only. Multimodal projector weights are not loaded unless a capability explicitly needs them.

## Adapter author requirements

An Adapter should:

1. implement or expose Provider API v1,
2. translate Kadoka normalized input into its vendor/runtime request,
3. translate the result back into the strict Kadoka response schema,
4. preserve request/trace IDs,
5. obey deadline/cancellation,
6. never synthesize an Action ID outside the provided candidate set,
7. declare whether any data can leave the device,
8. keep credentials outside normal payload/log fields.

Deployment kind describes the Provider endpoint that Kadoka talks to:
- `bundled_local`: Kadoka-managed loopback sidecar
- `local_external`: user/adapter-managed local endpoint; it may itself bridge to a remote vendor
- `remote_direct`: Kadoka talks directly to a remote HTTPS Provider endpoint

Whether data can leave the device is declared independently by `privacy.may_transmit_off_device`.

Vendor compatibility belongs to the Adapter. Kadoka's public protocol does not copy a vendor API as its source of truth.


## GTX 1080 backend note

The reference GTX 1080 is Pascal compute capability 6.1. Kadoka must not assume that an upstream generic Windows CUDA prebuilt contains sm_61 kernels.

For the `standard-small` acceptance run, compare:

1. a Kadoka-bundled CUDA 12 build that explicitly includes sm_61,
2. the Vulkan backend,
3. CPU fallback.

The selected bundle backend is based on measured end-to-end latency, peak VRAM, stability, and coexistence with Capture/Recognition/Safety. End users must not be asked to install CUDA Toolkit, CMake, or a compiler.


## CI acceptance and 1.0 release gate

Do not run the full GTX 1080 gameplay benchmark on every pull request.

### Pull request CI

For changes that affect inference, runtime, model profiles, or packaging:

- `smoke-tiny` remains the cheap contract/startup model.
- `standard-small` must actually start and complete at least one Provider API Decision inference.
- Long gameplay, p50/p95 characterization, and extended stability testing are not PR requirements.
- Model artifacts may be cached by source/file/SHA-256.

If NVIDIA VRAM telemetry is available, run the complete Kadoka acceptance command under:

```text
python tools/check_gpu_vram_budget.py --limit-mib 6144 -- <acceptance command>
```

The checker records total `nvidia-smi memory.used` before startup and samples while the complete acceptance command is running. The Gate uses the peak increase over that baseline.

- measurable peak increase `<= 6144 MiB`: pass the VRAM budget
- measurable peak increase `> 6144 MiB`: fail
- VRAM telemetry unavailable: report `unavailable`; do not fail the VRAM item only
- inference/process failure: fail regardless of VRAM telemetry

The 6 GiB limit intentionally leaves headroom on an 8 GB reference GPU for Windows, graphics, Capture/Recognition, and other Kadoka work.

### Before 1.0.0 release

Run a separate real-hardware Release Gate on a GTX 1080 8 GB machine with the rest of Kadoka active.

This Gate includes full memory/latency/stability measurement and actual gameplay. The game does not need to be an MIT/public CI asset; a locally and legally usable game is acceptable. If the default model cannot play the selected real game adequately enough to satisfy the release acceptance scenario, reject that model/profile as the default candidate.

PR CI protects against obvious breakage and resource growth. The Release Gate decides real-world fitness.


### smoke-tiny gameplay endurance

The tiny model is deliberately cheap enough to do more than a one-request smoke test.

Once the bundled Local Provider and Level 1 runner exist, routine CI should run a roughly 600-second 2048 campaign with `smoke-tiny` as the actual Decision Provider. If the game reaches a terminal state, restart it and continue until the campaign deadline.

This is a robustness/safety Gate, not a release-quality ranking of the tiny model. Score, max tile, episode count, action count, latency, and recovery count are recorded as metrics. The hard Gate is protocol validity, candidate grounding, Safety integrity, execution continuity, bounded recovery, and absence of Provider/runtime crashes.

The standard-small and pre-1.0.0 Gates remain separate:
- smoke-tiny: long CI gameplay endurance
- standard-small: lightweight real inference + optional VRAM budget in PR CI
- pre-1.0.0: GTX 1080 real-hardware quality/performance/playability acceptance
