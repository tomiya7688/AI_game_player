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
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <exception>
#include <functional>
#include <future>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <thread>
#include <utility>

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
//   責務: [kFramePoolPollInterval: FreeThreaded frame poolを次に確認するまでの上限待ち時間]
//   値: [5 milliseconds: frame到着の確認間隔]
// }
constexpr std::chrono::milliseconds kFramePoolPollInterval{5};

// {
//   責務: [run_capture_operation: WGC処理が失敗した段階とHRESULTをstderrへ記録して例外を再送出する]
//   引数: [operation_name: 診断ログへ記録するWGC処理名, operation: 実行するWGC処理]
//   戻り値: [呼び出したWGC処理の戻り値。HRESULT例外は診断後にそのまま再送出する]
// }
template <typename Operation>
decltype(auto) run_capture_operation(const char* operation_name, Operation&& operation) {
    try {
        return std::forward<Operation>(operation)();
    } catch (const winrt::hresult_error& error) {
        std::fprintf(
            stderr,
            "WGC operation '%s' failed with HRESULT 0x%08lX\n",
            operation_name,
            static_cast<unsigned long>(error.code().value)
        );
        throw;
    }
}

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
    // {
    //   理由: [WGC sessionごとに起動するMTA worker内でinterop factoryを取得してCreateForWindowを呼ぶ]
    // }
    winrt::hresult_error factory_error{E_FAIL};
    auto interop = winrt::try_get_activation_factory<GraphicsCaptureItem, IGraphicsCaptureItemInterop>(factory_error);
    if (!interop) {
        throw factory_error;
    }
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
//   責務: [kadoka_wgc_frame: copy完了まで取得frameをpoolへ返さずmetadataとDirect3D textureを保持する]
//   フィールド: [capture_frame: releaseまでpoolへ返さないWGC frame, texture: lease中にcopyするBGRA texture, width/height: frameの有効pixel範囲, frame_id: session内lease番号, timestamp_ns: WGC SystemRelativeTime, copy_mutex: 同じdevice contextを使うlease間の共有lock]
// }
struct kadoka_wgc_frame {
    Direct3D11CaptureFrame capture_frame{nullptr};
    winrt::com_ptr<ID3D11Texture2D> texture;
    uint32_t width = 0;
    uint32_t height = 0;
    uint64_t frame_id = 0;
    uint64_t timestamp_ns = 0;
    std::shared_ptr<std::mutex> copy_mutex;
};

// {
//   責務: [WgcSessionState: 選択HWND用のWGC sessionを専用MTA threadで所有し、capture requestを直列化する]
//   フィールド: [window: capture対象HWND, capture_item/capture_device/frame_pool/capture_session: 専用threadだけが使うWGC資源, native_context_mutex: frame lease間で共有するD3D context lock, worker_thread: MTA apartmentとWGC資源の寿命, pending_commands: capture threadへ渡す要求, next_frame_id: leaseへ割り当てる連番, close_requested: worker終了要求]
// }
struct WgcSessionState {
    HWND window = nullptr;
    GraphicsCaptureItem capture_item{nullptr};
    IDirect3DDevice capture_device{nullptr};
    winrt::com_ptr<ID3D11Device> native_device;
    std::shared_ptr<std::mutex> native_context_mutex = std::make_shared<std::mutex>();
    Direct3D11CaptureFramePool frame_pool{nullptr};
    GraphicsCaptureSession capture_session{nullptr};
    winrt::Windows::Graphics::SizeInt32 frame_pool_size{};
    std::thread worker_thread;
    std::mutex request_mutex;
    std::mutex command_mutex;
    std::condition_variable command_ready;
    std::deque<std::function<void()>> pending_commands;
    std::mutex close_mutex;
    uint64_t next_frame_id = 0;
    std::atomic<bool> close_requested{false};

    // {
    //   責務: [~WgcSessionState: 部分初期化中を含むWGC native資源を破棄する]
    //   戻り値: []
    // }
    ~WgcSessionState() {
        close();
    }

    // {
    //   責務: [start: MTA apartmentを維持する専用threadでtarget HWND用のWGC sessionを起動する]
    //   引数: [target_window: capture対象のHWND]
    //   戻り値: [void: 成功時にworkerがcapture requestを受け付ける]
    // }
    void start(HWND target_window) {
        window = target_window;
        std::promise<void> startup_result;
        auto startup_future = startup_result.get_future();
        worker_thread = std::thread([this, target_window, startup_result = std::move(startup_result)]() mutable {
            run_capture_thread(target_window, std::move(startup_result));
        });
        try {
            startup_future.get();
        } catch (...) {
            close();
            throw;
        }
    }

