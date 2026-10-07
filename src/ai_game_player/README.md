# AI Game Player Python source

The package root is intentionally shallow: entry points live here and implementation lives in subsystem packages.

## Start here

- [`app.py`](app.py) — application bootstrap / current desktop entry point
- [`core/`](core/) — closed-loop orchestration, pipeline, engine, shared runtime models
- [`perception/`](perception/) — frame analysis, OCR, candidate generation, UI recognition
- [`decision/`](decision/) — Decision Context, providers, evaluation, outcome detection/fusion
- [`execution/`](execution/) — action execution, SafetyGuard, fail-safe runtime
- [`platform/windows/`](platform/windows/) — Windows capture, window selection, OS input
- [`storage/`](storage/) — config, history, Experience, artifacts, logs
- [`learning/`](learning/) — learning contracts and dataset generation
- [`runtime/`](runtime/) — Native Runtime capability/FFI boundary
- [`ui/`](ui/) — application shell and UI components
- [`applications/`](applications/) — application-layer workflows such as quality tooling
- [`e2e/`](e2e/) — reusable end-to-end runners

## Core flow

```text
platform/windows capture
 -> perception
 -> core/DecisionPipeline
 -> decision
 -> execution/Safety
 -> platform/windows input
 -> decision/outcome
 -> storage / learning
 -> next observation
```

## Layout rules

- Keep OS-specific code under `platform/` or the Native Runtime boundary.
- Keep deterministic Safety independent from model/provider quality.
- Keep persistent data handling under `storage/`; avoid ad-hoc writes from UI code.
- Add a new top-level package only for a stable subsystem responsibility.
- Do not return to one-file-per-feature at the package root.

See [the repository README](../../README.md), [AGENTS.md](../../AGENTS.md), and [runtime architecture](../../doc/architecture/runtime_layers.md).
