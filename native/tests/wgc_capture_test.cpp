#include "kadoka/runtime_api.h"

#define NOMINMAX
#include <windows.h>

#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <vector>

int main() {
    kadoka_runtime_options options{};
    options.struct_size = sizeof(options);
    options.requested_abi_version = KADOKA_RUNTIME_ABI_VERSION;
    kadoka_runtime_handle* runtime = nullptr;
    if (kadoka_runtime_init(&options, &runtime) != KADOKA_RUNTIME_STATUS_OK || runtime == nullptr) {
        return 1;
    }

    kadoka_runtime_info runtime_info{};
    runtime_info.struct_size = sizeof(runtime_info);
    if (kadoka_runtime_query(&runtime_info) != KADOKA_RUNTIME_STATUS_OK) {
        kadoka_runtime_shutdown(&runtime);
        return 2;
    }
    if ((runtime_info.capabilities & KADOKA_CAP_CAPTURE) == 0u) {
        kadoka_runtime_shutdown(&runtime);
        return 77;
    }
    std::fprintf(stderr, "WGC stage: runtime support query passed\n");

    HWND target_window = CreateWindowExW(
        0u,
        L"STATIC",
        L"Kadoka WGC capture contract test",
        WS_OVERLAPPEDWINDOW,
        20,
        20,
        180,
        120,
        nullptr,
        nullptr,
        GetModuleHandleW(nullptr),
        nullptr
    );
    if (target_window == nullptr) {
        kadoka_runtime_shutdown(&runtime);
        return 3;
    }
    ShowWindow(target_window, SW_SHOWNOACTIVATE);
    UpdateWindow(target_window);
    std::fprintf(stderr, "WGC stage: test window created\n");

    kadoka_wgc_session* session = nullptr;
    if (kadoka_wgc_open_window(
            runtime,
            static_cast<uint64_t>(reinterpret_cast<uintptr_t>(target_window)),
            &session
        ) != KADOKA_RUNTIME_STATUS_OK || session == nullptr) {
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 4;
    }
    std::fprintf(stderr, "WGC stage: capture session opened\n");

    kadoka_wgc_frame* first_frame = nullptr;
    if (kadoka_wgc_next_frame(session, 5000u, &first_frame) != KADOKA_RUNTIME_STATUS_OK || first_frame == nullptr) {
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 5;
    }
    std::fprintf(stderr, "WGC stage: first frame arrived\n");
    kadoka_wgc_frame_info first_info{};
    first_info.struct_size = sizeof(first_info);
    if (kadoka_wgc_frame_get_info(first_frame, &first_info) != KADOKA_RUNTIME_STATUS_OK ||
        first_info.width == 0u || first_info.height == 0u || first_info.stride_bytes != first_info.width * 4u ||
        first_info.frame_id == 0u || first_info.timestamp_ns == 0u || first_info.native_surface_handle == 0u) {
        kadoka_wgc_frame_release(&first_frame);
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 6;
    }
    std::fprintf(stderr, "WGC stage: first size=%ux%u id=%llu\n",
        first_info.width, first_info.height, static_cast<unsigned long long>(first_info.frame_id));
    const uint64_t first_frame_size = static_cast<uint64_t>(first_info.stride_bytes) * first_info.height;
    std::vector<uint8_t> first_pixels(static_cast<size_t>(first_frame_size));
    if (kadoka_wgc_frame_copy_bgra(first_frame, first_pixels.data(), first_pixels.size()) != KADOKA_RUNTIME_STATUS_OK) {
        kadoka_wgc_frame_release(&first_frame);
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 7;
    }
    if (kadoka_wgc_frame_release(&first_frame) != KADOKA_RUNTIME_STATUS_OK || first_frame != nullptr) {
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 8;
    }
    std::fprintf(stderr, "WGC stage: first frame copied and released\n");

    if (!SetWindowPos(target_window, nullptr, 20, 20, 260, 160, SWP_NOACTIVATE | SWP_NOZORDER)) {
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 9;
    }
    if (!SetWindowTextW(target_window, L"Kadoka WGC resized capture contract test") ||
        !InvalidateRect(target_window, nullptr, TRUE)) {
        kadoka_wgc_close(&session);
        DestroyWindow(target_window);
        kadoka_runtime_shutdown(&runtime);
        return 12;
    }
    UpdateWindow(target_window);
    bool resize_succeeded = false;
    uint64_t last_frame_id = first_info.frame_id;
    for (unsigned int attempt = 0u; attempt < 5u && !resize_succeeded; ++attempt) {
        kadoka_wgc_frame* resized_frame = nullptr;
        const int32_t next_frame_status = kadoka_wgc_next_frame(session, 1000u, &resized_frame);
        std::fprintf(stderr, "WGC stage: resize capture status=%d lease=%p\n", next_frame_status, resized_frame);
        if (next_frame_status == KADOKA_RUNTIME_STATUS_TIMEOUT) {
            if (!InvalidateRect(target_window, nullptr, TRUE) || !UpdateWindow(target_window)) {
                break;
            }
            std::fprintf(stderr, "WGC stage: resized test window redrawn after frame timeout\\n");
            continue;
        }
        if (next_frame_status != KADOKA_RUNTIME_STATUS_OK || resized_frame == nullptr) {
            break;
        }
        kadoka_wgc_frame_info resized_info{};
        resized_info.struct_size = sizeof(resized_info);
        const bool info_available = kadoka_wgc_frame_get_info(resized_frame, &resized_info) == KADOKA_RUNTIME_STATUS_OK;
        if (info_available) {
            std::fprintf(stderr, "WGC stage: resized candidate size=%ux%u id=%llu\n",
                resized_info.width, resized_info.height, static_cast<unsigned long long>(resized_info.frame_id));
        }
        const bool frame_is_new = info_available && resized_info.frame_id > last_frame_id;
        if (frame_is_new) {
            last_frame_id = resized_info.frame_id;
        }
        resize_succeeded = frame_is_new && resized_info.width > 0u && resized_info.height > 0u &&
            (resized_info.width != first_info.width || resized_info.height != first_info.height);
        if (kadoka_wgc_frame_release(&resized_frame) != KADOKA_RUNTIME_STATUS_OK || resized_frame != nullptr) {
            resize_succeeded = false;
            break;
        }
    }
    kadoka_wgc_close(&session);
    std::fprintf(stderr, "WGC stage: capture session closed\n");
    const bool window_destroyed = DestroyWindow(target_window) != 0;
    const bool runtime_closed = kadoka_runtime_shutdown(&runtime) == KADOKA_RUNTIME_STATUS_OK && runtime == nullptr;
    std::fprintf(stderr, "WGC stage: resize=%d window_closed=%d runtime_closed=%d\n",
        resize_succeeded ? 1 : 0, window_destroyed ? 1 : 0, runtime_closed ? 1 : 0);
    assert(resize_succeeded);
    assert(window_destroyed);
    assert(runtime_closed);
    return resize_succeeded && window_destroyed && runtime_closed ? 0 : 11;
}
