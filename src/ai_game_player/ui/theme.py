"""Application-wide visual theme derived from the 賢者ミスク dot-art palette."""

import tkinter as tk
from tkinter import ttk

# 賢者ミスクの原画は黒背景・白い体・灰色の影・黒い目の3色で描かれている。
# 画面全体をその3色の夜景に揃え、停止・警告だけを色相のある色で目立たせる。
PALETTE = {
    "night": "#000000",
    "surface": "#0E0F14",
    "panel": "#16181F",
    "raised": "#22252E",
    "line": "#33363F",
    "ghost": "#FFFFFF",
    "text": "#ECECEC",
    "shade": "#C8C8C8",
    "muted": "#8C8F99",
    "ink": "#000000",
    "select": "#3A3D47",
    "danger": "#E5484D",
    "danger_active": "#FF6369",
    "warning": "#F5C04A",
}

FONT_FAMILY = "TkDefaultFont"
MONO_FAMILY = "TkFixedFont"


# {
#   責務: [apply_misk_theme: Tkルート全体のttk styleとoption databaseを賢者ミスクの配色へ揃える]
#   処理: [
#     1: 色を細かく指定できるclamテーマを基にし、全ttk部品の背景・文字・枠線を夜色と白へ設定する
#     2: ボタンは白い体に黒い目の配色、停止ボタンだけは赤、補助文字は影の灰色で区別する
#     3: tk.Textなどttk外の部品も同じ色になるよう、option databaseへ既定色を登録する
#   ]
#   引数: [root: styleを適用するTkルート]
#   戻り値: [ttk.Style: 適用済みのstyle。呼び出し側が追加styleを定義するときに使う]
# }
def apply_misk_theme(root: tk.Misc) -> ttk.Style:
    color = PALETTE
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    root.configure(background=color["surface"])

    style.configure(
        ".",
        background=color["surface"],
        foreground=color["text"],
        fieldbackground=color["panel"],
        bordercolor=color["line"],
        darkcolor=color["surface"],
        lightcolor=color["surface"],
        troughcolor=color["panel"],
        focuscolor=color["shade"],
        selectbackground=color["select"],
        selectforeground=color["ghost"],
        insertcolor=color["ghost"],
        arrowcolor=color["shade"],
    )
    style.map(
        ".",
        foreground=[("disabled", color["muted"])],
        background=[("disabled", color["surface"])],
    )
    style.configure("TFrame", background=color["surface"])
    style.configure("TLabel", background=color["surface"], foreground=color["text"])
    style.configure(
        "TLabelframe",
        background=color["surface"],
        bordercolor=color["line"],
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label", background=color["surface"], foreground=color["shade"]
    )
    style.configure("TSeparator", background=color["line"])
    style.configure("Sash", background=color["line"], sashthickness=4)

    style.configure(
        "TButton",
        background=color["ghost"],
        foreground=color["ink"],
        # 隣接ボタンが白い1枚に見えないよう、枠を背景色にして隙間を作る。
        bordercolor=color["surface"],
        lightcolor=color["surface"],
        darkcolor=color["surface"],
        borderwidth=2,
        padding=(10, 4),
        relief="solid",
    )
    style.map(
        "TButton",
        background=[
            ("disabled", color["raised"]),
            ("pressed", color["shade"]),
            ("active", color["shade"]),
        ],
        foreground=[("disabled", color["muted"])],
    )
    style.configure(
        "Stop.TButton",
        background=color["danger"],
        foreground=color["ghost"],
        bordercolor=color["danger"],
        lightcolor=color["danger"],
        darkcolor=color["danger"],
        font=(FONT_FAMILY, 10, "bold"),
    )
    style.map(
        "Stop.TButton",
        background=[("pressed", color["danger"]), ("active", color["danger_active"])],
    )

    style.configure(
        "TEntry",
        fieldbackground=color["panel"],
        foreground=color["ghost"],
        bordercolor=color["line"],
        insertcolor=color["ghost"],
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", color["shade"])],
        fieldbackground=[("disabled", color["surface"])],
    )
    style.configure(
        "TCombobox",
        fieldbackground=color["panel"],
        background=color["raised"],
        foreground=color["ghost"],
        arrowcolor=color["shade"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", color["panel"])],
        foreground=[("readonly", color["ghost"])],
        selectbackground=[("readonly", color["panel"])],
        selectforeground=[("readonly", color["ghost"])],
    )
    for indicator_style in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            indicator_style,
            background=color["surface"],
            foreground=color["text"],
            indicatorbackground=color["panel"],
            indicatorforeground=color["ghost"],
        )
        style.map(
            indicator_style,
            background=[("active", color["surface"])],
            indicatorbackground=[("selected", color["ghost"])],
            indicatorforeground=[("selected", color["ink"])],
        )
    style.configure("TScrollbar", background=color["raised"], bordercolor=color["line"])

    # Navigator: 選択中のWorkspaceを白地に黒文字（ミスクの顔）で示す。
    style.configure(
        "Nav.TRadiobutton",
        background=color["surface"],
        foreground=color["shade"],
        padding=(10, 5),
        indicatorsize=0,
        indicatormargin=0,
    )
    style.layout(
        "Nav.TRadiobutton",
        [
            (
                "Radiobutton.padding",
                {
                    "sticky": "nswe",
                    "children": [("Radiobutton.label", {"sticky": "w"})],
                },
            )
        ],
    )
    style.map(
        "Nav.TRadiobutton",
        background=[("selected", color["ghost"]), ("active", color["raised"])],
        foreground=[("selected", color["ink"]), ("active", color["ghost"])],
    )

    style.configure("Header.TFrame", background=color["night"])
    style.configure(
        "Header.TLabel", background=color["night"], foreground=color["ghost"]
    )
    style.configure(
        "HeaderTitle.TLabel",
        background=color["night"],
        foreground=color["ghost"],
        font=(FONT_FAMILY, 12, "bold"),
    )
    style.configure(
        "HeaderSub.TLabel", background=color["night"], foreground=color["muted"]
    )
    style.configure(
        "Status.TLabel",
        background=color["raised"],
        foreground=color["ghost"],
        padding=(8, 3),
    )
    style.configure("Muted.TLabel", foreground=color["muted"])
    style.configure(
        "Section.TLabel", foreground=color["shade"], font=(FONT_FAMILY, 10, "bold")
    )
    style.configure(
        "Heading.TLabel", foreground=color["ghost"], font=(FONT_FAMILY, 14, "bold")
    )

    style.configure("Seat.TFrame", background=color["night"])
    style.configure("Seat.TLabel", background=color["night"], foreground=color["ghost"])
    style.configure(
        "SeatName.TLabel",
        background=color["night"],
        foreground=color["ghost"],
        font=(FONT_FAMILY, 11, "bold"),
    )
    style.configure(
        "SeatMeta.TLabel", background=color["night"], foreground=color["muted"]
    )
    style.configure(
        "SeatBubble.TLabel",
        background=color["ghost"],
        foreground=color["ink"],
        padding=(8, 6),
    )

    for option, value in (
        ("*Text.background", color["panel"]),
        ("*Text.foreground", color["text"]),
        ("*Text.insertBackground", color["ghost"]),
        ("*Text.selectBackground", color["select"]),
        ("*Text.selectForeground", color["ghost"]),
        ("*Text.highlightThickness", 1),
        ("*Text.highlightBackground", color["line"]),
        ("*Text.highlightColor", color["shade"]),
        ("*Text.relief", "flat"),
        ("*Text.font", MONO_FAMILY),
        ("*Listbox.background", color["panel"]),
        ("*Listbox.foreground", color["text"]),
        ("*Listbox.selectBackground", color["select"]),
        ("*TCombobox*Listbox.background", color["panel"]),
        ("*TCombobox*Listbox.foreground", color["ghost"]),
        ("*TCombobox*Listbox.selectBackground", color["ghost"]),
        ("*TCombobox*Listbox.selectForeground", color["ink"]),
        ("*Toplevel.background", color["surface"]),
    ):
        root.option_add(option, value)
    return style
