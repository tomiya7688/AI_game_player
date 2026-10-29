"""Tkウィジェットへ遅延表示の補足説明を追加し、対象の破棄時に予約を取り消します。"""

import tkinter as tk


# {
#   責務: [ToolTip: 一つのTkウィジェットに、ホバーまたはキーボードフォーカスで補足説明を表示する]
#   フィールド: [widget: 説明対象, text: 表示文, delay_ms: 表示までの待機時間, _after_id: 未実行の予約IDまたはNone, _tip_window: 表示中の説明窓またはNone]
# }
class ToolTip:
    # {
    #   責務: [__init__: 対象への説明表示と終了イベントを既存のバインドへ追加する]
    #   処理: [進入とフォーカス取得で表示を予約し、退出・フォーカス喪失・クリック・Esc・非表示・破棄で終了する。フォーカスや入力の所有権は変更しない]
    #   引数: [widget: 表示先のTkウィジェット, text: 利用者へ表示する説明, delay_ms: 0以上の待機ミリ秒（既定400）]
    #   戻り値: [None: ウィジェットが持つバインド経由でToolTipを保持する。負の待機時間はValueError]
    # }
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400) -> None:
        if delay_ms < 0:
            raise ValueError("ToolTipの表示待機時間は0以上のミリ秒を指定してください")
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip_window: tk.Toplevel | None = None
        for event_name in ("<Enter>", "<FocusIn>"):
            widget.bind(event_name, self._schedule, add="+")
        for event_name in (
            "<Leave>",
            "<FocusOut>",
            "<ButtonPress>",
            "<Escape>",
            "<Unmap>",
        ):
            widget.bind(event_name, self.hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    # {
    #   責務: [_schedule: 未実行の表示予約を置き換えて説明を遅延表示する]
    #   処理: [既存の予約と説明窓を終了し、テキストがある場合だけafterでshowを予約する]
    #   引数: [_event: 進入またはフォーカス取得のTkイベント。処理には使用しない]
    #   戻り値: [None: 表示の完了はTkイベントループへ委ねる]
    # }
    def _schedule(self, _event: tk.Event | None = None) -> None:
        self.hide()
        if self.text:
            self._after_id = self.widget.after(self.delay_ms, self.show)

    # {
    #   責務: [show: 説明対象が表示中の場合に、フォーカスを奪わず説明窓を一つだけ表示する]
    #   処理: [未実行の予約を取り消し、対象の下にToplevelと折返し付きLabelを作り、Tkの仮想画面内へ位置を調整する。破棄済み対象などのTclErrorでは部分生成した窓も片付ける]
    #   引数: [self: 説明対象と表示文を保持するToolTip]
    #   戻り値: [None: 空テキスト・非表示対象・表示済みでは新しい説明窓を作らない]
    # }
    def show(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip_window is not None or not self.text:
            return
        try:
            if not self.widget.winfo_viewable():
                return
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self._tip_window = tk.Toplevel(self.widget, takefocus=False)
            self._tip_window.wm_overrideredirect(True)
            tip_window = self._tip_window
            tk.Label(
                self._tip_window,
                text=self.text,
                justify=tk.LEFT,
                background="#ffffe0",
                foreground="#000000",
                relief=tk.SOLID,
                borderwidth=1,
                wraplength=360,
                padx=4,
                pady=2,
            ).pack()
            tip_window.update_idletasks()
            self._position_popup(tip_window, x, y)
        except tk.TclError:
            # Tk終了・対象破棄と表示が重なった場合、作成途中の説明窓を残さない。
            self.hide()

    # {
    #   責務: [_position_popup: 説明窓の左上位置をTkが報告する仮想画面の範囲へ収める]
    #   処理: [説明窓の要求サイズを使い、右下にはみ出す分だけ左上へ移す。説明窓自体が画面より大きい場合は画面左上を優先する]
    #   引数: [tip_window: サイズ計算済みの説明窓, x: 対象直下の希望screen x座標, y: 対象直下の希望screen y座標]
    #   戻り値: [None: 仮想画面の原点を含むscreen座標で説明窓を配置する]
    # }
    def _position_popup(self, tip_window: tk.Toplevel, x: int, y: int) -> None:
        screen_left = self.widget.winfo_vrootx()
        screen_top = self.widget.winfo_vrooty()
        max_left = (
            screen_left + self.widget.winfo_vrootwidth() - tip_window.winfo_reqwidth()
        )
        max_top = (
            screen_top + self.widget.winfo_vrootheight() - tip_window.winfo_reqheight()
        )
        x = max(screen_left, min(x, max_left))
        y = max(screen_top, min(y, max_top))
        # +符号を明示し、負の座標を「右端からの距離」ではなく絶対座標として渡す。
        tip_window.wm_geometry(f"+{x}+{y}")

    # {
    #   責務: [hide: 表示予約と説明窓を終了して、未表示状態へ戻す]
    #   処理: [予約IDと説明窓の参照を先に外してから取消し・破棄する。Tk終了済みの場合のTclErrorだけを無視する]
    #   引数: [_event: 退出・クリック・フォーカス喪失などのTkイベント。処理には使用しない]
    #   戻り値: [None: 連続して呼んでも説明窓や予約を残さず、既存のキー・クリック処理を止めない]
    # }
    def hide(self, _event: tk.Event | None = None) -> None:
        after_id, self._after_id = self._after_id, None
        tip_window, self._tip_window = self._tip_window, None
        try:
            if after_id is not None:
                self.widget.after_cancel(after_id)
        except tk.TclError:
            pass
        try:
            if tip_window is not None:
                tip_window.destroy()
        except tk.TclError:
            pass

    # {
    #   責務: [_on_destroy: 説明対象自体の破棄時に表示予約と説明窓を終了する]
    #   処理: [子の説明窓から伝播したDestroyは除外し、対象widgetのDestroyだけでhideを呼ぶ]
    #   引数: [event: 破棄されたウィジェットを持つTkイベント]
    #   戻り値: [None: 他のDestroyバインドは継続する]
    # }
    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget == self.widget:
            self.hide()
