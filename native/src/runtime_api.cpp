#include "kadoka/runtime_api.h"

namespace {

void set_result(kadoka_safety_result* result, int32_t allowed, uint32_t reason) {
    result->allowed = allowed;
    result->reason_code = reason;
}

bool denied_key(const kadoka_safety_action* action) {
    constexpr uint32_t key_tab = 0x09u;
    constexpr uint32_t key_delete = 0x2Eu;
    constexpr uint32_t key_f4 = 0x73u;
    constexpr uint32_t key_f12 = 0x7Bu;
    constexpr uint32_t key_lwin = 0x5Bu;
    constexpr uint32_t key_rwin = 0x5Cu;

    if (action->key_code == key_f12 || action->key_code == key_lwin || action->key_code == key_rwin) {
        return true;
    }
    if ((action->modifiers & KADOKA_SAFETY_MOD_WIN) != 0u) {
        return true;
    }
    if ((action->modifiers & KADOKA_SAFETY_MOD_ALT) != 0u &&
        (action->key_code == key_tab || action->key_code == key_f4)) {
        return true;
    }
    return (action->modifiers & KADOKA_SAFETY_MOD_CTRL) != 0u &&
           (action->modifiers & KADOKA_SAFETY_MOD_ALT) != 0u &&
           action->key_code == key_delete;
}

}  // namespace

uint32_t kadoka_runtime_abi_version(void) {
    return KADOKA_RUNTIME_ABI_VERSION;
}

int32_t kadoka_runtime_query(kadoka_runtime_info* out_info) {
    if (out_info == nullptr || out_info->struct_size < sizeof(kadoka_runtime_info)) {
        return -1;
    }

    out_info->abi_version = KADOKA_RUNTIME_ABI_VERSION;
    out_info->capabilities = KADOKA_CAP_SAFETY;
    return 0;
}

int32_t kadoka_safety_validate_action(
    const kadoka_safety_action* action,
    kadoka_safety_result* out_result
) {
    if (action == nullptr || out_result == nullptr ||
        action->struct_size < sizeof(kadoka_safety_action) ||
        out_result->struct_size < sizeof(kadoka_safety_result)) {
        return -1;
    }

    set_result(out_result, 0, KADOKA_SAFETY_REASON_INVALID_KIND);
    if (action->kind < KADOKA_SAFETY_ACTION_CLICK || action->kind > KADOKA_SAFETY_ACTION_WAIT) {
        return 0;
    }
    if (action->viewport_width <= 0 || action->viewport_height <= 0) {
        set_result(out_result, 0, KADOKA_SAFETY_REASON_INVALID_VIEWPORT);
        return 0;
    }
    if (action->hold_seconds < 0.0 || action->max_hold_seconds <= 0.0 ||
        action->hold_seconds > action->max_hold_seconds) {
        set_result(out_result, 0, KADOKA_SAFETY_REASON_HOLD_TIMEOUT);
        return 0;
    }
    if (action->kind != KADOKA_SAFETY_ACTION_KEY && action->hold_seconds > 0.0) {
        set_result(out_result, 0, KADOKA_SAFETY_REASON_HOLD_TIMEOUT);
        return 0;
    }
    if (action->kind == KADOKA_SAFETY_ACTION_CLICK || action->kind == KADOKA_SAFETY_ACTION_DOUBLE_CLICK) {
        if (action->x < 0 || action->y < 0 ||
            action->x >= action->viewport_width || action->y >= action->viewport_height) {
            set_result(out_result, 0, KADOKA_SAFETY_REASON_INVALID_COORDINATE);
            return 0;
        }
    }
    if (action->kind == KADOKA_SAFETY_ACTION_KEY) {
        if (action->key_code == 0u) {
            set_result(out_result, 0, KADOKA_SAFETY_REASON_INVALID_KEY);
            return 0;
        }
        if (denied_key(action)) {
            set_result(out_result, 0, KADOKA_SAFETY_REASON_DENIED_KEY);
            return 0;
        }
    }

    set_result(out_result, 1, KADOKA_SAFETY_REASON_ALLOWED);
    return 0;
}
