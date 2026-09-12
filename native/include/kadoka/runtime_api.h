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

typedef struct kadoka_runtime_info {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t capabilities;
} kadoka_runtime_info;

KADOKA_RUNTIME_API uint32_t kadoka_runtime_abi_version(void);
KADOKA_RUNTIME_API int32_t kadoka_runtime_query(kadoka_runtime_info* out_info);

#ifdef __cplusplus
}
#endif
