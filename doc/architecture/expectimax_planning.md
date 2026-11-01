# Belief-aware bounded Expectimax

Issue #565（親 #480 / #478）は、typed immutable Beliefから複数worldとchanceを評価する汎用Coreと、停止可能な制限workerを提供します。状態型・遷移・合法操作・数値qualityはtrustedな登録Adapterから注入します。汎用Coreは盤面寸法、tile、カード規則、DOM、実入力を知りません。本番DecisionProviderへの配線と実プレイ比較は #567、lower-tail risk / VOIは #566 の責務です。

## 探索するpolicyと観測の仮定

`ExpectimaxSearch.deepen()` は深さ1から反復深化し、全root候補の評価が完成した深さだけを公開します。正の支持を持つ全worldの合法集合と、呼出元が検証したallowed IDsの共通集合を使います。確率0worldで候補を消しません。chance nodeは検証済み確率で積分し、max nodeは同じquality単位で比較します。request内LRU transposition cacheはcanonical state digestと残り深さ、または共通操作列をkeyにし、件数とlogical byte数を制限します。

既定は深さ1です。将来状態の完全観測を保証できないdomainの深さ2以上は、全worldとchance分岐に共通する有限操作列を列挙するopen-loop MPCです。最初の一手だけを返し、次の観測で計算し直します。これは一般POMDPの最適な観測依存policy treeではありません。共通列が存在しない深さや未完了の深さは捨て、完成済みの浅い結果へ戻ります。

`PlanningRegistration.future_state_observable` と `PlanningDomain.future_state_observable` の両方を明示して一致させた場合だけ、将来の状態ごとに異なるmaxを選ぶ通常のExpectimaxを許可します。規則がexactであるだけでは、将来の知覚が完全という保証にはなりません。結果の `continuation_semantics` は `one_step` / `open_loop_mpc` / `state_observed_expectimax` を区別します。

不完全情報の数値oracleでは、同確率のhidden world 2個に対し、probe後の最適操作が逆になります。安全なroot操作は常に0.3、probe後に共通の将来操作を固定すると期待値0です。未観測worldごとに将来maxを許すとprobeを1.0と誤って評価します。テストは通常domainでsafe、完全将来観測を保証した別domainでprobeを選ぶことを確認します。POMDPで潜在state別maxをそのまま実行可能policyと扱わないための反例です。

深い探索でrootごとに選んだ共通tailは固定policyの比較対象です。その後の観測でtail全体を再最適化する一般的な情報価値を、この固定menuだけのEVPI/EVSIで証明しません（#566 / #567）。

## 数値・確率・残余

`NumericQuality` は注入した評価式の正規化値[-1,1]と支持不足を表し、勝率・world確率・統計的信頼区間ではありません。missing axes / confidence不足は各leafの保守的quality区間へ残します。modelのraw reward deltaを、単位の異なるqualityへ勝手に加算しません。

校正済みworldでは、belief weight × chance probabilityをabsolute leaf質量として返し、残余world質量は[-1,1]へ残します。未校正の比較scoreでは、仮説条件付きchance分布だけを返し、global expected qualityはNone、global区間[-1,1]、global unknown1を維持します。既定は非選択で、明示的な実験opt-inだけが条件付き評価による選択を許します。pixel recognition confidenceをworld確率1へ置き換えません。

leaf traceは同値を合算し、bin上限に達すると近い値を質量加重平均と支持区間の包絡へ圧縮します。質量と点期待値を保持し、下側tailを一点へ潰しません。たとえば98%の+1と2%の-1を1binへ圧縮すると、値0.96・質量1・区間[-1,1]です。#566はこの区間で保守的riskを計算できます。区間が広いほど選択できなくなる限界があり、圧縮はrisk精度向上の証明ではありません。

## 資源・停止・期限

| 制限 | 実装と保証範囲 |
|---|---|
| 深さ・node | 深さ上限と全反復累積node訪問数。node上限前に未完了深さを打切り |
| state・分岐・trace・cache | typed stateのcanonical byte、chance件数、bin件数、LRU件数/serialization byteを検証。Python heap全体の量と混同しない |
| IPC | 所有一時directoryのatomic JSON。読取max+1、出力byte上限と文字列/escape対応の深さ32事前検査、重複key・NaN・不正field・質量・巨大整数を拒否。pickleを使わない |
| worker memory | LinuxはRLIMIT_AS、WindowsはJobのprocess/job private commit上限。registered factory / codec / callbackをimport・呼出す前に適用。失敗・未対応platformは探索しない |
| 計算時間 | 親のmonotonic deadlineで受領を打切り、所有workerをTERM→KILL。子にも相対時計の協調deadlineとLinux CPU backstop。遅いcallbackも親は待ち続けない |
| Stop・stale | 既存RunController世代、current Belief context/version、元観測TTLを監視し、Stop/rearm・別世代・古い結果を棄却。短い最終CAS内ではIOやmodelを呼ばない |

固定Python bootstrap / entrypoint importはOS memory guardより先です。registered model importとstate codec以降が制限対象です。Linux ASとWindows private commitは同じ指標ではありません。Windowsの実適用はローカルLinuxではUnverifiedであり、対応CIで検証します。これは任意Pythonのsecurity sandboxではありません。登録先はtrustedコードのみ、純粋・有限・決定的で、process起動・ネットワーク・GPU・外部入力・DOMへの副作用を禁止する契約です。親のcurrent snapshot getterは高速なin-memory metadata読取を要求します。

