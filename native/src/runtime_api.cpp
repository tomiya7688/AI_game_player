#include "kadoka/runtime_api.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <new>
#include <stdexcept>
#include <vector>

struct kadoka_runtime_handle {
    uint32_t abi_version;
};

namespace {

constexpr uint32_t kBgraBytesPerPixel = 4u;
constexpr uint32_t kRgbChannelCount = 3u;
constexpr uint32_t kRedLumaWeight = 299u;
constexpr uint32_t kGreenLumaWeight = 587u;
constexpr uint32_t kBlueLumaWeight = 114u;
constexpr uint32_t kLumaWeightTotal = 1000u;
constexpr uint32_t kMaxChannelValue = std::numeric_limits<uint8_t>::max();
constexpr uint32_t kDHashRows = 8u;
constexpr uint32_t kDHashComparisonsPerRow = 8u;
constexpr uint32_t kDHashSamplesPerRow = kDHashComparisonsPerRow + 1u;
constexpr uint32_t kMinimumBrightRegionExtent = 3u;
constexpr uint8_t kMaskNotBright = 0u;
constexpr uint8_t kMaskBright = 1u;
constexpr uint8_t kMaskVisited = 2u;

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

uint32_t round_ratio_to_even(uint64_t numerator, uint64_t denominator) {
    const uint64_t quotient = numerator / denominator;
    const uint64_t remainder = numerator % denominator;
    const uint64_t doubled_remainder = remainder * 2u;
    const bool round_up = doubled_remainder > denominator ||
        (doubled_remainder == denominator && (quotient & 1u) != 0u);
    return static_cast<uint32_t>(quotient + static_cast<uint64_t>(round_up));
}

uint8_t pixel_brightness(const uint8_t* pixel) {
    const uint32_t blue = pixel[0];
    const uint32_t green = pixel[1];
    const uint32_t red = pixel[2];
    return static_cast<uint8_t>((red * kRedLumaWeight + green * kGreenLumaWeight +
        blue * kBlueLumaWeight) / kLumaWeightTotal);
}

}  // namespace

uint32_t kadoka_runtime_abi_version(void) {
    return KADOKA_RUNTIME_ABI_VERSION;
}

