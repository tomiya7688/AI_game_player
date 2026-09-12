#include "kadoka/runtime_api.h"

uint32_t kadoka_runtime_abi_version(void) {
    return KADOKA_RUNTIME_ABI_VERSION;
}

int32_t kadoka_runtime_query(kadoka_runtime_info* out_info) {
    if (out_info == nullptr || out_info->struct_size < sizeof(kadoka_runtime_info)) {
        return -1;
    }

    out_info->abi_version = KADOKA_RUNTIME_ABI_VERSION;
    out_info->capabilities = 0;
    return 0;
}
