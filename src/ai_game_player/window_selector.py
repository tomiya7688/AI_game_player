import ctypes
import os
from dataclasses import dataclass


# {
#   責務: [
#     WindowInfo: 対象ウィンドウをHWNDと所有PIDで識別する
#   ]
#   フィールド: [
#     handle: ウィンドウハンドル
#     title: 一覧表示用タイトル
#     process_id: 列挙時点でHWNDを所有するプロセスID
#   ]
#   処理: [
#     1: 列挙時点の識別情報を不変値として保持する
#   ]
# }
@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_id: int = 0

    # {
    #   責務: [
    #     display_name: 選択一覧でウィンドウを識別できる表示名を返す
    #   ]
    #   処理: [
    #     1: タイトルとHWNDを表示形式へ整える
    #   ]
    #   引数: []
    #   戻り値: [
    #     str: タイトルと16進HWNDを含む表示名
    #   ]
    # }
    @property
    def display_name(self) -> str:
        return f"{self.title} (HWND 0x{self.handle:X})"


# {
#   責務: [
#     WindowsWindowSelector: 表示中のトップレベルウィンドウとプロセスIDを列挙する
#   ]
#   フィールド: []
#   処理: [
#     1: Windows APIでトップレベルウィンドウを走査する
#     2: タイトル・HWND・PIDをWindowInfoへ保存する
#   ]
# }
class WindowsWindowSelector:
    # {
    #   責務: [
    #     list_windows: 操作対象にできるウィンドウの識別情報を取得する
    #   ]
    #   処理: [
    #     1: Windows以外では明示的に失敗する
    #     2: タイトル付きトップレベルウィンドウを列挙する
    #     3: 各HWNDのプロセスIDを取得する
    #   ]
    #   引数: []
    #   戻り値: [
    #     list[WindowInfo]: タイトル・HWND・PIDの一覧
    #   ]
    #   エラー: [
    #     Windows以外で呼び出すとRuntimeError
    #   ]
    # }
    def list_windows(self) -> list[WindowInfo]:
        if os.name != "nt":
            raise RuntimeError("WindowsWindowSelector requires Windows")
        user32 = ctypes.windll.user32
        windows: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                process_id = ctypes.c_ulong()
                if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) and process_id.value:
                    windows.append(WindowInfo(int(hwnd), buffer.value, int(process_id.value)))
            return True

        user32.EnumWindows(callback_type(callback), 0)
        return windows
