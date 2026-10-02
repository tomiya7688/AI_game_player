# Native Runtime C ABI v1

`native/include/kadoka/runtime_api.h` is the public, C-compatible boundary between the high-level application and the C++ Native Runtime. C++ implementation details and STL types must not cross this boundary.

## Version and status contract

- `KADOKA_RUNTIME_ABI_VERSION` is the ABI version supported by this library.
- `kadoka_runtime_abi_version()` reports the library version without initializing runtime state.
- `kadoka_runtime_init()` requires an options structure whose `struct_size` covers the complete v1 prefix and whose `requested_abi_version` exactly matches the library. Unsupported versions return `KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI` and leave the output handle null.
- `kadoka_runtime_query()` reports ABI version and capability bits. Its output structure must be initialized with its size before calling.
- `KADOKA_RUNTIME_STATUS_OK` means the operation succeeded. Invalid arguments, unsupported ABI versions, and allocation failure have stable negative codes. No C++ exception may cross an exported function.

## Lifecycle

Before `kadoka_runtime_init()`, the caller initializes the output pointer to null. Each successful init returns one opaque handle. The caller owns it and must call `kadoka_runtime_shutdown(&handle)` exactly once. Re-initializing a non-null output pointer is rejected and retains the existing handle. Successful shutdown releases the handle and sets the caller's pointer to null; a second shutdown is rejected as an invalid argument. A failed init never transfers ownership. Separate instances have independent lifetime.

The caller must provide valid memory for each declared structure prefix and pass only live handles returned by this library. Handles must not be copied for ownership, fabricated, or used after shutdown. Calls on a handle must be serialized by the caller, including shutdown.

The v1 lifecycle currently owns only the runtime context. Capability query and the existing deterministic safety validation remain available through their stateless API functions. Future resource-owning components must attach lifetime to a runtime handle or a separately versioned coarse-grained contract.

## Coarse batch placeholder

`kadoka_runtime_process_batch()` accepts a size/version-tagged metadata envelope for one frame, observation, candidate set, decision, input batch, or control lease. It validates the runtime, structure sizes, exact ABI version, kind, and zero reserved field. Invalid requests leave result fields unchanged. Valid requests echo the batch ID and return `KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED` both as the function status and in the result. This entrypoint performs no capture, processing, or input, and query does not advertise those capabilities. Buffer payload/ownership is deferred to #233 and the Python binding to #232.

## Compatibility rules

- Existing v1 exported symbols and public structure layouts remain unchanged.
- Public structs start with `struct_size`; compatible extensions may only append fields while retaining the required v1 prefix. Breaking layout or semantic changes require a new ABI version.
- Callers set `struct_size` to the number of bytes they provide. Implementations validate the required v1 prefix and ignore unknown appended input fields.
- Crossings remain coarse-grained; pixel-, token-, and candidate-level FFI calls are prohibited.