`compute_cutoff_at_ns` は親が完全深さreportを受領すべき最終時刻、`created_at_ns` は親の受領時刻、`expires_at_ns` は元観測の失効時刻です。子の独立clock originを親の観測期限と比較しません。期限前に受領した完全深さは、計算打切り後でも観測がfreshなら利用できます。遅いreport、Stop、世代違いは利用しません。atomic最終markerの読取raceでは最後のcheckpointを再読取します。

process起動・OS scheduling・終了回収は無限精度のwall-time上限ではありません。TERM/KILL後の回収待ちは各100ms、回収不能ならそのplannerをpoisonして新workerを起動しません。予算はcallbackを含む探索の受領期限とworker資源境界であり、アプリ全体のRSSや厳密な関数return時刻の保証とは区別します。Stopが任意callbackの終了まで待つ設計にはしません。

## 任意参照Adapterと接続範囲

`planning_2048.py` だけが既存exact 2048 modelを使います。明示的な参照heuristicは空セル0.4、単調性0.2、滑らかさ0.1、最大tile進捗0.15、最大tile角配置0.15です。これはゲーム専用の任意profileで、汎用Coreのルールでも勝率校正式でもありません。8/16の曖昧fixtureと、盤面を持たないint/card相当finite-domain oracleを同じCoreで検査します。

| 指定手法 | 実装・証拠 | 接続・未達 |
|---|---|---|
| Expectimax / max-chance | full-observation保証時のmax、chance積分、Fraction oracle | learned world model / stochastic calibrationは未達 |
| Belief-aware search | 複数typed world、共通合法Action、unknown、score条件付き欄 | Recognition校正は#405 / #407、Runtimeは#567 |
| iterative deepening / transposition | 完全深さcheckpoint、累積node、有限LRU、最終marker race回帰 | persistent worker / Native hot path最適化は後続 |
| bounded planning / finite-horizon MPC | 共通open-loop tail、観測仮定反例、OS worker kill / memory / Stop / expiry試験 | POMCP / policy-tree探索ではない。包括的budget制御は#425 |
| normalized quality / risk | #491共有型の区間を保持、bin rare-catastrophe回帰 | 6軸Runtime投影は#491、CVaR / VOIは#566 |
| 実プレイ | #565はCPU Coreと制限workerまで | 本番Provider・fresh first-action・Baseline比較は#567。FPS成立や汎用攻略強化の証拠ではない |

Pythonを選んだのは既存typed Belief / Transition / injected evaluatorを粗いprocess境界で接続するためです。C++化を先に増やさず、cold spawnとserialization込みの費用を測り、FPSが必要な経路は測定後にpersistent worker / Nativeへ移す判断材料にします。

## 再現と参考手法

`PYTHONPATH=src:. python tools/benchmark_expectimax.py --iterations 20 --warmup 2 --seed 565 --depth 3 --nodes 5000 --wall-ms 250` はCPU-only intと曖昧2048を測定し、Git SHA・dirty・全関連source SHA256・fixture hash・環境・予算を固有reportへ記録します。親のcold spawn込みp50/p95、採用checkpointまでの子経過、child CPU/RSS high-water、親tracemalloc、候補/分岐/trace/cache/bytes/unknownを区別します。強制終了した未完了深さの作業量はcheckpoint countersに含まれないため、総探索量の上限測定とは呼びません。DOM・実入力・攻略成績を含みません。

- [Model Predictive Control](https://escholarship.org/uc/item/1wt3d4vr): 有限horizonを解き最初の操作後に再計画する設計と、今回の共通open-loop tailの位置付け。
- [Who Needs POMDPs?](https://www.pomdp.org/talks/who-needs-pomdps/index.html): 部分観測でstateとbeliefを区別する背景。今回のCoreは完全なPOMDP solverではありません。
- Expectimaxのmax/chance再帰、反復深化、transposition memoizationは古典的探索手法です。今回の置換・近似は、部分観測時の共通tail制限と保守的interval-bin traceとして上表・反例へ明示しています。

Linux x86_64 / Python 3.12.14、seed565、warmup2＋各20回、depth3 / nodes5000 / 250ms / worker256MiBでの測定例です（2026-11-01、親Git SHA `6ee4c6e` の変更中source SHA256をreportへ記録）。測定中のsource変更はありません。

| CPU fixture | cold spawn込みp50 / p95 | 採用checkpoint子経過p50 / p95 | 完成深さ | child累積RSS high-water | 親tracemalloc peak |
|---|---|---|---|---|---|
| int | 80.08 / 81.89ms | 6.55 / 7.17ms | 3が20/20 | 17.0MiB | 1.18MiB |
| 曖昧8/16 2048 | 251.89 / 251.94ms | 164.90 / 169.98ms | 2が19/20、1が1/20 | 17.57MiB | 0.93MiB |

採用checkpointの最大node / chance branchはint208 / 120、2048は3706 / 3543、trace上限32、最大resultは43983bytesです。両fixtureとも元unknown0.1を保持します。2048は計算deadlineで未完了depth3を捨て、期限前受領のdepth2（1回はdepth1）を採用しました。250msは関数returnの厳密上限ではなく、回収込み最大251.99msでした。cold spawnが支配的で、FPSに必要な低遅延や実ゲームの攻略成績をこのCPU測定で証明していません。完全な設定・source hash・固有run IDはローカルの `build/expectimax/report.json` と固有archiveへ出力されます（生成物はcommit対象外）。
