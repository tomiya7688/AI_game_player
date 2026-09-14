# OCR候補検出

`OcrTextCandidateDetector` はOCRエンジンの出力（文字列と矩形）を、認識共通中間表現 `DetectedElement` へ変換する。`text`、`bbox`、`source`、`confidence`、操作種別・危険フラグを保持し、この段階では実行用 `ActionCandidate` を確定しない。`CandidateMerger` がAutomation候補や画像由来Elementと統合した後に `ActionCandidate` を生成する。

## OCR Provider / Fusion

`OcrProvider` は `ScreenFrame` から `OcrResult[]` を返す共通契約である。`OcrResult` は文字列・bbox・confidenceに加えて `source`、`model`、`preprocessing`、`frame_id` を保持し、モデルや前処理を交換・併用してもprovenanceを失わない。

既存の `TesseractOcrRecognizer` などdict形式Recognizerは `RecognizerOcrProvider` でProvider契約へ接続できるため、現在の軽量OCR経路を維持したまま将来PP-OCR系やnumeric specialist等を追加できる。

`OcrFusionPipeline` は以下を担当する。

- 複数Providerを同一frameで実行する
- Providerごとのweightをconfidenceへ反映する
- 正規化text + bbox IoUが一致するEvidenceを統合する
- 直近frameで同じEvidenceが継続した場合は小さなtemporal consistency bonusを加える
- primary結果が無い、または低confidenceの場合だけfallback Providerを実行する
- Provider単独の例外・load failureをstatusへ記録し、他Providerを継続する
- disabled / skipped / success / errorをProvider単位で追跡する

`FrameAnalyzer` はFusion利用時、`ocr_candidates` に統合結果、`ocr_results` に生のProvider結果、`ocr_provider_status` にProvider状態、`ocr_fallback_used` にfallback実行有無を保存する。Fusion済みOCRは `detected_elements` にも追加される。

`OcrDecisionImpactEvaluator` は文字認識誤りそのものと、最終Decision誤り・Execution誤りを分けて記録する。これによりOCR benchmark精度だけでなく、誤認識が実際の誤Decision / 誤操作へ伝播したかを評価できる。

モデル名や標準Provider順位は固定仕様にせず、実ゲーム精度・latency・hardwareで交換可能とする。