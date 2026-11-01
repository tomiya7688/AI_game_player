# 参照ブラウザーの失敗後だけに行う描画診断（明示検証のみ）

Windows の initial observation が全空盤面のまま期限切れになる場合、
画素だけでは未実行の初期 RAF、DOM 構築、CSS 表示、GDI の合成面取得を
分離できない。原因はまだ確定していない。本診断は失敗を保ったまま、
既に保全された同一フレームの**後**に一度だけ DOM の構造を診断する。
ブラウザーへの接続自体がスケジューリングへ影響し得るため、接続後の
状態を元の画像の状態と同一とは扱わない。

## 起動と呼び出し

`ReferenceGame2048Launcher(..., post_failure_render_diagnostics=True)` の
明示指定時だけ、私有 profile と `127.0.0.1` の ephemeral DevTools port を
有効にする。通常起動の Chrome 引数は変えない。失敗確定と画素保全が
済んだ呼び出し側が `collect_failure_render_diagnostics(failure_confirmed=True,
frame_preserved=True)` を一度呼ぶ。再試行・入力・reload・RAF 登録・
状態修復を行わず、元の失敗判定を変更しない。

`tools/reference_browser_diagnostics.py` は元 browser process が生存し、
その PID が元 HWND を所有することを接続前後で確認する。私有 profile の
`DevToolsActivePort`、loopback port、単一 page target の元 URL 完全一致を
確認する。URL、WebSocket token、自由な例外・Console 本文は出力しない。

## 上限と出力

optional validation dependency は `websockets==15.0.1`。
標準の asyncio client を使用し、独自 WebSocket framing は実装しない。
全体は共通 1 秒の soft deadline であり、OS 同期 I/O やスケジューリングの
hard 上限を保証しない。私有 profile と port file の lstat、regular file・
reparse/symlink・size 検査と open 後の fstat 照合を行い、FIFO 等を拒否する。
利用可能な OS では NONBLOCK/NOFOLLOW も使う。HTTP `/json/list` は別枠で
body 16 KiB・JSON field 256 個・深さ 8 を上限とする。WebSocket は
message 16 KiB・返信累積 64 KiB・イベント最大 8 件・返信累積 JSON field
256 個・深さ 8・queue 1・close timeout 20 ms を上限とする。redirect は追わない。
ライブラリーは通常の製品起動・Source/Planner の依存にはしない。

送信する CDP は固定 `Runtime.evaluate` 一件だけである。
`readyState`、visibility、focus、既知 constructor の存在、静的 grid と
tile root の存在、上限 32 の tile DOM 数、opacity と可視矩形の boolean
だけを返す。DOM collection 全体を配列化せず、先頭最大 32 tile・各最大
4 子要素だけを添字 loop で参照し、返却値の cap と DOM 作業の cap を保つ。
tile の値、得点、localStorage、ゲーム内部 state、任意式を
取得しない。失敗や不正応答は固定ラベルの unavailable とし、取得値を
AI・Source・cache・lease・学習へ戻さない。

サーバー側は固定 index/JS/CSS allowlist の status、書込済み byte 数、
本文 SHA256 と件数だけを保持する。完了は server write の完了を意味し、
ブラウザーが受信・実行・描画した証明にはならない。
BrokenPipe 等の送信失敗も response 1 件・completed false として記録する。
例外発生までに実際に送信された byte 数は不明なので、seeded page では
0 の下限値と unknown flag を記録し、完了した本文 hash は出さない。

## 現時点の検証

isolated tests で default off、loopback opt-in、失敗/画素保全 barrier、
一度限り、deadline、所有 target、redirect/サイズ/field/イベント上限、
固定式と自由本文の非出力を検証する。Linux の実 Chromium と固定版
2048 に対する transport smoke では DOM 数 2 を取得した。Linux では
Windows HWND 所有確認を mock しているため、Windows 実機の所有照合・
失敗原因・最終 CI は未検証である。明示検証経路へ接続した診断であり、元の
initial observation failure を修正したと主張しない。

参考: [CDP Runtime.evaluate](https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate)、
[websockets 15.0.1 asyncio client](https://websockets.readthedocs.io/en/15.0.1/reference/asyncio/client.html)。
