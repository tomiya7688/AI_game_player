#include "kadoka/runtime_api.h"

#if defined(_WIN32)

#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <roapi.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/base.h>

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <new>

namespace {

using winrt::Windows::Graphics::Capture::Direct3D11CaptureFrame;
using winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool;
using winrt::Windows::Graphics::Capture::GraphicsCaptureItem;
using winrt::Windows::Graphics::Capture::GraphicsCaptureSession;
using winrt::Windows::Graphics::DirectX::DirectXPixelFormat;
using winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;

constexpr DirectXPixelFormat kCapturePixelFormat = DirectXPixelFormat::B8G8R8A8UIntNormalized;
constexpr int32_t kCaptureFramePoolBufferCount = 2;
constexpr uint64_t kNanosecondsPerWinRtTimeUnit = 100u;
constexpr uint32_t kBytesPerPixel = 4u;

// {
//   責務: [WinRtApartmentScope: C ABI呼び出しthreadでWinRT APIを使える状態にする]
//   処理: [COM apartmentが未初期化ならMTAで初期化し、既存STAはそのまま利用する]
// }
class WinRtApartmentScope {
public:
    // {
    //   責務: [WinRtApartmentScope: WinRT呼び出しに必要なCOM apartmentを初期化する]
    //   戻り値: []
    // }
    WinRtApartmentScope() noexcept {
        const HRESULT result = RoInitialize(RO_INIT_MULTITHREADED);
        if (result == RPC_E_CHANGED_MODE) {
            status_ = S_OK;
            return;
        }
        status_ = result;
        should_uninitialize_ = SUCCEEDED(result);
    }

    // {
    //   責務: [~WinRtApartmentScope: このscopeが初期化したCOM apartmentだけを終了する]
    //   戻り値: []
    // }
    ~WinRtApartmentScope() {
        if (should_uninitialize_) {
            RoUninitialize();
        }
    }

    // {
    //   責務: [status: WinRT apartmentの初期化結果を返す]
    //   戻り値: [HRESULT: WinRT API呼び出しを続けられる場合は成功status]
    // }
    HRESULT status() const noexcept {
        return status_;
    }

private:
    HRESULT status_ = E_FAIL;
    bool should_uninitialize_ = false;
};

// {
//   責務: [create_capture_device: WGC frame poolへ渡すhardware Direct3D 11 deviceを作る]
//   引数: [native_device: DXGI device生成にも使うID3D11Device出力先]
//   戻り値: [IDirect3DDevice: Windows Graphics Captureが使うWinRT device]
// }
IDirect3DDevice create_capture_device(winrt::com_ptr<ID3D11Device>& native_device) {
    winrt::com_ptr<ID3D11DeviceContext> device_context;
    winrt::check_hresult(D3D11CreateDevice(
        nullptr,
        D3D_DRIVER_TYPE_HARDWARE,
        nullptr,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        nullptr,
        0,
        D3D11_SDK_VERSION,
        native_device.put(),
        nullptr,
        device_context.put()
    ));

    winrt::com_ptr<IDXGIDevice> dxgi_device = native_device.as<IDXGIDevice>();
    winrt::com_ptr<IInspectable> inspectable_device;
    winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(
        dxgi_device.get(),
        inspectable_device.put()
    ));
    return inspectable_device.as<IDirect3DDevice>();
}

// {
//   責務: [create_capture_item: 指定HWNDに結び付いたGraphicsCaptureItemを作る]
//   引数: [window: capture対象として検証済みのWindows HWND]
//   戻り値: [GraphicsCaptureItem: WGC sessionが取得する単一window item]
// }
GraphicsCaptureItem create_capture_item(HWND window) {
    auto interop = winrt::get_activation_factory<GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
    GraphicsCaptureItem item{nullptr};
    winrt::check_hresult(interop->CreateForWindow(
        window,
        winrt::guid_of<GraphicsCaptureItem>(),
        winrt::put_abi(item)
    ));
    return item;
}

// {
//   責務: [query_wgc_support_once: WinRT factoryを初期化thread apartmentの終了後に再利用しないようOS対応判定を一度だけ行う]
//   戻り値: [bool: Windows Graphics Captureを現在のOSが提供する場合はtrue]
// }
bool query_wgc_support_once() noexcept {
    static std::once_flag support_query_once;
    static bool is_supported = false;
    try {
        std::call_once(support_query_once, [] {
            try {
                is_supported = GraphicsCaptureSession::IsSupported();
            } catch (...) {
                is_supported = false;
            }
        });
    } catch (...) {
        return false;
    }
    return is_supported;
}

}  // namespace

