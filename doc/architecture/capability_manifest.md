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

Register manifests first, then call `CapabilityManifestRegistry.validate(available_dependencies)` to check fallback references, compatibility, capability coverage, cycles, and required dependencies that have no fallback. `manifests_for(capability)` provides discovery metadata without selecting an implementation. Runtime selection and fallback execution are deliberately left to the separate Composition Resolver in Issue #220.
