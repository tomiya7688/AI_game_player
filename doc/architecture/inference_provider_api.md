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

The machine-readable source of truth is:

- `schemas/inference_provider/v1/provider_manifest.schema.json`
- `schemas/inference_provider/v1/inference_request.schema.json`
- `schemas/inference_provider/v1/inference_response.schema.json`

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

Vendor compatibility belongs to the Adapter. Kadoka's public protocol does not copy a vendor API as its source of truth.
