# Runtime Evaluation (#491)

元#8の6軸 PrimitiveEvaluator を実Sessionへ接続する作業。
現在の共有契約 `evaluation_quality.NumericQuality` は標準Pythonだけを使う不変値である。
value/confidence/available、projection/profile版、最大8件のopaque provenanceを保持する。
coverageと欠損軸を別に保持し、confidenceは全weight分母の支持率としてcoverageを超えない。
`bounds` はeffective_value±(1-confidence)の未知質量envelopeで、統計CIやchance確率ではない。
未計測はvalue=0/confidence=0/available=False。計測された中立値0とは区別する。
`effective_value` は支持率で縮小したsoft寄与であり、confidenceを成功確率と称さない。
plannerのhypothetical leafとactual receiptのどちらかは、品質数値の外側のprojectionで区別する。

RuntimeEvaluationAdapterの本番接続、6軸availability、profile選択、監査/overhead試験は
このIssueの後続差分で実装する。共有値だけをもってRuntime/Real-play完了とは扱わない。
