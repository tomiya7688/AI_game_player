# 文字・ボタン付き2048終了画面の再生資料

このdirectoryは、認識器の開発者が終了画面の文字と再試行ボタンの影響を検査するための保存画素を収めます。生成元はGabriele Cirulliの2048、commit `478b6ec346e3787f589e4af751378d06ded4cbbc`です。`tools/render_2048_terminal_fixture.py`がhash検証済みの固定版ソースを変更せずに配信し、headlessブラウザの保存状態へ移動不能な4×4盤面と`over=true`を設定して描画しました。自然な操作で終了した画面でも、WindowsのHWND capture画像でもありません。

各`terminal-*`のPNGは目視確認用です。`.bgra.zlib`は上から下へのrow順、paddingなし、1 pixelあたりB・G・R・Aの4 byteをzlib圧縮した画素です。JSONは寸法、元のBGRAのSHA-256、生成条件、期待する盤面、生成時の認識結果を対応付けます。0.75倍・1倍ともCSS viewportは1180×850です。通常のunit testは保存画素から盤面・終了判定・候補0件を検査し、ブラウザやOCRを起動しません。

再生成はリポジトリルートで`PYTHONPATH=src;.`を設定し、Pillowとplaywright、EdgeまたはChromeを用意して、`python -m tools.render_2048_terminal_fixture --output-directory tests/fixtures/2048/terminal-render`を実行してください。同名のPNG・BGRA・JSONを上書きするため、既存差分を保護してから実行してください。生成後はPNGの終了文字と再試行ボタンを目視確認し、`python -m unittest discover -s tests -p test_2048_terminal_render.py -v`で再生結果を確認してください。過去のCI認識失敗の原因はこのfixtureだけでは特定できません。

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