// {
//   責務: [kadoka_wgc_frame: 取得済みframeのmetadataと参照中Direct3D textureを保持する]
//   フィールド: [texture: leaseが解放されるまで保持するBGRA texture, width/height: frameの有効pixel範囲, frame_id/timestamp_ns: WGC到着順とQPC時刻]
// }
struct kadoka_wgc_frame {
    winrt::com_ptr<ID3D11Texture2D> texture;
    uint32_t width = 0;
    uint32_t height = 0;
    uint64_t frame_id = 0;
    uint64_t timestamp_ns = 0;
    std::mutex copy_mutex;
};

// {
//   責務: [WgcSessionState: 選択HWND用のWGC session、frame pool、到着通知を所有する]
//   フィールド: [window: capture対象HWND, capture_item/capture_device/frame_pool/capture_session: Windows Graphics Capture資源, frame_arrived_event: frame到着または終了を待機threadへ通知するWin32 event, arrival_generation: FrameArrived通知数]
// }
struct WgcSessionState : std::enable_shared_from_this<WgcSessionState> {
    HWND window = nullptr;
    GraphicsCaptureItem capture_item{nullptr};
    IDirect3DDevice capture_device{nullptr};
    winrt::com_ptr<ID3D11Device> native_device;
    Direct3D11CaptureFramePool frame_pool{nullptr};
    GraphicsCaptureSession capture_session{nullptr};
    winrt::event_token frame_arrived_token{};
    winrt::Windows::Graphics::SizeInt32 frame_pool_size{};
    std::mutex capture_mutex;
    std::mutex arrival_mutex;
    HANDLE frame_arrived_event = nullptr;
    uint64_t arrival_generation = 0;
    bool is_closed = false;

    // {
    //   責務: [~WgcSessionState: 部分初期化中を含むWGC native資源を破棄する]
    //   戻り値: []
    // }
    ~WgcSessionState() {
        close();
    }

