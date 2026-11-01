# 観測価値と下振れリスクを使うPlanning Policy

Issue #566は、探索が返す予測分布から、操作提案・追加観測・保留・追加判断要求を選ぶCoreです。
ゲームの操作や状態を埋め込まず、実入力・画面取得・Remote送信を発行しません。
探索本体は#565、本番Provider/実プレイ比較は#567、Unknownness Action Policy全体は#206です。

## 既存手法と今回の実装

| 手法・原資料 | 実装する範囲 | 実装しない範囲 |
|---|---|---|
| Rockafellar/Uryasevの[一般分布CVaR](https://sites.math.washington.edu/~rtr/papers/rtr187-CVaR2.pdf) | 利益の下側alpha質量を積分する。境界の離散atomも必要な割合だけ算入 | Portfolio最適化器、確率校正器 |
| [観測設計に依存するEVSI](https://journals.sagepub.com/doi/10.1177/0272989X15575286) | 有限world/action/signal表で厳密な期待値を列挙し、Q幅と未知worldを保守区間へ | 引用論文の回帰近似やnested Monte Carlo。有限表には不要 |
| 完全情報の価値EVPI | `E[max_a Q]-max_a E[Q]`の区間上界で高価な観測を除外 | 完全情報上界だけで再観測の利益を保証すること |
| Belief-state / chance分岐 | #565から絶対質量付きleaf値と仮説別Q区間を受ける | 仮説の平均値だけをCVaRへ入れてchanceの破局を消すこと |
| 有界メタ判断 | 回数・累積費用・episode経過時間、追加計算のmarginal upper gain/cost | #425の自動資源分類、#409の本番marginal gain推定器 |

この文書の数式は利益を大きくする符号です。損失を大きくする論文の上側CVaRと符号を取り違えません。
`alpha=0.05`は最悪5%の利益の平均です。95%の正常経路の平均ではありません。
値と費用は、同じevaluator id/versionに対応するnormalized planning value `[-1,1]`の単位を使います。
秒やtoken数をそのまま利益費用に加えません。

## chance内の稀な損失と未知質量

各操作の分布はBelief weight × chance probability × 仮想的な将来policyの分岐です。
仮説ごとの平均だけへ圧縮すると、98%で+1・2%で-1の操作が確定+0.96に変わり、
下振れリスクを失います。今回のriskはleaf分布の2%を保持して計算します。

Belief residual、未評価の分岐、traceの容量切詰めは、確率を既知leafへ再配分しません。
未知質量を利益下限-1へ置き、期待値上限だけは+1を使います。
このためtraceを切ると保守性が増す場合があります。未知を無害と判定するために閾値を緩めません。
catastrophic rateは指定した利益閾値以下の質量の上界であり、実世界の事故率やSafety証明ではありません。

画像confidence・NumericQuality confidence・相対scoreは、worldの校正確率にしません。
未校正のBeliefに対するglobal確率riskは未確定です。
仮説を条件としたchance分布と、未知worldも含めた全体のriskは別の意味を持ちます。

## EVPIとEVSI

仮説hの絶対質量をw、操作aのQ区間を[L,U]、未知world質量をrとします。
完全情報上界は次です。

```text
EVPI_upper = min(2, sum_h w_h max_a U_ha - max_a sum_h w_h L_ha + 2r)
```

これは情報を得た後に操作を変える最大余地の上界です。
実際のセンサがその情報を区別できるという意味ではありません。
risk-neutralに最大期待利益を選ぶ場合、この上界が観測費用以下なら追加観測は不要です。
実RiskPolicyはCVaR条件と混合順位を使うため、現在選択する操作の期待利益が
`max E[Q]`と一致しない場合があります。要求判断では、実際の現在提案の利益下限を
基準にした別の完全情報上界も計算し、それが費用以下ならearly exitします。
両者の基準を混ぜず、Traceの`information.evpi_upper_bound`と
`observation_policy_upper_bound`へ別々に残します。

実観測のEVSIには、校正参照を持つ有限表q(signal|h)を明示入力します。
各hについてsignal確率の和は1です。同じcontext/Belief版/evaluator版へ結び付けます。
完全に識別するセンサと、どのhでも同じsignal分布の無情報センサを区別します。

```text
pre_L/U = max_a sum_h w_h L/U_ha
post_L/U = sum_signal max_a sum_h w_h q(signal|h) L/U_ha
EVSI_lower = max(0, post_L - pre_U - 2r)
EVSI_upper = min(EVPI_upper, post_U - pre_L + 2r)
```

未知worldの情報利得を楽観的に0扱いしていません。rに対応する最大影響を区間へ残します。
Qが点値かつr=0のときだけ、このモデルのEVSIは点値になります。
qは現在worldについての観測channelで、将来chanceはworld条件付きで独立というモデル仮定です。
観測中にゲームが進む・操作が不可逆・遅延損失がある場合は費用とQへ反映するproducerが必要です。
その本番推定/校正は#405/#407/#409/#567へ追跡し、今回のAPIだけで導入済みとは扱いません。

## 観測後mappingのrisk再評価

risk-neutral EVSIが正でも、観測後に提案される高期待値操作がCVaR条件を満たすとは限りません。
要求前に各signal後のposterior chance分布へ同じsoft risk条件と順位を適用し、
実際に採択する操作の期待利益を現在の提案と比較します。
現在または観測後に保留する場合、保留の価値も未計測なら0と捏造せず区間[-1,1]です。
`withhold_value`をproducerが同じ価値単位で明示した場合だけ、その点値を使用します。
unknown worldの観測channelが不足する場合は、この改善を未確定とします。

さらに各posteriorでの合格だけでは不十分です。CVaRは再計画の前後で同じ判定になりません。
alpha=.05で、A=(-1を1%、+1を99%)もB=(+.6を100%)も下側CVaR=.6ですが、
両分布を半分ずつ混ぜるとCVaR=.44となります。最低値.5なら混合は不合格です。

このためsignal×world×選択操作×chanceを合成したcontingent policy全体にも、
同じglobal CVaRと破局質量条件を再適用します。全mappingの最適化は行いません。
計算したmappingが不合格、未知、期限切れなら、正の情報利益だけでOBSERVE_MOREへ進みません。
`observation_policy_gain_lower/upper`と`observation_contingent_risk`を監査結果に残します。
実現可能な改善下限が費用を上回り、mapping全体riskとepisode予算を満たす場合だけ要求します。

## 再観測の予算と実行境界

`PlanningDecisionEpisode`は一つの実行scopeとGoalを所有します。
新Frame・新snapshot・Belief版更新で作り直さず、同じインスタンスを使います。
最大要求回数、累積同価値単位費用、episode開始からの時間を全て確認します。
frame sequence・手番・時計が後退した場合や、同Frame別snapshotも拒否します。

予算resetは、明示された別scope/実行世代への移行か、最後のSELECTと一致する実行ackだけです。
実行ackは#564の`BeliefActionRef`を再利用します。trusted runtime adapterが実ExecutionReceiptから
投影する契約であり、その型だけで実行事実を証明できるわけではありません。
送信不明・別操作・旧context・ack再送は予算を復活させません。
ackしたFrameでは再度SELECTできず、次の実Frameを要求します。

SELECTは入力権限ではありません。現在のallowed候補とSafety hard vetoを先に適用し、
実行時にも既存Verifier/Safety/Stop検査を通します。
OBSERVE_MORE/ABSTAINを受けたProvider Adapterが、別Baselineへfallbackして入力を発行してはいけません。
ESCALATEは追加判断の要求であり、Remote Providerへの同意や送信許可ではありません。

## 配置・検証・未達

有限分布の集約とメタ判断を既存Python制御層へ配置します。OS/APIの近傍処理や画像反復ではなく、
bounded tupleの受け渡しで済むため、この段階ではC++/FFI境界を増やしません。
任意のcallbackを強制停止する仕組みはありません。入力は有限数値表で、期限を処理境界で検査します。
本番ownerによるscope・deadline・allowedの供給、観測channel校正、追加計算gain推定は#567/#405/#407/#409/#425です。

Coreの数値検証、risk/VOI、episode境界は単体/CPU結合試験で確認します。
実ゲームの勝率向上や全ゲーム適応の証拠とは区別します。実プレイ比較は#567で行います。

## CPU診断の再実行

```sh
PYTHONPATH=src:. python -m unittest discover -s tests -p 'test_planning_risk*.py' -v
PYTHONPATH=src:. python tools/benchmark_planning_policy.py --iterations 100 --seed 566 --output build/planning-policy/report.json
```

合成の校正済みworldと既知channelを使い、各診断sampleは独立したGoal episodeです。
本番でFrame毎にepisodeを再生成することを許可する例ではありません。
測定はepisode/policy構築と判断を含み、fixture生成を除外します。CPU-only、実入力なしです。
tracemalloc測定は通常latency測定と分け、instrumented診断には明示1秒処理予算を使います。
既定100msや実ゲームFPS性能の受入れ証拠ではありません。
reportにはGit head/dirty、全Planning source/fixture hash、seed、規模、設定、p50/p95、
CPU、heap retained/peak、dispositionと実際のcontingent riskを保存します。

主な回帰は、独立したFraction有理数Oracle100例、稀なchance損失、unknown、interval leaf、
一致/不一致/不明、EVPIとEVSI、risk-neutral利益と実採択利益の差、contingent CVaR反例、
hard veto/既存Verifier、Frame非reset、実行ack/再送、stale/scope/世代、deadline/破損です。
contingent global-risk gateを除去した隔離プロセスで回帰がOBSERVE_MOREとなり失敗することを確認しました。

2026-11-01 Linux/Python 3.12.14診断（seed566、各100反復）の例です。環境依存値でありFPS保証ではありません。

| world / action / 絶対leaf | p50 / p95 ms | 全反復CPU ms | traced peak byte |
|---|---:|---:|---:|
| 2 / 2 / 8 | 0.188 / 0.329 | 21.75 | 6,234 |
| 16 / 8 / 256 | 4.245 / 4.733 | 428.95 | 23,514 |
| 64 / 16 / 2048 | 18.171 / 18.572 | 1821.38 | 58,200 |
