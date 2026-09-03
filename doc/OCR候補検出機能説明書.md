# OCR候補検出

`OcrTextCandidateDetector` はOCRエンジンの出力（文字列と矩形）を `ActionCandidate` へ変換する。文字領域の中心を候補座標とし、信頼度・危険フラグ・操作種別を引き継ぐ。OCR処理そのものは担当しないため、Tesseractや各種クラウドOCRを後から接続できる。