    // {
    //   責務: [start: target HWNDを監視するFreeThreaded WGC frame poolとcapture sessionを起動する]
    //   引数: [target_window: capture対象のHWND]
    //   戻り値: [void: 成功時にsessionがframe arrivalを通知する]
    // }
    void start(HWND target_window) {
        window = target_window;
        frame_arrived_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (frame_arrived_event == nullptr) {
            const HRESULT event_error = HRESULT_FROM_WIN32(GetLastError());
            throw winrt::hresult_error(FAILED(event_error) ? event_error : E_FAIL);
        }
        capture_item = create_capture_item(window);
        frame_pool_size = capture_item.Size();
        if (frame_pool_size.Width <= 0 || frame_pool_size.Height <= 0) {
            throw winrt::hresult_error(E_INVALIDARG);
        }

        capture_device = create_capture_device(native_device);
        frame_pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            capture_device,
            kCapturePixelFormat,
            kCaptureFramePoolBufferCount,
            frame_pool_size
        );
        std::weak_ptr<WgcSessionState> weak_state = shared_from_this();
        frame_arrived_token = frame_pool.FrameArrived(
            [weak_state](const Direct3D11CaptureFramePool&, const winrt::Windows::Foundation::IInspectable&) {
                const auto state = weak_state.lock();
                if (!state) {
                    return;
                }
                {
                    std::lock_guard lock(state->arrival_mutex);
                    if (state->is_closed) {
                        return;
                    }
                    ++state->arrival_generation;
                    SetEvent(state->frame_arrived_event);
                }
            }
        );
        capture_session = frame_pool.CreateCaptureSession(capture_item);
        capture_session.StartCapture();
    }

    // {
    //   責務: [close: WGC event、capture session、frame poolを一度だけ終了する]
    //   戻り値: [void: 再呼び出し可能な終了処理]
    // }
    void close() noexcept {
        {
            std::lock_guard lock(arrival_mutex);
            if (is_closed) {
                return;
            }
            is_closed = true;
            if (frame_arrived_event != nullptr) {
                SetEvent(frame_arrived_event);
            }
        }
        std::lock_guard capture_lock(capture_mutex);
        try {
            if (frame_pool && frame_arrived_token.value != 0) {
                frame_pool.FrameArrived(frame_arrived_token);
            }
        } catch (...) {
            // Event revocation failure must not prevent session and pool shutdown.
        }
        try {
            if (capture_session) {
                capture_session.Close();
            }
        } catch (...) {
            // Native shutdown must continue releasing the frame pool.
        }
        try {
            if (frame_pool) {
                frame_pool.Close();
            }
        } catch (...) {
            // Releasing the projected references below still tears down local ownership.
        }
        capture_session = nullptr;
        frame_pool = nullptr;
        capture_item = nullptr;
        capture_device = nullptr;
        native_device = nullptr;
        {
            std::lock_guard lock(arrival_mutex);
            if (frame_arrived_event != nullptr) {
                CloseHandle(frame_arrived_event);
                frame_arrived_event = nullptr;
            }
        }
    }

    // {
    //   責務: [next_frame: 到着済みWGC frameのうち最新の1枚を期限内にleaseとして返す]
    //   引数: [timeout_ms: FrameArrived通知を待つ上限ミリ秒]
    //   戻り値: [unique_ptr<kadoka_wgc_frame>: callerがreleaseするtexture lease、timeout時はnull]
    // }
    std::unique_ptr<kadoka_wgc_frame> next_frame(uint32_t timeout_ms) {
        std::unique_lock capture_lock(capture_mutex, std::try_to_lock);
        if (!capture_lock.owns_lock()) {
            throw winrt::hresult_error(E_ILLEGAL_METHOD_CALL);
        }

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        uint64_t observed_generation = 0;
        while (std::chrono::steady_clock::now() < deadline) {
            DWORD wait_timeout_ms = 0u;
            {
                const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                    deadline - std::chrono::steady_clock::now()
                ).count();
                wait_timeout_ms = static_cast<DWORD>(std::clamp<int64_t>(remaining, 1, MAXDWORD - 1));
                std::lock_guard arrival_lock(arrival_mutex);
                if (is_closed || frame_arrived_event == nullptr) {
                    return nullptr;
                }
            }

            const DWORD wait_result = WaitForSingleObject(frame_arrived_event, wait_timeout_ms);
            if (wait_result == WAIT_TIMEOUT) {
                return nullptr;
            }
            if (wait_result != WAIT_OBJECT_0) {
                const HRESULT wait_error = HRESULT_FROM_WIN32(GetLastError());
                throw winrt::hresult_error(FAILED(wait_error) ? wait_error : E_FAIL);
            }
            {
                std::lock_guard arrival_lock(arrival_mutex);
                if (is_closed || frame_arrived_event == nullptr) {
                    return nullptr;
                }
                observed_generation = arrival_generation;
                if (!ResetEvent(frame_arrived_event)) {
                    const HRESULT reset_error = HRESULT_FROM_WIN32(GetLastError());
                    throw winrt::hresult_error(FAILED(reset_error) ? reset_error : E_FAIL);
                }
            }

            auto captured_frame = frame_pool.TryGetNextFrame();
            if (!captured_frame) {
                continue;
            }
            while (auto newer_frame = frame_pool.TryGetNextFrame()) {
                captured_frame.Close();
                captured_frame = std::move(newer_frame);
            }

            const auto content_size = captured_frame.ContentSize();
            if (content_size.Width <= 0 || content_size.Height <= 0 ||
                static_cast<uint64_t>(content_size.Width) * kBytesPerPixel >
                    std::numeric_limits<uint32_t>::max()) {
                captured_frame.Close();
                throw winrt::hresult_error(E_INVALIDARG);
            }

            auto surface = captured_frame.Surface();
            auto surface_access = surface.as<Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
            auto frame_lease = std::make_unique<kadoka_wgc_frame>();
            winrt::check_hresult(surface_access->GetInterface(
                __uuidof(ID3D11Texture2D),
                frame_lease->texture.put_void()
            ));
            D3D11_TEXTURE2D_DESC texture_description{};
            frame_lease->texture->GetDesc(&texture_description);
            if (texture_description.Format != DXGI_FORMAT_B8G8R8A8_UNORM ||
                texture_description.Width < static_cast<uint32_t>(content_size.Width) ||
                texture_description.Height < static_cast<uint32_t>(content_size.Height)) {
                captured_frame.Close();
                throw winrt::hresult_error(E_INVALIDARG);
            }

            frame_lease->width = static_cast<uint32_t>(content_size.Width);
            frame_lease->height = static_cast<uint32_t>(content_size.Height);
            frame_lease->frame_id = observed_generation;
            const int64_t capture_time_units = captured_frame.SystemRelativeTime().count();
            if (capture_time_units > 0 && static_cast<uint64_t>(capture_time_units) <=
                    std::numeric_limits<uint64_t>::max() / kNanosecondsPerWinRtTimeUnit) {
                frame_lease->timestamp_ns =
                    static_cast<uint64_t>(capture_time_units) * kNanosecondsPerWinRtTimeUnit;
            }
            captured_frame.Close();

            if (content_size.Width != frame_pool_size.Width || content_size.Height != frame_pool_size.Height) {
                frame_pool_size = content_size;
                frame_pool.Recreate(
                    capture_device,
                    kCapturePixelFormat,
                    kCaptureFramePoolBufferCount,
                    frame_pool_size
                );
            }
            return frame_lease;
        }
        return nullptr;
    }
};

