import tkinter as tk


# {
#   責務: [
#     ToolTip: Tkinterウィジェットにマウスホバーで補助説明テキストを表示するポップアップ
#   ]
#   フィールド: [
#     widget: ツールチップを紐付けるTkinterウィジェット
#     text: ホバー時に表示する説明テキスト
#     delay_ms: ホバー開始から表示までの遅延ミリ秒
#     _tip_window: 表示中のToplevelウィンドウまたは未表示時None
#     _after_id: タイマー予約IDまたは予約なし時None
#   ]
# }
class ToolTip:
    # {
    #   責務: [
    #     __init__: ウィジェットにマウスホバーイベントバインディングを登録する
    #   ]
    #   処理: [
    #     1: ウィジェット、テキスト、表示遅延ミリ秒を保持する
    #     2: <Enter>、<Leave>、<ButtonPress>イベントにハンドラーを追加バインドする
    #   ]
    #   引数: [
    #     widget: ツールチップを表示する対象ウィジェット
    #     text: ツールチップに表示する案内テキスト
    #     delay_ms: ホバー検出から表示までのミリ秒（既定値400ms）
    #   ]
    #   戻り値: [なし]
    # }
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._tip_window: tk.Toplevel | None = None
        self._after_id: str | None = None

        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")

    # {
    #   責務: [
    #     _on_enter: マウスカーソル進入時にツールチップ表示タイマーを開始する
    #   ]
    #   処理: [
    #     1: タイマーを予約する
    #   ]
    #   引数: [
    #     _event: Tkinterイベントオブジェクト（未使用）
    #   ]
    #   戻り値: [なし]
    # }
    def _on_enter(self, _event: tk.Event | None = None) -> None:
        self._schedule()

    # {
    #   責務: [
    #     _on_leave: マウスカーソル退出またはクリック時にツールチップを消去する
    #   ]
    #   処理: [
    #     1: 予約中のタイマーを取り消す
    #     2: 表示中のツールチップウィンドウを破棄する
    #   ]
    #   引数: [
    #     _event: Tkinterイベントオブジェクト（未使用）
    #   ]
    #   戻り値: [なし]
    # }
    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self.hide()

    # {
    #   責務: [
    #     _schedule: 指定された遅延後にツールチップを表示するafter予約を登録する
    #   ]
    #   処理: [
    #     1: 既存の予約があれば取り消す
    #     2: widget.afterでshowメソッドを遅延呼び出し予約する
    #   ]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def _schedule(self) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self.show)

    # {
    #   責務: [
    #     _cancel: 予約済みのツールチップ表示タイマーを取り消す
    #   ]
    #   処理: [
    #     1: after_idが存在する場合widget.after_cancelを呼び出しタイマーを初期化する
    #   ]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def _cancel(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    # {
    #   責務: [
    #     show: ツールチップをウィジェット下部に表示する
    #   ]
    #   処理: [
    #     1: ウィジェットが有効かつテキストがある場合のみToplevelウィンドウを生成する
    #     2: ウィジェットの画面座標を取得し、その直下にツールチップを配置する
    #     3: 枠線と背景色を設定したラベルをToplevel内に配置する
    #   ]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def show(self) -> None:
        if self._tip_window or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return

        tw = tk.Toplevel(self.widget)
        self._tip_window = tw
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            background="#ffffe0",
            foreground="#000000",
            relief=tk.SOLID,
            borderwidth=1,
            font=("TkDefaultFont", 9, "normal"),
            padx=4,
            pady=2,
        )
        label.pack(ipadx=1)

    # {
    #   責務: [
    #     hide: 表示中のツールチップウィンドウを破棄する
    #   ]
    #   処理: [
    #     1: _tip_windowが存在する場合destroyを呼び出して破棄しNoneに戻す
    #   ]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def hide(self) -> None:
        if self._tip_window:
            try:
                self._tip_window.destroy()
            except Exception:
                pass
            self._tip_window = None
