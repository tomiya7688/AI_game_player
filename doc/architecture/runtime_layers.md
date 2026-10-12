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

## Application workflow boundaries

This section is for developers changing how the desktop UI captures a game screen, prepares a decision, or starts a session. Tkinter is Python's desktop UI toolkit. The `Application` class owns Tkinter controls, reads the current screen and candidate JSON fields, checks live-input permission, and displays results. The `Application` class delegates screen capture and frame analysis to `LiveObservationService`. The service owns one bounded latest-frame producer, so the capture button and continuous-session observer use the same OS capture stream.

A preview or recorder can call `subscribe_frames()` to receive the producer's `FramePacket` values. Each `FramePacket` contains one captured frame, a frame ID, and the source generation; the source generation increases when the selected window changes. A latest-mode queue replaces an unread frame with the newest frame. An ordered-mode queue preserves arrival order up to its configured capacity and discards a new frame when the queue is full. The subscription API shares capture data; it does not implement video recording.

`LiveObservationService` rejects a frame whose capture time is older than its configured freshness limit. The service stores the producer-scoped `frame_id`, `source_generation`, and capture `monotonic_ns` in `ScreenObservation.features.capture_metadata`. The service does not read or update Tkinter widgets.

The `Application` class delegates provider, outcome, runtime-construction, settings-persistence, candidate-parsing, and candidate-evaluation work to `ApplicationWorkflowService`. The workflow service receives ordinary Python values and injected `ConfigStore`, `RuntimeComposition`, and `MemorySource` dependencies, so tests can exercise those operations without creating a Tk window. A `ScreenObservation` contains the screen ID, pixel dimensions, recognized text, and visual features. A successful candidate evaluation updates `MemorySource`; the next `SessionRuntime` reads the same observation and candidate list.

The capture button currently writes the returned `ScreenObservation` into the editable observation JSON field. The decision and execution commands read that field together with the separate editable candidate JSON field. Automatic generation of action candidates from a captured screen is not part of these service boundaries; Issue #337 tracks that integration.

```text
Tkinter Application
  -> LiveObservationService -> SharedFrameProducer -> ScreenObservation.features.capture_metadata
  -> ApplicationWorkflowService -> ConfigStore / MemorySource / RuntimeComposition
  -> GameSessionController -> SessionRuntime
```

`GameSessionController` owns start, step, loop, stop, and session-resource lifetime. `RuntimeComposition` creates the provider and decision pipeline that make up each `SessionRuntime`. The `Application` class displays controller state and delivers user commands; it does not assemble provider or pipeline dependencies directly.

`Application.start_loop()` validates the selected target and current settings, then calls `GameSessionController.start_loop()`. The controller owns the next-step timer and repeat/terminal stop decisions. `TkAfterScheduler` adapts Tkinter's `after()` and `after_cancel()` calls to the controller's scheduler contract, so the `Application` class does not keep a second loop flag or loop callback.

## Windows Graphics Capture for selected windows

The native runtime exposes an optional C ABI v1 Windows Graphics Capture (WGC) session for one HWND. The capability query reports whether the current Windows environment supports WGC. A session returns a frame lease containing BGRA dimensions, a session frame ID, a monotonic timestamp, and a borrowed opaque Direct3D texture handle. The caller may retain each frame lease independently; releasing a lease invalidates only that frame's borrowed texture handle.

The Python `WindowsScreenCapture` uses WGC only for a selected HWND and reuses that HWND's session across captures. The Python binding copies each texture into caller-owned packed BGRA bytes and releases the native lease before returning `ScreenFrame`; this path is not zero-copy. Desktop-wide capture continues to use the existing GDI implementation. If WGC is unavailable or capture fails, `WindowsScreenCapture` records the reason and attempts GDI fallback for the selected HWND. WGC support does not guarantee frames for minimized windows or protected content.

## FAST_CV frame preprocessing

The optional `FAST_CV` capability exposes a coarse frame-preprocessing operation through C ABI v1. The caller lends a BGRA frame synchronously; the native runtime returns RGB/brightness summaries, a perceptual sample hash, and filtered bright-region bounds in caller-owned output buffers. If the region buffer is too small, the caller retries with the reported required capacity. The result reports elapsed time, logical frame-payload bytes processed, region-payload bytes written, and input-copy count/bytes; frame-payload bytes exclude row padding, and the current implementation borrows the input and reports zero copies. The Python binding accepts provider-reported copy metrics and sums them across buffer-capacity retries. This is an explicit runtime API only: `FrameAnalyzer` keeps the Python reference implementation as its default fallback, and applications must inject a native preprocessor deliberately.

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