    // {
    //   責務: [initialize_capture: workerのMTA apartmentで選択HWNDのWGC resourcesを作る]
    //   引数: [target_window: capture対象のHWND]
    //   戻り値: [void: 成功時にcapture_sessionがframe取得を開始する]
    // }
    void initialize_capture(HWND target_window) {
        window = target_window;
        capture_item = create_capture_item(target_window);
        frame_pool_size = capture_item.Size();
        if (frame_pool_size.Width <= 0 || frame_pool_size.Height <= 0) {
            throw winrt::hresult_error(E_INVALIDARG);
        }

        capture_device = create_capture_device(native_device);
        // {
        //   理由: [CreateFreeThreadedを実行する現在のMTA workerからStatics2 factoryを取得してframe poolを作る]
        // }
        winrt::hresult_error frame_pool_factory_error{E_FAIL};
        auto frame_pool_factory = winrt::try_get_activation_factory<
            Direct3D11CaptureFramePool,
            winrt::Windows::Graphics::Capture::IDirect3D11CaptureFramePoolStatics2
        >(frame_pool_factory_error);
        if (!frame_pool_factory) {
            throw frame_pool_factory_error;
        }
        frame_pool = frame_pool_factory.CreateFreeThreaded(
            capture_device,
            kCapturePixelFormat,
            kCaptureFramePoolBufferCount,
            frame_pool_size
        );
        capture_session = frame_pool.CreateCaptureSession(capture_item);
        capture_session.StartCapture();
    }

    // {
    //   責務: [run_capture_thread: MTA apartmentとWGC resourcesを維持しcommand queueを処理する]
    //   引数: [target_window: capture対象HWND, startup_result: WGC resource初期化結果をstartへ返すpromise]
    //   戻り値: [void: resource作成から解放まで同じMTAを維持し、終了時にworker所有のWGC resourcesを解放する]
    // }
    void run_capture_thread(HWND target_window, std::promise<void> startup_result) {
        bool startup_completed = false;
        WinRtApartmentScope apartment;
        try {
            if (FAILED(apartment.status())) {
                throw winrt::hresult_error(apartment.status());
            }
            initialize_capture(target_window);
            startup_result.set_value();
            startup_completed = true;

            while (true) {
                std::function<void()> command;
                {
                    std::unique_lock command_lock(command_mutex);
                    command_ready.wait(command_lock, [this] {
                        return close_requested.load() || !pending_commands.empty();
                    });
                    if (close_requested.load() && pending_commands.empty()) {
                        break;
                    }
                    command = std::move(pending_commands.front());
                    pending_commands.pop_front();
                }
                command();
            }
            release_capture_resources();
            return;
        } catch (...) {
            if (!startup_completed) {
                try {
                    startup_result.set_exception(std::current_exception());
                } catch (...) {
                    // Startup caller may already have abandoned its promise after a thread failure.
                }
            }
        }
        release_capture_resources();
    }

    // {
    //   責務: [release_capture_resources: worker threadが所有するcapture sessionとframe poolを終了する]
    //   戻り値: [void: 再呼び出し可能な終了処理]
    // }
    void release_capture_resources() noexcept {
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
    }

    // {
    //   責務: [enqueue_command: close前のcapture要求だけをworker queueへ追加する]
    //   引数: [command: worker threadで実行するrequest]
    //   戻り値: [bool: request追加に成功した場合はtrue]
    // }
    bool enqueue_command(std::function<void()> command) {
        {
            std::lock_guard command_lock(command_mutex);
            if (close_requested.load()) {
                return false;
            }
            pending_commands.push_back(std::move(command));
        }
        command_ready.notify_one();
        return true;
    }

    // {
    //   責務: [close: worker終了を要求しWGC資源の解放完了まで待つ]
    //   戻り値: [void: worker join後に再呼び出しできる終了処理]
    // }
    void close() noexcept {
        std::lock_guard close_lock(close_mutex);
        close_requested.store(true);
        command_ready.notify_all();
        if (worker_thread.joinable() && worker_thread.get_id() != std::this_thread::get_id()) {
            worker_thread.join();
        }
    }

