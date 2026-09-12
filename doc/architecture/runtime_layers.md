# Runtime Architecture

Kadoka is intentionally modular internally while remaining minimal for ordinary users.

## Layers

```text
Application / UI
    |
AI / orchestration layer
    |
Runtime capability boundary
    |
Native Runtime (C++) / high-level providers
    |
Windows / hardware
```

The boundary is capability-based rather than language-based. A backend may provide one or more of:

- capture
- input
- safety
- fast CV

`src/ai_game_player/runtime/` defines the high-level contract. `native/` owns the stable C ABI for native implementations.

## Rules

1. Performance-, realtime-, OS-, and safety-sensitive work should be evaluated for native implementation before coding.
2. Keep language crossings coarse grained: frame, observation, candidate set, decision, or batch. Do not cross FFI per pixel/token/candidate.
3. Count FFI, IPC, serialization, copying, scheduling, and deployment overhead in end-to-end latency decisions.
4. Prefer zero-copy/shared buffer techniques for large image/tensor payloads when measurements justify them.
5. Do not expose language-specific objects as the only cross-layer contract. Version every external contract.
6. Safety-critical stop/release behavior must remain available even when the upper AI layer is unhealthy.
7. Optional providers must not become required dependencies of the minimal runtime unless they are safety-required.

## Repository ownership

- `src/ai_game_player/`: application and high-level AI/orchestration code.
- `src/ai_game_player/runtime/`: language-neutral runtime capability contracts and backend selection.
- `native/`: C/C++ runtime ABI and native implementations.
- `profiles/`: user-selectable configuration profiles; basic operation must not require manual editing.
- `plugins/`: extension contract notes/examples; plugins may not depend on private internals.
- `doc/`: detailed architecture and feature contracts.

## Distribution rule

Development may require multiple toolchains/runtimes. End-user distribution must absorb that complexity. A normal user must not be required to install Python, pip, CMake, MSVC, Lua, Node.js, .NET SDK, or edit PATH/environment variables manually.

Release acceptance eventually requires clean-Windows installation/bundle testing and automatic initialization of bundled runtimes/dependencies/models. Vendor/OS drivers that cannot legally or technically be bundled must be detected and explained clearly.

## Customization

Keep Basic usage small, but preserve Advanced / Expert / Developer entry points. Model/provider selection, policy/rules, evaluator composition, optional features, tracing, performance budgets, plugins, and profiles should remain customizable behind versioned contracts. Minimum fail-closed/emergency-stop guarantees are not removable by an expert profile.
