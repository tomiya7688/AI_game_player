#include "kadoka/runtime_api.h"

int main(void) {
    kadoka_runtime_options options = {sizeof(kadoka_runtime_options), KADOKA_RUNTIME_ABI_VERSION};
    kadoka_runtime_handle* runtime = 0;
    kadoka_runtime_info info = {sizeof(kadoka_runtime_info), 0u, 0u};
    kadoka_runtime_batch_request request = {
        sizeof(kadoka_runtime_batch_request), KADOKA_RUNTIME_ABI_VERSION,
        42u, KADOKA_RUNTIME_BATCH_INPUT, 0u
    };
    kadoka_runtime_batch_result result = {sizeof(kadoka_runtime_batch_result), 0u, 0u, 0, 0u};

    if (kadoka_runtime_abi_version() != KADOKA_RUNTIME_ABI_VERSION) {
        return 1;
    }
    if (kadoka_runtime_init(&options, &runtime) != KADOKA_RUNTIME_STATUS_OK || runtime == 0) {
        return 2;
    }
    if (kadoka_runtime_query(&info) != KADOKA_RUNTIME_STATUS_OK || info.abi_version != KADOKA_RUNTIME_ABI_VERSION) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 3;
    }
    if ((info.capabilities & KADOKA_CAP_SAFETY) == 0u) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 4;
    }
    if (kadoka_runtime_process_batch(runtime, &request, &result) != KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED ||
        result.status != KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED || result.batch_id != request.batch_id) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 6;
    }
    if (kadoka_runtime_shutdown(&runtime) != KADOKA_RUNTIME_STATUS_OK || runtime != 0) {
        return 5;
    }
    return 0;
}