    // {
    //   責務: [next_frame: callerのthreadからframe requestを送り完了frameを受け取る]
    //   引数: [timeout_ms: workerがframe poolを確認する上限ミリ秒]
    //   戻り値: [unique_ptr<kadoka_wgc_frame>: callerがreleaseするtexture lease、timeoutまたはclose時はnull]
    // }
    std::unique_ptr<kadoka_wgc_frame> next_frame(uint32_t timeout_ms) {
        std::unique_lock request_lock(request_mutex, std::try_to_lock);
        if (!request_lock.owns_lock()) {
            throw winrt::hresult_error(E_ILLEGAL_METHOD_CALL);
        }
        if (close_requested.load()) {
            return nullptr;
        }

        auto result = std::make_shared<std::promise<std::unique_ptr<kadoka_wgc_frame>>>();
        auto result_future = result->get_future();
        const bool request_queued = enqueue_command([this, result, timeout_ms] {
            try {
                if (close_requested.load()) {
                    result->set_value(nullptr);
                } else {
                    result->set_value(capture_next_frame(timeout_ms));
                }
            } catch (...) {
                result->set_exception(std::current_exception());
            }
        });
        if (!request_queued) {
            return nullptr;
        }
        return result_future.get();
    }

    // {
    //   責務: [capture_next_frame: worker threadでframe poolをpollしlatest frame leaseを作る]
    //   引数: [timeout_ms: frame poolをpollする上限ミリ秒]
    //   戻り値: [unique_ptr<kadoka_wgc_frame>: frame lease、timeoutまたはclose時はnull]
    // }
    std::unique_ptr<kadoka_wgc_frame> capture_next_frame(uint32_t timeout_ms) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now() < deadline) {
            if (close_requested.load()) {
                return nullptr;
            }

            auto captured_frame = run_capture_operation("FramePool.TryGetNextFrame", [this] {
                return frame_pool.TryGetNextFrame();
            });
            if (close_requested.load()) {
                if (captured_frame) {
                    captured_frame.Close();
                }
                return nullptr;
            }
            if (!captured_frame) {
                const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                    deadline - std::chrono::steady_clock::now()
                );
                const auto poll_interval = std::min(remaining, kFramePoolPollInterval);
                if (poll_interval.count() > 0) {
                    Sleep(static_cast<DWORD>(poll_interval.count()));
                }
                continue;
            }
            while (auto newer_frame = run_capture_operation("FramePool.TryGetNextFrame", [this] {
                return frame_pool.TryGetNextFrame();
            })) {
                captured_frame.Close();
                captured_frame = std::move(newer_frame);
            }

            const auto content_size = run_capture_operation("CaptureFrame.ContentSize", [&captured_frame] {
                return captured_frame.ContentSize();
            });
            if (content_size.Width <= 0 || content_size.Height <= 0 ||
                static_cast<uint64_t>(content_size.Width) * kBytesPerPixel >
                    std::numeric_limits<uint32_t>::max()) {
                std::fprintf(
                    stderr,
                    "WGC CaptureFrame.ContentSize invalid: width=%ld height=%ld\n",
                    static_cast<long>(content_size.Width),
                    static_cast<long>(content_size.Height)
                );
                captured_frame.Close();
                throw winrt::hresult_error(E_INVALIDARG);
            }
            if (content_size.Width != frame_pool_size.Width || content_size.Height != frame_pool_size.Height) {
                // {
                //   理由: [WGCのresize通知frameが新しいContentSizeと古いtexture寸法を持つことがあるため、そのframeを破棄して新しい寸法でframe poolを作り直す]
                // }
                captured_frame.Close();
                run_capture_operation("FramePool.Recreate", [this, content_size] {
                    frame_pool.Recreate(
                        capture_device,
                        kCapturePixelFormat,
                        kCaptureFramePoolBufferCount,
                        content_size
                    );
                });
                frame_pool_size = content_size;
                continue;
            }

            auto surface = run_capture_operation("CaptureFrame.Surface", [&captured_frame] {
                return captured_frame.Surface();
            });
            auto surface_access = run_capture_operation("CaptureSurface.as", [&surface] {
                return surface.as<Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
            });
            auto frame_lease = std::make_unique<kadoka_wgc_frame>();
            frame_lease->copy_mutex = native_context_mutex;
            run_capture_operation("CaptureSurface.GetInterface", [&surface_access, &frame_lease] {
                winrt::check_hresult(surface_access->GetInterface(
                    __uuidof(ID3D11Texture2D),
                    frame_lease->texture.put_void()
                ));
            });
            D3D11_TEXTURE2D_DESC texture_description{};
            frame_lease->texture->GetDesc(&texture_description);
            if (texture_description.Format != DXGI_FORMAT_B8G8R8A8_UNORM) {
                std::fprintf(
                    stderr,
                    "WGC capture texture has an unsupported format: format=0x%08lX\n",
                    static_cast<unsigned long>(texture_description.Format)
                );
                captured_frame.Close();
                throw winrt::hresult_error(E_INVALIDARG);
            }
            if (texture_description.Width < static_cast<uint32_t>(content_size.Width) ||
                texture_description.Height < static_cast<uint32_t>(content_size.Height)) {
                // {
                //   理由: [FramePool.Recreate後もWGCが旧texture寸法の遷移frameを返す場合があるため、そのframeを破棄して新しいtextureが届くまで待つ]
                // }
                std::fprintf(
                    stderr,
                    "WGC resize transition frame discarded: texture=%ux%u content=%ldx%ld\n",
                    texture_description.Width,
                    texture_description.Height,
                    static_cast<long>(content_size.Width),
                    static_cast<long>(content_size.Height)
                );
                captured_frame.Close();
                continue;
            }

            frame_lease->width = static_cast<uint32_t>(content_size.Width);
            frame_lease->height = static_cast<uint32_t>(content_size.Height);
            frame_lease->frame_id = ++next_frame_id;
            const int64_t capture_time_units = run_capture_operation(
                "CaptureFrame.SystemRelativeTime",
                [&captured_frame] {
                    return captured_frame.SystemRelativeTime().count();
                }
            );
            if (capture_time_units > 0 && static_cast<uint64_t>(capture_time_units) <=
                    std::numeric_limits<uint64_t>::max() / kNanosecondsPerWinRtTimeUnit) {
                frame_lease->timestamp_ns =
                    static_cast<uint64_t>(capture_time_units) * kNanosecondsPerWinRtTimeUnit;
            }
            // {
            //   理由: [capture_frameをCloseするとframe poolが同じsurfaceを再利用するため、callerのBGRA copyが終わるlease releaseまでframeを保持する]
            // }
            frame_lease->capture_frame = std::move(captured_frame);
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

    if (!frame.copy_mutex) {
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    std::lock_guard copy_lock(*frame.copy_mutex);
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
//   責務: [kadoka_wgc_next_frame: workerへcaptureを依頼し最新textureをleaseとして返す]
//   引数: [session: 稼働中のWGC session, timeout_ms: workerのframe取得上限ミリ秒, out_frame: frame lease出力先]
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
    } catch (const winrt::hresult_error& error) {
        std::fprintf(
            stderr,
            "WGC operation 'WgcSessionState.next_frame' failed with HRESULT 0x%08lX\n",
            static_cast<unsigned long>(error.code().value)
        );
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    } catch (const std::exception& error) {
        std::fprintf(
            stderr,
            "WGC operation 'WgcSessionState.next_frame' failed: %s\n",
            error.what()
        );
        return KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    } catch (...) {
        std::fprintf(stderr, "WGC operation 'WgcSessionState.next_frame' failed with unknown exception\n");
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
    if (frame == nullptr || frame->capture_frame == nullptr ||
        frame->texture == nullptr || frame->copy_mutex == nullptr ||
        frame->width == 0u || frame->height == 0u ||
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
//   責務: [kadoka_wgc_frame_release: 保持中WGC frameをpoolへ返しframe leaseを解放してpointerをnullへ戻す]
//   引数: [frame: 解放するframe lease pointer]
//   戻り値: [int32_t: pool返却とlease解放が成功した場合はOK、close失敗はCAPTURE_FAILED、null pointerはINVALID_ARGUMENT]
// }
int32_t kadoka_wgc_frame_release(kadoka_wgc_frame** frame) {
    if (frame == nullptr || *frame == nullptr) {
        return KADOKA_RUNTIME_STATUS_INVALID_ARGUMENT;
    }
    int32_t release_status = KADOKA_RUNTIME_STATUS_OK;
    try {
        (*frame)->capture_frame.Close();
    } catch (...) {
        release_status = KADOKA_RUNTIME_STATUS_CAPTURE_FAILED;
    }
    delete *frame;
    *frame = nullptr;
    return release_status;
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
    (*session)->state->close();
    delete *session;
    *session = nullptr;
    return KADOKA_RUNTIME_STATUS_OK;
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
