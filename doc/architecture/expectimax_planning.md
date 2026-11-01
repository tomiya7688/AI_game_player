# Belief-aware Expectimax の共有契約

Issue #565（親 #480）の段階実装です。このcheckpointは後続 #566 / #567 が共有する型、予算、数値意味、JSON境界を提供します。探索Coreと制限process本体は #565 で継続実装し、Runtime / Real-playの完了と区別します。

`planning_contract.py` は trusted local factory の登録、ゲーム非依存の model / codec / legal actions / NumericQuality evaluator と有限予算を宣言します。4x4盤面はCoreへ埋め込みません。OSメモリ制限を適用したworkerでcallbackを実行する構造を予定し、現在の宣言だけをhard resource制御の実装済み証拠にはしません。

`planning_result.py` は source BeliefContext / 仮説版 / allowed IDs / Run世代 / 数値の版を結果へ束縛します。`WeightedValue` はchance/world質量とquality区間を別に保持します。#491のNumericQualityが表す支持不足をworld確率へ変換しません。未校正worldのglobal expected qualityはNone、区間[-1,1]、unknown1とし、仮説条件付きのchance評価だけを別欄に保持できます。

`compute_cutoff_at_ns` は親が完全深さreportを受領すべき最終時刻、`created_at_ns` は親の受領時刻、`expires_at_ns` は元観測の失効時刻です。計算deadlineと観測の鮮度を区別します。後続AdapterはStop・source世代・仮説版・許可候補を実行直前にも再検証し、最初の一手だけを既存Verifier/Safetyへ渡します。

`planning_wire.py` は既存atomic JSONを使い、有界message、重複key、NaN、不正fieldを検証します。JSONを使っても任意factoryを安全に実行できるsandboxになるとは扱いません。import先は信頼された登録コードで定義し、画面・Model応答の文字列から作りません。

Coreの数値・質量・serialization契約はCPU unit test、qualityのshared型は #491、リスク・VOIは #566、本番Providerと比較は #567 の成果物として分けます。ゲームの認識精度や攻略成績は、このcheckpointでは未検証です。
