#include "kadoka/runtime_api.h"

#include <cassert>

namespace {

void test_batch_contract(const kadoka_runtime_handle* runtime) {
    kadoka_runtime_batch_request request{};
    request.struct_size = sizeof(request);
    request.abi_version = KADOKA_RUNTIME_ABI_VERSION;
    request.batch_id = 42u;
    request.kind = KADOKA_RUNTIME_BATCH_FRAME;
    struct ExtendedResult {
        kadoka_runtime_batch_result base;
        uint64_t extension;
    } result{{}, UINT64_C(0x12345678)};
    result.base.struct_size = sizeof(result);
    result.base.status = 777;

    assert(kadoka_runtime_process_batch(nullptr, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(kadoka_runtime_process_batch(runtime, nullptr, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(kadoka_runtime_process_batch(runtime, &request, nullptr) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    request.struct_size = sizeof(request) - 1u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    request.struct_size = sizeof(request);
    result.base.struct_size = sizeof(result.base) - 1u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    result.base.struct_size = sizeof(result);
    request.abi_version = KADOKA_RUNTIME_ABI_VERSION + 1u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI);
    request.abi_version = KADOKA_RUNTIME_ABI_VERSION;
    request.kind = 0u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    request.kind = KADOKA_RUNTIME_BATCH_CONTROL_LEASE + 1u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    request.kind = KADOKA_RUNTIME_BATCH_FRAME;
    request.reserved = 1u;
    assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(result.base.status == 777);
    assert(result.base.abi_version == 0u);
    assert(result.base.batch_id == 0u);

    request.reserved = 0u;
    for (uint32_t kind = KADOKA_RUNTIME_BATCH_FRAME; kind <= KADOKA_RUNTIME_BATCH_CONTROL_LEASE; ++kind) {
        request.kind = kind;
        assert(kadoka_runtime_process_batch(runtime, &request, &result.base) == KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED);
        assert(result.base.status == KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED);
        assert(result.base.abi_version == KADOKA_RUNTIME_ABI_VERSION);
        assert(result.base.batch_id == request.batch_id);
        assert(result.base.struct_size == sizeof(result));
        assert(result.extension == UINT64_C(0x12345678));
    }
}

}  // namespace

int main() {
    assert(kadoka_runtime_abi_version() == KADOKA_RUNTIME_ABI_VERSION);

    kadoka_runtime_handle* runtime = nullptr;
    assert(kadoka_runtime_init(nullptr, &runtime) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(runtime == nullptr);

    kadoka_runtime_options options{};
    options.struct_size = sizeof(options);
    options.requested_abi_version = KADOKA_RUNTIME_ABI_VERSION + 1u;
    assert(kadoka_runtime_init(&options, &runtime) == KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI);
    assert(runtime == nullptr);

    options.requested_abi_version = KADOKA_RUNTIME_ABI_VERSION;
    options.struct_size = sizeof(options) - 1u;
    assert(kadoka_runtime_init(&options, &runtime) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(runtime == nullptr);
    options.struct_size = sizeof(options);
    assert(kadoka_runtime_init(&options, nullptr) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(kadoka_runtime_init(&options, &runtime) == KADOKA_RUNTIME_STATUS_OK);
    assert(runtime != nullptr);
    auto* original = runtime;
    assert(kadoka_runtime_init(&options, &runtime) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(runtime == original);
    test_batch_contract(runtime);
    kadoka_runtime_handle* second = nullptr;
    struct ExtendedOptions {
        kadoka_runtime_options base;
        uint64_t extension;
    } extended_options{options, UINT64_C(0x12345678)};
    extended_options.base.struct_size = sizeof(extended_options);
    assert(kadoka_runtime_init(&extended_options.base, &second) == KADOKA_RUNTIME_STATUS_OK);
    assert(extended_options.extension == UINT64_C(0x12345678));
    assert(second != runtime);
    assert(kadoka_runtime_shutdown(&runtime) == KADOKA_RUNTIME_STATUS_OK);
    assert(runtime == nullptr);
    assert(kadoka_runtime_shutdown(&runtime) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(kadoka_runtime_shutdown(nullptr) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    test_batch_contract(second);
    assert(kadoka_runtime_shutdown(&second) == KADOKA_RUNTIME_STATUS_OK);

    assert(kadoka_runtime_query(nullptr) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    kadoka_runtime_info info{};
    info.struct_size = sizeof(info) - 1u;
    assert(kadoka_runtime_query(&info) == KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT);
    assert(info.abi_version == 0u);
    assert(info.capabilities == 0u);
    info.struct_size = sizeof(info);
    assert(kadoka_runtime_query(&info) == 0);
    assert(info.abi_version == KADOKA_RUNTIME_ABI_VERSION);
    assert((info.capabilities & KADOKA_CAP_SAFETY) != 0u);
    assert(info.capabilities == KADOKA_CAP_SAFETY);
    struct ExtendedInfo {
        kadoka_runtime_info base;
        uint64_t extension;
    } extended{{}, UINT64_C(0x12345678)};
    extended.base.struct_size = sizeof(extended);
    assert(kadoka_runtime_query(&extended.base) == KADOKA_RUNTIME_STATUS_OK);
    assert(extended.extension == UINT64_C(0x12345678));

    kadoka_safety_action action{};
    action.struct_size = sizeof(action);
    action.kind = KADOKA_SAFETY_ACTION_CLICK;
    action.x = 20;
    action.y = 30;
    action.viewport_width = 640;
    action.viewport_height = 480;
    action.max_hold_seconds = 1.0;

    kadoka_safety_result result{};
    result.struct_size = sizeof(result);
    assert(kadoka_safety_validate_action(&action, &result) == 0);
    assert(result.allowed == 1);
    assert(result.reason_code == KADOKA_SAFETY_REASON_ALLOWED);

    action.x = -1;
    assert(kadoka_safety_validate_action(&action, &result) == 0);
    assert(result.allowed == 0);
    assert(result.reason_code == KADOKA_SAFETY_REASON_INVALID_COORDINATE);

    action.kind = KADOKA_SAFETY_ACTION_KEY;
    action.x = 0;
    action.key_code = 0x09u;
    action.modifiers = KADOKA_SAFETY_MOD_ALT;
    assert(kadoka_safety_validate_action(&action, &result) == 0);
    assert(result.allowed == 0);
    assert(result.reason_code == KADOKA_SAFETY_REASON_DENIED_KEY);

    action.key_code = static_cast<uint32_t>('A');
    action.modifiers = 0u;
    action.hold_seconds = 2.0;
    assert(kadoka_safety_validate_action(&action, &result) == 0);
    assert(result.allowed == 0);
    assert(result.reason_code == KADOKA_SAFETY_REASON_HOLD_TIMEOUT);

    action.hold_seconds = 0.2;
    assert(kadoka_safety_validate_action(&action, &result) == 0);
    assert(result.allowed == 1);
    return 0;
}
