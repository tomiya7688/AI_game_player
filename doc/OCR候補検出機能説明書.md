# OCR候補検出

`OcrTextCandidateDetector` はOCRエンジンの出力（文字列と矩形）を、認識共通中間表現 `DetectedElement` へ変換する。`text`、`bbox`、`source`、`confidence`、操作種別・危険フラグを保持し、この段階では実行用 `ActionCandidate` を確定しない。`CandidateMerger` がAutomation候補や画像由来Elementと統合した後に `ActionCandidate` を生成する。OCR処理そのものは担当しないため、Tesseractや各種OCR Providerを後から接続できる。