# Optional Capability Manifest

Optional features declare their capability IDs, required and optional dependency IDs, lifecycle, configuration namespace, and fallback in `config/capability_manifest.schema.json`. `CapabilityManifest.from_dict()` performs runtime validation without adding a JSON Schema library dependency.

`contract_version` must match `RUNTIME_CONTRACT_VERSION`; `schema_version` identifies the manifest schema. `lifecycle` records the feature maturity (`experimental`, `beta`, `stable`, or `deprecated`). Optional dependencies require an explicit `degraded_mode`. A fallback must be registered and declare the same capabilities; a fallback cycle is invalid.

```json
{
  "schema_version": 1,
  "manifest_id": "ocr.tesseract",
  "contract_version": 1,
  "capabilities": ["ocr.text"],
  "required_dependencies": ["pillow"],
  "optional_dependencies": ["language-data"],
  "lifecycle": "stable",
  "fallback_manifest_id": "ocr.basic",
  "degraded_mode": "ocr-without-language-data",
  "config_namespace": "features.ocr.tesseract"
}
```

Register manifests first, then call `CapabilityManifestRegistry.validate(available_dependencies)` to check fallback references, compatibility, capability coverage, cycles, and required dependencies that have no fallback. `manifests_for(capability)` provides discovery metadata without selecting an implementation.

## Composition Resolver

`CompositionResolver` in `runtime/composition.py` binds manifest IDs to provider factories. Resolution is explicit per capability: a caller may provide a preferred manifest ID, or use the resolver's configured preference; without one, registration order is the deterministic default. The resolver checks declared required dependencies, initializes the provider, and requires a passing `is_healthy()` check. Failures follow the declared fallback chain and are returned as structured reasons on `ResolvedFeature`; optional dependency absence or unknown availability marks the result degraded and includes the declared degraded mode.

The returned `LifecycleHandle` owns the selected provider and closes/shuts it down once, including context-manager use. A provider whose initialization or health check fails is cleaned up before trying its fallback. This resolver is intentionally separate from the GUI/Application composition root; Issue #147 remains responsible for wiring production components into the application.
