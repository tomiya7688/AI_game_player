"""Player Seat: shows which Player Character is currently in charge of the game."""

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ai_game_player.player_character import PlayerAssetPack, PlayerState, nearest_scale
from ai_game_player.ui.theme import PALETTE, ui_font

STATE_LABELS = {
    PlayerState.IDLE: "待機中",
    PlayerState.OBSERVE: "観察中",
    PlayerState.THINK: "考え中",
    PlayerState.ACT: "操作中",
    PlayerState.EVALUATE: "評価中",
    PlayerState.UNKNOWN: "状態不明",
    PlayerState.WARNING: "要確認",
    PlayerState.STOP: "停止",
}
STATE_COLORS = {
    PlayerState.WARNING: PALETTE["warning"],
    PlayerState.STOP: PALETTE["danger"],
}
# 停止・要確認では動きで注意を散らさないよう、浮遊を止めて基準位置に置く。
STILL_STATES = frozenset({PlayerState.STOP, PlayerState.WARNING})
SEAT_IMAGE_PX = 128
FLOAT_INTERVAL_MS = 600
FLOAT_OFFSET_PX = 3


# {
#   責務: [load_scaled_photo: dot-art画像をnearest-neighborの整数倍率で目標サイズへ変換したPhotoImageを返す]
#   処理: [PNGを読み、nearest_scaleが返す倍率でzoomまたはsubsampleする。読めない場合はNoneを返す]
#   引数: [master: PhotoImageを所有するTk部品, path: 画像パス, target_px: 表示したい一辺のpixel数]
#   戻り値: [tk.PhotoImage | None: 変換済み画像。呼び出し側は参照を保持する必要がある]
# }
def load_scaled_photo(
    master: tk.Misc, path: Path, target_px: int
) -> tk.PhotoImage | None:
    try:
        source = tk.PhotoImage(master=master, file=str(path))
        zoom, subsample = nearest_scale(max(source.width(), source.height()), target_px)
        if zoom > 1:
            return source.zoom(zoom)
        if subsample > 1:
            return source.subsample(subsample)
        return source
    except (tk.TclError, ValueError):
        return None


