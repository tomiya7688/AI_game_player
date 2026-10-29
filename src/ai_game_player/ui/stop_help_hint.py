"""Play画面で共通の停止方法と補足説明の出し方を案内します。"""

import tkinter as tk
from tkinter import ttk


# {
#   責務: [StopHelpHint: 全OSで共通の停止方法を表示し、割り当てられた画面幅で文章を折り返すLabel]
#   保持状態: [Tkのtextオプション: 停止方法と補足説明の操作手順, Tkのwraplengthオプション: 現在のLabel幅]
# }
class StopHelpHint(ttk.Label):
    # {
    #   責務: [__init__: 停止案内を左揃えで構築し、幅の変更へ折り返しを接続する]
    #   処理: [停止ボタンとフォーカス時のEscを案内し、詳細は停止ボタンのホバーまたはTabによるフォーカスで確認する手順を表示する。既存Configureバインドは置き換えない]
    #   引数: [master: Labelを配置する親ウィジェット]
    #   戻り値: [None: Labelを構築する。配置は呼び出し側のpack/grid等で指定する]
    # }
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(
            master,
            text=(
                "停止方法: 停止ボタン / 本アプリにフォーカスがある状態のEsc\n"
                "詳しい停止条件は、停止ボタンにマウスを置くかTabでフォーカスを合わせて確認してください"
            ),
            justify=tk.LEFT,
        )
        self.bind("<Configure>", self._fit_text_to_width, add="+")

    # {
    #   責務: [_fit_text_to_width: Labelに割り当てられた幅を文章の折り返し幅へ反映する]
    #   処理: [Configureイベントが正の幅を報告した場合だけwraplengthを更新する]
    #   引数: [event: このLabelの表示サイズが変化したTkイベント。widthは現在の幅をpixel単位で表す]
    #   戻り値: [None: 案内文と停止処理は変更せず、折り返し幅だけを更新する]
    # }
    def _fit_text_to_width(self, event: tk.Event) -> None:
        if event.width > 0:
            self.configure(wraplength=event.width)
