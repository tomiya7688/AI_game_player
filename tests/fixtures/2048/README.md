# 固定版2048の認識回帰資料

認識器の開発者は、`standard.json`の合成画面、`terminal-render/`の終了画面、`gdi-row-gap.*`のCI実失敗画素を使い、盤面認識を検査できます。`gdi-row-gap.bgra.zlib`はartifact `12103297123`の`last-captured.bgra`を変更せずzlib圧縮した資料です。JSONは取得元run・commit・寸法・SHA-256・期待盤面と矩形を対応付けます。展開した画素は上から下へのrow順、paddingなし、1 pixelあたりB・G・R・Aの4 byteです。

`python -m unittest discover -s tests -p test_2048_gdi_row_gap.py -v`は実失敗画素を再解析し、hash一致と盤面・候補を確認します。`PYTHONPATH=src;.`を設定してリポジトリルートで実行してください。テストは画面取得や実入力を行いません。画像のゲーム描画元はGabriele Cirulli 2048のcommit `478b6ec346e3787f589e4af751378d06ded4cbbc`です。取得条件の詳細は同directoryのJSONに記録しています。

## 発光による実プレイ回帰（Issue #659）

`glow-256.*`と`glow-256-seed480.*`はsource `bb8648866b791c346d32e2068340ab0474b3563f` の隔離Chromium比較cohortから取得した未加工PNG/BGRAです。各JSONはhash・正しい矩形・視認した16セルと取得元を保持します。seed479の問題セルは、同画像の既知2と暗い字形maskが横1px整列で一致することを分類器と独立に検査します。

`glow-render/`の6組は固定版CSSを描画した256/512/1024/2048・scale/位置/overlayの資料です。`tools/render_2048_glow_fixture.py`で再生成できます。localStorageの状態とDOM boundsは生成時oracleだけで、認識器へ渡しません。`test_2048_glow.py`は外部ライブラリやブラウザーなしにzlibを展開し、画素・OCR bboxだけを入力します。これらの画像にも以下の2048 MIT表記を適用します。

## 生成元2048のライセンス

`gdi-internal-fragments.bgra.zlib`はpush run `39409678110`のartifact `12259118469`から取得した未加工画素です。対応JSONはhash、寸法、保存PNGから確認した16 cell、盤面矩形を記録します。短い欠落直後に現れる盤面内側の断片による3分割を再現し、同じ`test_2048_gdi_row_gap.py`で検査します。元のWGC timeoutやlease失効の原因を再現する資料ではありません。この画面の2048描画にも次のMIT表記を適用します。

The MIT License (MIT)

Copyright (c) 2014 Gabriele Cirulli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

`score-windows-headers/manifest.json` はPR #662のowned-window診断ログから復元した9件の得点headerを参照します。元PNGのchunk数・byte数・SHA256を検証し、640×240の画素だけをzlibへ保存しています。元frameは904×648、cropは(202,0,640,240)です。回帰ではこのheaderを元座標へ配置し、領域外を白で埋めます。完全な実frameやAIへのstate入力ではありません。期待得点は独立したUI視認に基づき、OCRの出力をラベルにしません。既存固定2048のMIT来歴を継承し、未知のゲームassetを追加しません。
