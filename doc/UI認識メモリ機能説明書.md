# UI Detection / Recognition Memory

Issue #10 の初期実装は、未知ゲーム向けの汎用detectorと、プレイ済みゲーム向けの軽量Recognition Memoryを分離する。

## UI detector Provider

`UiDetectorProvider` は `ScreenFrame -> DetectedElement[]` の共通interfaceを持つ。`DetectorProviderAdapter` により既存の `BrightRegionDetector` や今後のOpenCV/segmentation detectorを同じ経路へ接続できる。

`UiDetectionPipeline` はまず `generic=False` の軽量Providerを実行する。既知UIが十分なconfidenceで得られた場合は重いgeneric Providerを呼ばず、未知/低confidence時だけgeneric Providerへfallbackする。複数Providerの結果はelement type、text、bbox IoUを使って統合し、sourceとconfidenceを `DetectedElement` に残す。Providerのload/runtime failureは個別statusとして隔離する。

## Recognition Memory

`UiRecognitionMemory` はゲーム固有sampleとゲーム横断prototypeをJSON内でも別配列として保存する。sampleは以下を分離して保持する。

- `visual_id`: UI矩形を4x4グリッドで量子化した軽量visual fingerprint
- `interaction_id`: click等の操作identity
- `transition_id`: 操作前後の画面遷移identity
- `sample_kind`: `positive` / `hard_negative`
- bbox、type、text、source、confidence

visual identityだけからinteraction/transitionの意味を確定しない。embeddingやMetric Learningは #11 およびデータ蓄積後の拡張とし、初期経路は標準ライブラリだけで決定論的に動作する。

`KnownUiDetector` は保存済みgame-specific sampleの正規化bboxを再確認し、visual fingerprintが一致したUIだけを軽量に再検出する。同一ゲームの既知画面ではgeneric detectorをskipできる。

## Cross-game prototype

`promote_cross_game()` は同じvisual fingerprint/typeが複数ゲームでpositive確認された場合だけ `UiPrototype` へ昇格する。1ゲームだけの知識を自動的に一般化しない。`CrossGamePrototypeDetector` は昇格済みprototypeのみを利用する。

HDBSCAN等によるPrototype Discoveryは毎frame実行せず、将来の低頻度/プレイ後整理層として追加する。クラスタは確定ラベルではなくprototype候補として扱う。

## 評価

`UiTransferEvaluator` はunseen gameでのprototype検出をprecision / recall / false positive / missedで評価する。`UiDetectionImpactEvaluator` はUI取り逃がし率とDecision/Executionの誤りを分けて記録できるため、mask精度だけでなく最終誤操作への影響を評価できる。
