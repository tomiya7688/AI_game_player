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
    const uint8_t pixel[4] = {0u, 0u, 255u, 0u};
    kadoka_frame_preprocess_input frame_input = {
        sizeof(kadoka_frame_preprocess_input), KADOKA_RUNTIME_ABI_VERSION,
        1u, 1u, 4u, 220u, 9u, 0u, sizeof(pixel), pixel
    };
    kadoka_frame_preprocess_result frame_result = {0};
    frame_result.struct_size = sizeof(frame_result);
    frame_result.abi_version = KADOKA_RUNTIME_ABI_VERSION;

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
    if ((info.capabilities & (KADOKA_CAP_SAFETY | KADOKA_CAP_FAST_CV)) !=
        (KADOKA_CAP_SAFETY | KADOKA_CAP_FAST_CV)) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 4;
    }
    if (kadoka_runtime_process_batch(runtime, &request, &result) != KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED ||
        result.status != KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED || result.batch_id != request.batch_id) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 6;
    }
    if (kadoka_runtime_preprocess_frame(runtime, &frame_input, &frame_result) != KADOKA_RUNTIME_STATUS_OK ||
        frame_result.mean_red != 255u || frame_result.mean_green != 0u ||
        frame_result.mean_blue != 0u || frame_result.mean_brightness != 85u ||
        frame_result.perceptual_hash != 0u || frame_result.region_count != 0u ||
        frame_result.input_bytes_read != sizeof(pixel) || frame_result.output_bytes_written != 0u ||
        frame_result.input_copy_count != 0u || frame_result.input_copy_bytes != 0u) {
        (void)kadoka_runtime_shutdown(&runtime);
        return 7;
    }
    if (kadoka_runtime_shutdown(&runtime) != KADOKA_RUNTIME_STATUS_OK || runtime != 0) {
        return 5;
    }
    return 0;
}
