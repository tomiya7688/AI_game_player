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
#define KADOKA_RUNTIME_STATUS_OK 0
#define KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT -1
#define KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI -2
#define KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED -3
#define KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED -4
#define KADOKA_RUNTIME_STATUS_BUFFER_TOO_SMALL -5
#define KADOKA_RUNTIME_STATUS_TIMEOUT -6
#define KADOKA_RUNTIME_STATUS_CAPTURE_FAILED -7

#define KADOKA_RUNTIME_BATCH_FRAME 1u
#define KADOKA_RUNTIME_BATCH_OBSERVATION 2u
#define KADOKA_RUNTIME_BATCH_CANDIDATE_SET 3u
#define KADOKA_RUNTIME_BATCH_DECISION 4u
#define KADOKA_RUNTIME_BATCH_INPUT 5u
#define KADOKA_RUNTIME_BATCH_CONTROL_LEASE 6u
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

typedef struct kadoka_runtime_options {
    uint32_t struct_size;
    uint32_t requested_abi_version;
} kadoka_runtime_options;

typedef struct kadoka_runtime_handle kadoka_runtime_handle;

// {
//   責務: [kadoka_wgc_session/kadoka_wgc_frame: PythonへWinRT objectを渡さずcapture sessionとframe leaseを識別するopaque handle]
// }
typedef struct kadoka_wgc_session kadoka_wgc_session;
typedef struct kadoka_wgc_frame kadoka_wgc_frame;

// {
//   責務: [kadoka_wgc_frame_info: frame leaseのpacked BGRA寸法、WGC frame ID、QPC時刻、native textureを示す]
//   フィールド: [width/height/stride_bytes: copy APIの画素寸法, frame_id: session内で単調増加するarrival ID, timestamp_ns: SystemRelativeTimeのnanoseconds, native_surface_handle: frame releaseまでnative callerだけが使うopaque texture handle]
// }
typedef struct kadoka_wgc_frame_info {
    uint32_t struct_size;
    uint32_t abi_version;
    uint32_t width;
    uint32_t height;
    uint32_t stride_bytes;
    uint32_t reserved;
    uint64_t frame_id;
    uint64_t timestamp_ns;
    uint64_t native_surface_handle;
} kadoka_wgc_frame_info;

/* Metadata-only v1 placeholder. Payload/ownership belong to a later contract. */
typedef struct kadoka_runtime_batch_request {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t batch_id;
    uint32_t kind;
    uint32_t reserved;
} kadoka_runtime_batch_request;

typedef struct kadoka_runtime_batch_result {
    uint32_t struct_size;
    uint32_t abi_version;
    uint64_t batch_id;
    int32_t status;
    uint32_t reserved;
} kadoka_runtime_batch_result;

typedef struct kadoka_frame_preprocess_input {
    uint32_t struct_size;
    uint32_t abi_version;
    uint32_t width;
    uint32_t height;
    uint32_t stride_bytes;
    uint32_t brightness_threshold;
    uint32_t min_region_pixels;
    uint32_t reserved;
    uint64_t bgra_size;
    /* Borrowed only for the duration of kadoka_runtime_preprocess_frame. */
    const uint8_t* bgra;
} kadoka_frame_preprocess_input;

/* Caller-owned output. Pixel count records the connected bright component size. */
typedef struct kadoka_frame_region {
    int32_t x;
    int32_t y;
    int32_t width;
    int32_t height;
    uint32_t reserved;
    uint64_t pixel_count;
} kadoka_frame_region;

