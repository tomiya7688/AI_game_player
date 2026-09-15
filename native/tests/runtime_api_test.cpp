#include "kadoka/runtime_api.h"

#include <cassert>

int main() {
    kadoka_runtime_info info{};
    info.struct_size = sizeof(info);
    assert(kadoka_runtime_query(&info) == 0);
    assert(info.abi_version == KADOKA_RUNTIME_ABI_VERSION);
    assert((info.capabilities & KADOKA_CAP_SAFETY) != 0u);

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