int32_t kadoka_runtime_init(
    const kadoka_runtime_options* options,
    kadoka_runtime_handle** out_runtime
) {
    if (out_runtime == nullptr || *out_runtime != nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    if (options == nullptr || options->struct_size < sizeof(kadoka_runtime_options)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    if (options->requested_abi_version != KADOKA_RUNTIME_ABI_VERSION) {
        return KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI;
    }

    auto* runtime = new (std::nothrow) kadoka_runtime_handle{KADOKA_RUNTIME_ABI_VERSION};
    if (runtime == nullptr) {
        return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
    }
    *out_runtime = runtime;
    return KADOKA_RUNTIME_STATUS_OK;
}

int32_t kadoka_runtime_shutdown(kadoka_runtime_handle** runtime) {
    if (runtime == nullptr || *runtime == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    delete *runtime;
    *runtime = nullptr;
    return KADOKA_RUNTIME_STATUS_OK;
}

int32_t kadoka_runtime_query(kadoka_runtime_info* out_info) {
    if (out_info == nullptr || out_info->struct_size < sizeof(kadoka_runtime_info)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }

    out_info->abi_version = KADOKA_RUNTIME_ABI_VERSION;
    out_info->capabilities = KADOKA_CAP_SAFETY | KADOKA_CAP_FAST_CV;
    return KADOKA_RUNTIME_STATUS_OK;
}

int32_t kadoka_safety_validate_action(
    const kadoka_safety_action* action,
    kadoka_safety_result* out_result
) {
    if (action == nullptr || out_result == nullptr ||
        action->struct_size < sizeof(kadoka_safety_action) ||
        out_result->struct_size < sizeof(kadoka_safety_result)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
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
    return KADOKA_RUNTIME_STATUS_OK;
}

int32_t kadoka_runtime_process_batch(
    const kadoka_runtime_handle* runtime,
    const kadoka_runtime_batch_request* request,
    kadoka_runtime_batch_result* out_result
) {
    if (runtime == nullptr || request == nullptr || out_result == nullptr ||
        request->struct_size < sizeof(kadoka_runtime_batch_request) ||
        out_result->struct_size < sizeof(kadoka_runtime_batch_result)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    if (request->abi_version != runtime->abi_version) {
        return KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI;
    }
    if (request->reserved != 0u || request->kind < KADOKA_RUNTIME_BATCH_FRAME ||
        request->kind > KADOKA_RUNTIME_BATCH_CONTROL_LEASE) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }

    out_result->abi_version = runtime->abi_version;
    out_result->batch_id = request->batch_id;
    out_result->status = KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
    out_result->reserved = 0u;
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

int32_t kadoka_runtime_preprocess_frame(
    const kadoka_runtime_handle* runtime,
    const kadoka_frame_preprocess_input* input,
    kadoka_frame_preprocess_result* out_result
) {
    if (runtime == nullptr || input == nullptr || out_result == nullptr ||
        input->struct_size < sizeof(kadoka_frame_preprocess_input) ||
        out_result->struct_size < sizeof(kadoka_frame_preprocess_result)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    if (input->abi_version != runtime->abi_version) {
        return KADOKA_RUNTIME_STATUS_UNSUPPORTED_ABI;
    }
    if (input->reserved != 0u || input->width == 0u || input->height == 0u ||
        input->width > static_cast<uint32_t>(std::numeric_limits<int32_t>::max()) ||
        input->height > static_cast<uint32_t>(std::numeric_limits<int32_t>::max()) ||
        input->brightness_threshold > kMaxChannelValue || input->min_region_pixels == 0u ||
        input->bgra == nullptr || (out_result->regions == nullptr && out_result->region_capacity != 0u)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }

    const uint64_t minimum_stride = static_cast<uint64_t>(input->width) * kBgraBytesPerPixel;
    const uint64_t required_bgra_size = static_cast<uint64_t>(input->stride_bytes) * input->height;
    const uint64_t frame_pixel_count = static_cast<uint64_t>(input->width) * input->height;
    if (input->stride_bytes < minimum_stride || input->bgra_size < required_bgra_size ||
        frame_pixel_count > std::numeric_limits<uint32_t>::max()) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }

    const size_t frame_pixel_count_size = static_cast<size_t>(frame_pixel_count);
    const auto processing_start = std::chrono::steady_clock::now();
    try {
        std::vector<uint8_t> bright_mask(frame_pixel_count_size, kMaskNotBright);
        uint64_t red_channel_sum = 0u;
        uint64_t green_channel_sum = 0u;
        uint64_t blue_channel_sum = 0u;
        for (uint32_t y = 0u; y < input->height; ++y) {
            const uint8_t* row = input->bgra + static_cast<size_t>(y) * input->stride_bytes;
            for (uint32_t x = 0u; x < input->width; ++x) {
                const uint8_t* pixel = row + static_cast<size_t>(x) * kBgraBytesPerPixel;
                blue_channel_sum += pixel[0];
                green_channel_sum += pixel[1];
                red_channel_sum += pixel[2];
                const size_t pixel_index = static_cast<size_t>(y) * input->width + x;
                if (pixel_brightness(pixel) >= input->brightness_threshold) {
                    bright_mask[pixel_index] = kMaskBright;
                }
            }
        }

        uint64_t frame_perceptual_hash = 0u;
        uint8_t brightness_samples[kDHashRows][kDHashSamplesPerRow]{};
        for (uint32_t row = 0u; row < kDHashRows; ++row) {
            const uint32_t y = static_cast<uint32_t>(std::min<uint64_t>(
                input->height - 1u, static_cast<uint64_t>(row) * input->height / kDHashRows));
            for (uint32_t column = 0u; column < kDHashSamplesPerRow; ++column) {
                const uint32_t x = static_cast<uint32_t>(std::min<uint64_t>(
                    input->width - 1u,
                    static_cast<uint64_t>(column) * input->width / kDHashSamplesPerRow));
                const uint8_t* pixel = input->bgra + static_cast<size_t>(y) * input->stride_bytes +
                    static_cast<size_t>(x) * kBgraBytesPerPixel;
                brightness_samples[row][column] = pixel_brightness(pixel);
            }
            for (uint32_t column = 0u; column < kDHashComparisonsPerRow; ++column) {
                frame_perceptual_hash = (frame_perceptual_hash << 1u) |
                    static_cast<uint64_t>(
                        brightness_samples[row][column] > brightness_samples[row][column + 1u]);
            }
        }

        std::vector<kadoka_frame_region> detected_regions;
        std::vector<uint32_t> pending_pixel_indices;
        for (uint32_t start_pixel_index = 0u;
             start_pixel_index < static_cast<uint32_t>(frame_pixel_count);
             ++start_pixel_index) {
            if (bright_mask[start_pixel_index] != kMaskBright) {
                continue;
            }
            pending_pixel_indices.clear();
            pending_pixel_indices.push_back(start_pixel_index);
            bright_mask[start_pixel_index] = kMaskVisited;
            uint32_t min_x = start_pixel_index % input->width;
            uint32_t max_x = min_x;
            uint32_t min_y = start_pixel_index / input->width;
            uint32_t max_y = min_y;
            uint64_t component_pixel_count = 0u;
            for (size_t queue_position = 0u;
                 queue_position < pending_pixel_indices.size();
                 ++queue_position) {
                const uint32_t pixel_index = pending_pixel_indices[queue_position];
                const uint32_t x = pixel_index % input->width;
                const uint32_t y = pixel_index / input->width;
                ++component_pixel_count;
                min_x = std::min(min_x, x);
                max_x = std::max(max_x, x);
                min_y = std::min(min_y, y);
                max_y = std::max(max_y, y);

                const auto visit_neighbor = [&](uint32_t neighbor_pixel_index) {
                    if (bright_mask[neighbor_pixel_index] == kMaskBright) {
                        bright_mask[neighbor_pixel_index] = kMaskVisited;
                        pending_pixel_indices.push_back(neighbor_pixel_index);
                    }
                };
                if (x > 0u) {
                    visit_neighbor(pixel_index - 1u);
                }
                if (x + 1u < input->width) {
                    visit_neighbor(pixel_index + 1u);
                }
                if (y > 0u) {
                    visit_neighbor(pixel_index - input->width);
                }
                if (y + 1u < input->height) {
                    visit_neighbor(pixel_index + input->width);
                }
            }

            const uint32_t region_width = max_x - min_x + 1u;
            const uint32_t region_height = max_y - min_y + 1u;
            if (component_pixel_count >= input->min_region_pixels &&
                region_width >= kMinimumBrightRegionExtent &&
                region_height >= kMinimumBrightRegionExtent) {
                detected_regions.push_back({
                    static_cast<int32_t>(min_x),
                    static_cast<int32_t>(min_y),
                    static_cast<int32_t>(region_width),
                    static_cast<int32_t>(region_height),
                    0u,
                    component_pixel_count,
                });
            }
        }

        const uint32_t region_count = static_cast<uint32_t>(detected_regions.size());
        out_result->abi_version = runtime->abi_version;
        out_result->mean_red = round_ratio_to_even(red_channel_sum, frame_pixel_count);
        out_result->mean_green = round_ratio_to_even(green_channel_sum, frame_pixel_count);
        out_result->mean_blue = round_ratio_to_even(blue_channel_sum, frame_pixel_count);
        out_result->mean_brightness = round_ratio_to_even(
            red_channel_sum + green_channel_sum + blue_channel_sum,
            kRgbChannelCount * frame_pixel_count);
        out_result->perceptual_hash = frame_perceptual_hash;
        out_result->region_count = region_count;
        out_result->input_frame_bytes_processed = frame_pixel_count * kBgraBytesPerPixel;
        out_result->output_bytes_written = 0u;
        out_result->input_copy_count = 0u;
        out_result->input_copy_bytes = 0u;
        out_result->processing_ns = 0u;
        out_result->reserved = 0u;
        if (region_count > out_result->region_capacity) {
            const auto processing_end = std::chrono::steady_clock::now();
            out_result->processing_ns = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    processing_end - processing_start).count());
            return KADOKA_RUNTIME_STATUS_BUFFER_TOO_SMALL;
        }
        for (uint32_t region_index = 0u; region_index < region_count; ++region_index) {
            out_result->regions[region_index] = detected_regions[region_index];
        }
        out_result->output_bytes_written = static_cast<uint64_t>(region_count) * sizeof(kadoka_frame_region);
        const auto processing_end = std::chrono::steady_clock::now();
        out_result->processing_ns = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                processing_end - processing_start).count());
        return KADOKA_RUNTIME_STATUS_OK;
    } catch (const std::bad_alloc&) {
        return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
    } catch (const std::length_error&) {
        return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
    }
}