// {
//   責務: [kadoka_wgc_session: C ABIを通じてWGC session状態を所有するopaque handle]
// }
struct kadoka_wgc_session {
    std::shared_ptr<WgcSessionState> state;
};

// {
//   責務: [copy_frame_to_bgra: BGRA textureをcaller-owned packed pixel bufferへ写す]
//   引数: [frame: 読み取り元のframe lease, output_bgra: row paddingなしBGRA出力, output_capacity: 出力buffer容量byte]
//   戻り値: [int32_t: copy成功、buffer不足、またはDirect3D failureのstatus]
// }
int32_t copy_frame_to_bgra(kadoka_wgc_frame& frame, uint8_t* output_bgra, uint64_t output_capacity) {
    const uint64_t row_bytes = static_cast<uint64_t>(frame.width) * kBytesPerPixel;
    const uint64_t required_bytes = row_bytes * frame.height;
    if (output_bgra == nullptr || output_capacity < required_bytes) {
        return KADOKA_RUNTIME_STATUS_BUFFER_TOO_SMALL;
    }

    std::lock_guard copy_lock(frame.copy_mutex);
    winrt::com_ptr<ID3D11Device> device;
    frame.texture->GetDevice(device.put());
    D3D11_TEXTURE2D_DESC staging_description{};
    staging_description.Width = frame.width;
    staging_description.Height = frame.height;
    staging_description.MipLevels = 1;
    staging_description.ArraySize = 1;
    staging_description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    staging_description.SampleDesc.Count = 1;
    staging_description.Usage = D3D11_USAGE_STAGING;
    staging_description.CPUAccessFlags = D3D11_CPU_ACCESS_READ;

    winrt::com_ptr<ID3D11Texture2D> staging_texture;
    if (FAILED(device->CreateTexture2D(&staging_description, nullptr, staging_texture.put()))) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    winrt::com_ptr<ID3D11DeviceContext> device_context;
    device->GetImmediateContext(device_context.put());
    const D3D11_BOX source_region{
        0u,
        0u,
        0u,
        frame.width,
        frame.height,
        1u,
    };
    device_context->CopySubresourceRegion(
        staging_texture.get(), 0u, 0u, 0u, 0u, frame.texture.get(), 0u, &source_region
    );

    D3D11_MAPPED_SUBRESOURCE mapped_pixels{};
    if (FAILED(device_context->Map(staging_texture.get(), 0u, D3D11_MAP_READ, 0u, &mapped_pixels))) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    for (uint32_t row = 0; row < frame.height; ++row) {
        const auto* source_row = static_cast<const uint8_t*>(mapped_pixels.pData) +
            static_cast<size_t>(row) * mapped_pixels.RowPitch;
        auto* destination_row = output_bgra + static_cast<size_t>(row) * static_cast<size_t>(row_bytes);
        std::memcpy(destination_row, source_row, static_cast<size_t>(row_bytes));
    }
    device_context->Unmap(staging_texture.get(), 0u);
    return KADOKA_RUNTIME_STATUS_OK;
}

