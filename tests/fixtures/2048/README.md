# 固定版2048の認識回帰資料

認識器の開発者は、`standard.json`の合成画面、`terminal-render/`の終了画面、`gdi-row-gap.*`のCI実失敗画素を使い、盤面認識を検査できます。`gdi-row-gap.bgra.zlib`はartifact `12103297123`の`last-captured.bgra`を変更せずzlib圧縮した資料です。JSONは取得元run・commit・寸法・SHA-256・期待盤面と矩形を対応付けます。展開した画素は上から下へのrow順、paddingなし、1 pixelあたりB・G・R・Aの4 byteです。

`python -m unittest discover -s tests -p test_2048_gdi_row_gap.py -v`は実失敗画素を再解析し、hash一致と盤面・候補を確認します。`PYTHONPATH=src;.`を設定してリポジトリルートで実行してください。テストは画面取得や実入力を行いません。画像のゲーム描画元はGabriele Cirulli 2048のcommit `478b6ec346e3787f589e4af751378d06ded4cbbc`です。取得条件の詳細は同directoryのJSONに記録しています。

## 生成元2048のライセンス

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
