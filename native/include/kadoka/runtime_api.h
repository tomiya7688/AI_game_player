#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
  #if defined(KADOKA_RUNTIME_BUILD)
    #define KADOKA_RUNTIME_API __declspec(dllexport)
  #else
    #define KADOKA_RUNTIME_API __declspec(dllimport)
  #endif
#else
  #define KADOKA_RUNTIME_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define KADOKA_RUNTIME_ABI_VERSION 1u
#define KADOKA_CAP_CAPTURE (UINT64_C(1) << 0)
#define KADOKA_CAP_INPUT (UINT64_C(1) << 1)
#define KADOKA_CAP_SAFETY (UINT64_C(1) << 2)
#define KADOKA_CAP_FAST_CV (UINT64_C(1) << 3)

#define KADOKA_SAFETY_ACTION_CLICK 1u
#define KADOKA_SAFETY_ACTION_DOUBLE_CLICK 2u
#define KADOKA_SAFETY_ACTION_KEY 3u
#define KADOKA_SAFETY_ACTION_WAIT 4u

#define KADOKA_SAFETY_MOD_CTRL (1u << 0)
#define KADOKA_SAFETY_MOD_ALT (1u << 1)
#define KADOKA_SAFETY_MOD_SHIFT (1u << 2)
#define KADOKA_SAFETY_MOD_WIN (1u << 3)

#define KADOKA_SAFETY_REASON_ALLOWED 0u
#define KADOKA_SAFETY_REASON_INVALID_KIND 1u
#define KADOKA_SAFETY_REASON_INVALID_VIEWPORT 2u
#define KADOKA_SAFETY_REASON_INVALID_COORDINATE 3u
#define KADOKA_SAFETY_REASON_HOLD_TIMEOUT 4u
#define KADOKA_SAFETY_REASON_DENIED_KEY 5u
#define KADOKA_SAFETY_REASON_INVALID_KEY 6u

typedef struct kadoka_runtime_info {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t capabilities;
} kadoka_runtime_info;

typedef struct kadoka_safety_action {
    uint32_t struct_size;
    uint32_t kind;
    int32_t x;
    int32_t y;
    int32_t viewport_width;
    int32_t viewport_height;
    double hold_seconds;
    double max_hold_seconds;
    uint32_t key_code;
    uint32_t modifiers;
} kadoka_safety_action;

typedef struct kadoka_safety_result {
    uint32_t struct_size;
    int32_t allowed;
    uint32_t reason_code;
} kadoka_safety_result;

KADOKA_RUNTIME_API uint32_t kadoka_runtime_abi_version(void);
KADOKA_RUNTIME_API int32_t kadoka_runtime_query(kadoka_runtime_info* out_info);
KADOKA_RUNTIME_API int32_t kadoka_safety_validate_action(
    const kadoka_safety_action* action,
    kadoka_safety_result* out_result
);

#ifdef __cplusplus
}
#endif
