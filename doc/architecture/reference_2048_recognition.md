# 固定版2048の発光・得点認識回帰（Issue #659）

この修正は任意の参照2048 Adapterの画像認識に限定する。Minecraft・カードゲーム・FPS等の汎用Planner、Belief、Verifier、Safetyに盤面固有の規則を加えない。入力は同一frameのBGRAとOCR矩形で、DOM・保存状態・予測盤面で未知セルを埋めない。

## 実画像の失敗と正解根拠

測定source `bb8648866b791c346d32e2068340ab0474b3563f`、失敗cohort `run-3de18f03ecfd4897b4b8edc87d08c2cc` は保持する。seed479は135送信後、seed480は131送信後の256発光で停止した。修正前はROIがそれぞれ `(64,274,511,511)` と `(75,274,511,511)` へ拡大し、セルがNoneになった。画面上の正しい盤面は両方とも `(75,274,500,500)`。

`tests/fixtures/2048/glow-256*` は元PNGと未加工BGRAのzlib・寸法・SHA-256・視認した16セルを保存する。seed479のrow2/col0は空ではなく2である。同じ画像の既知2と暗い字形画素が双方537個、横1px整列後の対称差0で一致することも、分類器と独立に検証する。期待値を分類器自身の出力から作らない。

## 手法と実装・接続

| 要求・手法 | 実装・接続 | 検証と範囲 |
|---|---|---|
| 局所外れ値に頑健な矩形推定 | `game_2048_board._robust_board_bounds` が行の左右端の中央値を使う。盤面幅の過半数を覆い、少なくとも片辺が既存の余白許容内で一致する行で上下端を推定する | min/maxの局所発光による包絡拡大を防ぐ。正方形・密度の既存閾値は維持 |
| 色と幾何の整合 | 既存palette、色距離、行gap、cellの4小patch照合を維持。領域外の行・行幅を密度に加算しない | seed/手数条件や未知色の近似割当を追加しない。未知セルがあるAdapterは候補を出さない |
| OCR layout association | `game_2048_adapter._read_score` がSCOREラベルと水平に重なり、直下にある数値を対応付ける。ラベル/数字の低い方のconfidenceを採用 | BESTや見出しを得点にしない。異なる複数数値はunknown。位置なしは同一文字列の明示SCORE数値だけを採用 |
| 同frame evidenceの保全 | `BrowserScreenSource` がOCR wordのbbox/confidenceを`ocr_candidates`に保持 | 比較probeも本番FrameAnalyzerと同じ証拠形式をAdapterへ渡す。OCR未実行のscoreはunknown |
| 再現画像と負例 | 元失敗2枚、固定CSSの256/512/1024/2048、0.75/1/1.25倍、四隅、overlay、未知色・曖昧scoreを回帰化 | 合成状態・DOM boundsはfixture生成時の独立oracleだけ。認識器入力は画素のみ |

中央値による端点推定は局所汚染に対する頑健統計、過半数幅と辺の照合は幾何の支持検査である。単一の最外画素を矩形へ採用しない。画像の大半が未知・遮蔽された場合の復元を保証せず、Noneを保持する。色距離・認識confidenceの閾値緩和や、以前の盤面による補完は行わない。

## 費用と再現手順

既存のPython認識境界へ数百行分の端点統計を加える。既存の全画素走査はO(W×H)、追加処理はO(H log H)時間/O(H)補助領域で、新たなフル画像コピー、FFI、依存packageはない。既存Native境界を細粒度呼出しに分割しない。

650×850元画像を10回warm replayした同一Linux環境の参考値は、旧中央値111.25ms・新110.69ms、Python peak allocation 73,352→73,472byte。既存frame bufferとブラウザー資源は含めず、Windows latencyや一般ゲームの性能保証ではない。

```bash
PYTHONPATH=src:.:tests python -m unittest test_2048_glow test_2048_gdi_row_gap test_2048_terminal_render -v
PYTHONPATH=src:. python tools/render_2048_glow_fixture.py --output build/glow-fixtures
```

画像生成はPillow/Playwrightと隔離Chromiumを使う明示的な開発操作で、通常runtimeの依存へ追加しない。生成元は既存lockのGabriele Cirulli 2048、ライセンス表記は`tests/fixtures/2048/README.md`を参照する。

**Core:** 元画像の修正前FAIL、修正後PASS、未知色と曖昧scoreの拒否を検査する。**Runtime:** 既存Adapterと比較probeへ接続し、入力許可・Stopを維持する。**Real-play:** 修正版の独立head/cohortでseed479/480の135/131手以降を再試験する。旧失敗を成功へ読み替えない。Windows WGC/Win32や全ゲームの認識・攻略精度はこの修正の受入れではなく、#30/#96/#480等の未達として残す。
