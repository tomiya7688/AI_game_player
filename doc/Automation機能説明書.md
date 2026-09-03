# Automation候補検出

`ConfiguredRegionDetector` はゲーム固有の `automation.json` に定義された名前付き矩形を `ActionCandidate` へ変換する。矩形の中心をクリック候補とし、危険フラグ・信頼度・操作種別を引き継ぐ。画面外や重複などの最終的な拒否は `ActionEvaluator` が担当する。

将来の画像認識候補も同じ `ActionCandidate` 契約へ変換する。