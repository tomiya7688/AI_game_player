# UI埋め込み機能

`ui_embedding.py` はUI crop/regionを検索用特徴へ変換し、同一ゲーム再認識とゲーム横断prototype検索へ利用する基盤を提供する。

## 基本方針

Embeddingを1本の共通空間へ強制統合しない。`EmbeddingVector` は `lane / provider / model / version / preprocessing / dimension / confidence` を保持し、`visual`、`semantic`、将来の `interaction` 等を独立した検索空間として扱う。

異なる `model / version / preprocessing / dimension` のvectorはcosine比較しない。モデル交換時に旧vectorを誤比較しないことを優先する。

## Provider

`EmbeddingProvider` が共通interfaceである。初期の外部依存なしbaselineとして以下を持つ。

- `GridVisualEmbeddingProvider`: UI領域を小さなbrightness gridへ変換する軽量visual embedding。
- `HashedTextEmbeddingProvider`: OCR/textがある場合だけ生成する決定的semantic embedding。

これらは精度上限を決める固定モデルではない。CLIP系visual encoder、text encoder、Metric Learning済みモデル等へProvider単位で差し替えられる。

`EmbeddingPipeline` は複数Providerを同時利用できる。同じlaneのfallback Providerはprimary laneが空の場合のみ実行されるため、known UIでは軽量Provider、曖昧時だけ高精度Providerというcascadeへ拡張できる。

Provider障害は他Providerから分離し、`success / empty / skipped / disabled / error` をstatusとして保持する。

## Cache

`EmbeddingCache` はprovider metadata、UI bbox、text、crop bytesからexact cache keyを作る。同一crop/modelへの再推論を避ける。pathを指定した場合はJSONへ永続化する。

## Retrieval Memory

`EmbeddingMemory` はgame IDとUI identityごとにvectorを保存する。検索はまずexact cosine searchを使用し、以下を明示的に評価できる。

- same-game retrieval: 指定game内だけ検索
- cross-game retrieval: query元gameを除外して検索

データ量増加後はこの検索境界を保ったままANN/HNSW/FAISS系へ置換できる。

## #10 Prototype Memoryとの接続

`build_from_ui_prototypes()` は #10 の `UiPrototype` を受け取り、その `visual_id` と `source_games` に対応するEmbedding recordからmodel空間ごとのcentroidを生成する。

`EmbeddingPrototype` は以下を分離して保持する。

- `ui_prototype_id`: #10 Recognition Memory側prototype
- `identity_id`: visual identity
- embedding model metadata
- centroid vector
- source games
- centroidに最も近いrepresentative sample

cross-game prototypeは複数model空間を混ぜず、各空間ごとに別centroidを持つ。

## 評価

`EmbeddingRetrievalEvaluator` は `accuracy_at_1` と `recall_at_k` をsame-game / cross-gameの両方で算出する。一般画像benchmarkだけでなく、UI再認識・未知ゲームtransferへの寄与を直接評価するための最小指標である。

## 将来拡張

Metric Learning、Contrastive Learning、PCA/projection、Matryoshka表現、Product Quantization、ANN、cross-modal alignmentはProviderまたは検索backendとして追加する。学習結果を確定ラベルやAction価値へ直結させず、Recognition / Retrieval evidenceとして扱う。