typedef struct kadoka_frame_preprocess_result {
    uint32_t struct_size;
    uint32_t abi_version;
    uint32_t mean_red;
    uint32_t mean_green;
    uint32_t mean_blue;
    uint32_t mean_brightness;
    uint64_t perceptual_hash;
    /* region_count is required capacity; on BUFFER_TOO_SMALL, retry with that capacity. */
    uint32_t region_count;
    uint32_t region_capacity;
    /* Caller-owned storage; no native allocation is returned across the ABI. */
    kadoka_frame_region* regions;
    /* Logical BGRA pixel payload processed per call; excludes row padding and struct headers. */
    uint64_t input_frame_bytes_processed;
    uint64_t output_bytes_written;
    /* The BGRA pointer is borrowed; these fields report copies of the source pixels. */
    uint64_t input_copy_count;
    uint64_t input_copy_bytes;
    /* Native processing duration; excludes Python/FFI scheduling and marshaling. */
    uint64_t processing_ns;
    uint32_t reserved;
} kadoka_frame_preprocess_result;

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
/* out_runtime must point to a null-initialized pointer. Failure retains it. */
KADOKA_RUNTIME_API int32_t kadoka_runtime_init(
    const kadoka_runtime_options* options,
    kadoka_runtime_handle** out_runtime
);
KADOKA_RUNTIME_API int32_t kadoka_runtime_shutdown(kadoka_runtime_handle** runtime);
KADOKA_RUNTIME_API int32_t kadoka_runtime_query(kadoka_runtime_info* out_info);
KADOKA_RUNTIME_API int32_t kadoka_runtime_process_batch(
    const kadoka_runtime_handle* runtime,
    const kadoka_runtime_batch_request* request,
    kadoka_runtime_batch_result* out_result
);
KADOKA_RUNTIME_API int32_t kadoka_runtime_preprocess_frame(
    const kadoka_runtime_handle* runtime,
    const kadoka_frame_preprocess_input* input,
    kadoka_frame_preprocess_result* out_result
);
KADOKA_RUNTIME_API int32_t kadoka_safety_validate_action(
    const kadoka_safety_action* action,
    kadoka_safety_result* out_result
);

// {
//   責務: [kadoka_wgc_is_supported: 現在のWindows環境でWindows Graphics Captureが使えるか調べる]
//   引数: [out_supported: 対応している場合に1、未対応の場合に0を受け取る出力先]
//   戻り値: [int32_t: 問い合わせ処理のstatus code]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_is_supported(uint32_t* out_supported);

// {
//   責務: [kadoka_wgc_open_window: 指定HWNDを取得するWindows Graphics Capture sessionを作る]
//   引数: [runtime: 初期化済みNative Runtime, window_handle: capture対象のWindows HWND, out_session: 新しいsessionを受け取るnullポインタ]
//   戻り値: [int32_t: session作成結果のstatus code]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_open_window(
    const kadoka_runtime_handle* runtime,
    uint64_t window_handle,
    kadoka_wgc_session** out_session
);

// {
//   責務: [kadoka_wgc_next_frame: 指定時間以内に届いた最新frameを所有権付きで返す]
//   引数: [session: open_windowが返したsession, timeout_ms: frame到着を待つ上限ミリ秒, out_frame: 新しいframe leaseを受け取るnullポインタ]
//   戻り値: [int32_t: frame取得結果。期限超過ではTIMEOUTを返す]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_next_frame(
    kadoka_wgc_session* session,
    uint32_t timeout_ms,
    kadoka_wgc_frame** out_frame
);

// {
//   責務: [kadoka_wgc_frame_get_info: frame寸法、識別子、時刻、native texture handleを返す]
//   引数: [frame: next_frameが返したframe lease, out_info: frame metadataを受け取るsize付き構造体]
//   戻り値: [int32_t: metadata取得結果のstatus code。native_surface_handleはkadoka_wgc_frame_releaseまで有効]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_frame_get_info(
    const kadoka_wgc_frame* frame,
    kadoka_wgc_frame_info* out_info
);

// {
//   責務: [kadoka_wgc_frame_copy_bgra: frame textureをcaller-owned BGRA bufferへcopyする]
//   引数: [frame: copy対象のframe lease, output_bgra: packed BGRAを書き込むcaller buffer, output_capacity: buffer容量byte]
//   戻り値: [int32_t: copy結果。容量不足ではBUFFER_TOO_SMALLを返しleaseを保持する]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_frame_copy_bgra(
    kadoka_wgc_frame* frame,
    uint8_t* output_bgra,
    uint64_t output_capacity
);

// {
//   責務: [kadoka_wgc_frame_release: frame leaseが保持するWinRT frameとnative textureを解放する]
//   引数: [frame: 解放後にnullへ戻すframe lease pointer]
//   戻り値: [int32_t: 解放結果のstatus code]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_frame_release(kadoka_wgc_frame** frame);

// {
//   責務: [kadoka_wgc_close: capture session、frame pool、Direct3D deviceを閉じる]
//   引数: [session: close後にnullへ戻すcapture session pointer]
//   戻り値: [int32_t: close結果のstatus code]
// }
KADOKA_RUNTIME_API int32_t kadoka_wgc_close(kadoka_wgc_session** session);

#ifdef __cplusplus
}
#endif