# {
#   責務: [PlayerSeat: 現在のoccupantの画像・名前・状態・脳内メッセージを1つの席として表示し、描画失敗をCoreへ伝播させない]
#   フィールド: [
#     frame: 席全体を収めるttk.Frame。親が配置する,
#     occupant: 現在座っているPlayerAssetPack,
#     state: 現在表示しているPlayerState,
#     canvas: occupant画像または文字fallbackを描くCanvas,
#     bubble / license_label: 脳内メッセージの吹き出しとLicense名の表示欄,
#     name_var / state_var / message_var / license_var: 各表示欄の文字列変数,
#     _photo: Canvasが参照する画像。GCで消えないよう保持する,
#     _float_job / _float_up: 浮遊表示のafter予約と、画像が基準位置より上にあるか,
#     _pack_options: set_visible(False)の直前に保存した親でのpack設定
#   ]
# }
class PlayerSeat:
    # {
    #   責務: [__init__: 席の表示部品を構築し、初期occupantを座らせる]
    #   処理: [Canvas・名前・状態・吹き出し・License名を縦に配置し、occupantをIDLE状態で表示して浮遊表示を開始する]
    #   引数: [master: 席を置く親部品, occupant: 初期occupant, animate: Falseなら浮遊表示をしない]
    #   戻り値: [None]
    # }
    def __init__(
        self, master: tk.Misc, occupant: PlayerAssetPack, *, animate: bool = True
    ) -> None:
        self.frame = ttk.Frame(master, style="Seat.TFrame", padding=(8, 10))
        self.occupant = occupant
        self.state = PlayerState.IDLE
        self._photo: tk.PhotoImage | None = None
        self._float_job: str | None = None
        self._float_up = False
        self._animate = animate
        self._pack_options: dict[str, object] | None = None

        ttk.Label(self.frame, text="PLAYER SEAT", style="SeatMeta.TLabel").pack(
            anchor=tk.W
        )
        self.canvas = tk.Canvas(
            self.frame,
            width=SEAT_IMAGE_PX,
            height=SEAT_IMAGE_PX + FLOAT_OFFSET_PX,
            background=PALETTE["night"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(pady=(4, 2))
        self.name_var = tk.StringVar(master=self.frame)
        self.state_var = tk.StringVar(master=self.frame)
        self.message_var = tk.StringVar(master=self.frame)
        self.license_var = tk.StringVar(master=self.frame)
        ttk.Label(
            self.frame, textvariable=self.name_var, style="SeatName.TLabel"
        ).pack()
        self.state_label = ttk.Label(
            self.frame, textvariable=self.state_var, style="Seat.TLabel"
        )
        self.state_label.pack(pady=(0, 6))
        self.bubble = ttk.Label(
            self.frame,
            textvariable=self.message_var,
            style="SeatBubble.TLabel",
            # 吹き出しの左右padding分を引き、席の幅を超えて文字が欠けないようにする。
            wraplength=SEAT_IMAGE_PX - 8,
            justify=tk.LEFT,
        )
        self.bubble.pack(fill=tk.X)
        self.license_label = ttk.Label(
            self.frame,
            textvariable=self.license_var,
            style="SeatMeta.TLabel",
            wraplength=SEAT_IMAGE_PX + 8,
        )
        self.license_label.pack(anchor=tk.W, pady=(6, 0))
        self.frame.bind("<Destroy>", self._on_destroy, add="+")
        self.set_occupant(occupant)
        self._schedule_float()

    # {
    #   責務: [set_occupant: 席に座るPlayerを差し替える]
    #   処理: [画像をnearest-neighborで席サイズへ変換して描き、画像がなければ表示名の頭文字を描く。名前・License名・現在状態の表示も更新する]
    #   引数: [occupant: 新しく座るPlayerAssetPack]
    #   戻り値: [bool: 描画できたらTrue。Tk描画に失敗した場合はFalseで、例外は送出しない]
    # }
    def set_occupant(self, occupant: PlayerAssetPack) -> bool:
        self.occupant = occupant
        try:
            self.canvas.delete("occupant")
            self._photo = None
            if occupant.player_image is not None and occupant.has_image():
                self._photo = load_scaled_photo(
                    self.canvas, occupant.player_image, SEAT_IMAGE_PX
                )
            center = SEAT_IMAGE_PX // 2
            if self._photo is not None:
                self.canvas.create_image(
                    center,
                    center + FLOAT_OFFSET_PX,
                    image=self._photo,
                    tags=("occupant",),
                )
            else:
                self.canvas.create_text(
                    center,
                    center + FLOAT_OFFSET_PX,
                    text=(occupant.display_name[:1] or "?"),
                    fill=PALETTE["ghost"],
                    font=ui_font(self.canvas, 40),
                    tags=("occupant",),
                )
            self.name_var.set(occupant.display_name)
            self.license_var.set(
                f"License: {occupant.license_name}" if occupant.license_name else ""
            )
            self._float_up = False
            return self.set_state(self.state)
        except tk.TclError:
            return False

    # {
    #   責務: [set_state: runtime状態を席の状態表示と脳内メッセージへ反映する]
    #   処理: [状態名と色を更新し、occupantの状態別メッセージを吹き出しへ表示する。メッセージが空なら吹き出しを隠す。停止・要確認では画像を基準位置へ戻す。ラベル未定義の状態は「状態不明」と表示する]
    #   引数: [state: 表示するPlayerState]
    #   戻り値: [bool: 表示できたらTrue。Tk部品が破棄済みなどで失敗した場合はFalse]
    # }
    def set_state(self, state: PlayerState) -> bool:
        self.state = state
        try:
            label = STATE_LABELS.get(state, STATE_LABELS[PlayerState.UNKNOWN])
            self.state_var.set(f"● {label}")
            self.state_label.configure(
                foreground=STATE_COLORS.get(state, PALETTE["shade"])
            )
            message = self.occupant.message_for(state)
            self.message_var.set(message)
            if message:
                # 一度隠した吹き出しは末尾へ再packされるため、License名の上へ戻す。
                self.bubble.pack(fill=tk.X, before=self.license_label)
            else:
                self.bubble.pack_forget()
            if state in STILL_STATES and self._float_up:
                self.canvas.move("occupant", 0, FLOAT_OFFSET_PX)
                self._float_up = False
            return True
        except tk.TclError:
            return False

    # {
    #   責務: [set_visible: 席全体の表示・非表示を切り替える]
    #   処理: [隠す前に親でのpack設定を保存してpack_forgetし、再表示ではその設定（before等の位置を含む）で戻す。保存した後続の部品が使えなければ位置指定を外して戻す。保存がなければ横幅いっぱいでpackする]
    #   引数: [visible: Trueで表示、Falseで非表示]
    #   戻り値: [bool: 切り替えられたら、または既に非表示ならTrue。破棄済みや親がpack以外で配置している場合はFalse]
    # }
    def set_visible(self, visible: bool) -> bool:
        try:
            manager = self.frame.winfo_manager()
            if manager not in ("", "pack"):
                return False
            if visible:
                options = self._pack_options or {"fill": tk.X}
                try:
                    self.frame.pack(**options)
                except tk.TclError:
                    options.pop("before", None)
                    self.frame.pack(**options)
                self._pack_options = None
            elif manager == "pack":
                info = self.frame.pack_info()
                info.pop("in", None)
                siblings = self.frame.master.pack_slaves()
                if self.frame in siblings:
                    index = siblings.index(self.frame)
                    if index + 1 < len(siblings):
                        info["before"] = siblings[index + 1]
                self._pack_options = info
                self.frame.pack_forget()
            return True
        except tk.TclError:
            return False

    # {
    #   責務: [_schedule_float: 幽霊らしく画像を上下にゆっくり揺らす次の描画を予約する]
    #   処理: [animateが有効なら一定間隔で_floatを予約する]
    #   引数: []
    #   戻り値: [None]
    # }
    def _schedule_float(self) -> None:
        if not self._animate:
            return
        try:
            self._float_job = self.frame.after(FLOAT_INTERVAL_MS, self._float)
        except tk.TclError:
            self._float_job = None

    # {
    #   責務: [_float: occupant画像を上下に1段動かす]
    #   処理: [停止・要確認状態では動かさず、それ以外は上下位置を交互に切り替えて次を予約する]
    #   引数: []
    #   戻り値: [None]
    # }
    def _float(self) -> None:
        self._float_job = None
        try:
            if self.state not in STILL_STATES:
                offset = FLOAT_OFFSET_PX if self._float_up else -FLOAT_OFFSET_PX
                self.canvas.move("occupant", 0, offset)
                self._float_up = not self._float_up
            self._schedule_float()
        except tk.TclError:
            return

    # {
    #   責務: [_on_destroy: 席の破棄時に浮遊表示の予約を取り消す]
    #   処理: [席のFrame自身の破棄イベントだけでafter予約を取り消す]
    #   引数: [event: Destroyイベント]
    #   戻り値: [None]
    # }
    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self.frame or self._float_job is None:
            return
        try:
            self.frame.after_cancel(self._float_job)
        except tk.TclError:
            pass
        self._float_job = None