// {
//   責務: [kadoka_wgc_is_supported: Windows Graphics Captureが現在のOSで利用可能か返す]
//   引数: [out_supported: WGC対応状態を受け取るuint32出力先]
//   戻り値: [int32_t: 問い合わせ処理のstatus code]
// }
int32_t kadoka_wgc_is_supported(uint32_t* out_supported) {
    if (out_supported == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    *out_supported = 0u;
    WinRtApartmentScope apartment;
    if (FAILED(apartment.status())) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    *out_supported = query_wgc_support_once() ? 1u : 0u;
    return KADOKA_RUNTIME_STATUS_OK;
}

// {
//   責務: [kadoka_wgc_open_window: 選択HWND専用のWGC sessionを作成してNative Runtimeが所有する]
//   引数: [runtime: 初期化済みNative Runtime handle, window_handle: capture対象HWND, out_session: 新しいsessionの出力先]
//   戻り値: [int32_t: session作成結果のstatus code]
// }
int32_t kadoka_wgc_open_window(
    const kadoka_runtime_handle* runtime,
    uint64_t window_handle,
    kadoka_wgc_session** out_session
) {
    if (runtime == nullptr || window_handle == 0u || out_session == nullptr || *out_session != nullptr ||
        window_handle > std::numeric_limits<uintptr_t>::max()) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    WinRtApartmentScope apartment;
    if (FAILED(apartment.status())) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    const HWND target_window = reinterpret_cast<HWND>(static_cast<uintptr_t>(window_handle));
    if (!IsWindow(target_window)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    try {
        if (!query_wgc_support_once()) {
            return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
        }
        auto session_state = std::make_shared<WgcSessionState>();
        session_state->start(target_window);
        auto* session = new (std::nothrow) kadoka_wgc_session{std::move(session_state)};
        if (session == nullptr) {
            return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
        }
        *out_session = session;
        return KADOKA_RUNTIME_STATUS_OK;
    } catch (const std::bad_alloc&) {
        return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
    } catch (...) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
}

// {
//   責務: [kadoka_wgc_next_frame: 到着をtimeout_msだけ待ち、最新textureをleaseとして返す]
//   引数: [session: 稼働中のWGC session, timeout_ms: frame待機上限ミリ秒, out_frame: frame lease出力先]
//   戻り値: [int32_t: frame取得結果のstatus code]
// }
int32_t kadoka_wgc_next_frame(
    kadoka_wgc_session* session,
    uint32_t timeout_ms,
    kadoka_wgc_frame** out_frame
) {
    if (session == nullptr || session->state == nullptr || out_frame == nullptr || *out_frame != nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    WinRtApartmentScope apartment;
    if (FAILED(apartment.status())) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    if (!IsWindow(session->state->window)) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    try {
        auto frame = session->state->next_frame(timeout_ms);
        if (!frame) {
            return KADOKA_RUNTIME_STATUS_TIMEOUT;
        }
        *out_frame = frame.release();
        return KADOKA_RUNTIME_STATUS_OK;
    } catch (const std::bad_alloc&) {
        return KADOKA_RUNTIME_STATUS_ALLOCATION_FAILED;
    } catch (...) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
}

// {
//   責務: [kadoka_wgc_frame_get_info: frame leaseの寸法、時刻、ID、opaque texture handleを返す]
//   引数: [frame: 取得済みframe lease, out_info: 呼び出し元が用意したsize付きmetadata構造体]
//   戻り値: [int32_t: metadata取得結果のstatus code]
// }
int32_t kadoka_wgc_frame_get_info(const kadoka_wgc_frame* frame, kadoka_wgc_frame_info* out_info) {
    if (frame == nullptr || out_info == nullptr || out_info->struct_size < sizeof(kadoka_wgc_frame_info)) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    out_info->abi_version = KADOKA_RUNTIME_ABI_VERSION;
    out_info->width = frame->width;
    out_info->height = frame->height;
    out_info->stride_bytes = frame->width * kBytesPerPixel;
    out_info->reserved = 0u;
    out_info->frame_id = frame->frame_id;
    out_info->timestamp_ns = frame->timestamp_ns;
    out_info->native_surface_handle = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(frame->texture.get()));
    return KADOKA_RUNTIME_STATUS_OK;
}

// {
//   責務: [kadoka_wgc_frame_copy_bgra: Direct3D textureをcaller-owned packed BGRA bufferへcopyする]
//   引数: [frame: 取得済みframe lease, output_bgra: pixel dataを書き込む出力buffer, output_capacity: 出力buffer容量byte]
//   戻り値: [int32_t: copy結果のstatus code]
// }
int32_t kadoka_wgc_frame_copy_bgra(kadoka_wgc_frame* frame, uint8_t* output_bgra, uint64_t output_capacity) {
    if (frame == nullptr || frame->texture == nullptr || frame->width == 0u || frame->height == 0u ||
        static_cast<uint64_t>(frame->width) * kBytesPerPixel >
            std::numeric_limits<uint64_t>::max() / frame->height) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    try {
        return copy_frame_to_bgra(*frame, output_bgra, output_capacity);
    } catch (...) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
}

// {
//   責務: [kadoka_wgc_frame_release: frame leaseとtexture referenceを解放しpointerをnullへ戻す]
//   引数: [frame: 解放するframe lease pointer]
//   戻り値: [int32_t: 解放処理のstatus code。null pointerまたはnull leaseではINVALID_ARGUMENT]
// }
int32_t kadoka_wgc_frame_release(kadoka_wgc_frame** frame) {
    if (frame == nullptr || *frame == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    delete *frame;
    *frame = nullptr;
    return KADOKA_RUNTIME_STATUS_OK;
}

// {
//   責務: [kadoka_wgc_close: WGC sessionのnative資源を解放しpointerをnullへ戻す]
//   引数: [session: close対象のcapture session pointer]
//   戻り値: [int32_t: session解放結果。null pointerまたはnull sessionではINVALID_ARGUMENT]
// }
int32_t kadoka_wgc_close(kadoka_wgc_session** session) {
    if (session == nullptr || *session == nullptr || (*session)->state == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    WinRtApartmentScope apartment;
    (*session)->state->close();
    delete *session;
    *session = nullptr;
    return SUCCEEDED(apartment.status()) ? KADOKA_RUNTIME_STATUS_OK : KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
}

#else

struct kadoka_wgc_session {};
struct kadoka_wgc_frame {};

// {
//   責務: [kadoka_wgc_is_supported: Windows以外のNative RuntimeでWGC非対応を返す]
//   引数: [out_supported: 非対応の0を受け取る出力先]
//   戻り値: [int32_t: 正常な問い合わせstatus code]
// }
int32_t kadoka_wgc_is_supported(uint32_t* out_supported) {
    if (out_supported == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    *out_supported = 0u;
    return KADOKA_RUNTIME_STATUS_OK;
}

// {
//   責務: [kadoka_wgc_open_window: Windows以外ではWGC sessionを作らず未実装statusを返す]
//   引数: [runtime: Native Runtime handle, window_handle: target window, out_session: session output]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_open_window(const kadoka_runtime_handle*, uint64_t, kadoka_wgc_session** out_session) {
    if (out_session == nullptr || *out_session != nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

// {
//   責務: [kadoka_wgc_next_frame: Windows以外ではframeを取得せず未実装statusを返す]
//   引数: [session: WGC session, timeout_ms: frame待機上限, out_frame: frame output]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_next_frame(kadoka_wgc_session*, uint32_t, kadoka_wgc_frame** out_frame) {
    if (out_frame == nullptr || *out_frame != nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

// {
//   責務: [kadoka_wgc_frame_get_info: Windows以外では存在しないframe metadataを返さない]
//   引数: [frame: WGC frame, out_info: frame metadata output]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_frame_get_info(const kadoka_wgc_frame*, kadoka_wgc_frame_info*) {
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

// {
//   責務: [kadoka_wgc_frame_copy_bgra: Windows以外ではWGC pixelをcopyせず未実装statusを返す]
//   引数: [frame: WGC frame, output_bgra: caller-owned pixel buffer, output_capacity: 出力buffer容量byte]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_frame_copy_bgra(kadoka_wgc_frame*, uint8_t*, uint64_t) {
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

// {
//   責務: [kadoka_wgc_frame_release: Windows以外では生成されないframe leaseを解放しない]
//   引数: [frame: WGC frame lease pointer]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_frame_release(kadoka_wgc_frame** frame) {
    if (frame == nullptr || *frame == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

// {
//   責務: [kadoka_wgc_close: Windows以外では生成されないWGC sessionを閉じない]
//   引数: [session: WGC session pointer]
//   戻り値: [int32_t: WGC非対応status code]
// }
int32_t kadoka_wgc_close(kadoka_wgc_session** session) {
    if (session == nullptr || *session == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    return KADOKA_RUNTIME_STATUS_NOT_IMPLEMENTED;
}

#endif
