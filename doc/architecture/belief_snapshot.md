# 型付き BeliefSnapshot と有界候補 Store

Issue #478 の子 #563 が実装する **Versioned Bounded BeliefSnapshot Contract + Candidate Dedup/Residual Mass** の契約です。複数の plausible world を保持し、予測を観測事実へ変換しません。統合版では#564の[有限仮説のBayesian更新Core](belief_update.md)がこの契約を使います。particle filterへ交換可能な境界と、resampling本体の実装は区別します。

## 実装とデータ境界

- `belief_contract.py`: 同じ Frame / turn / session / action epoch / source generation / clock の context、証拠参照、鮮度、重みの小さな不変宣言。
- `world_state_hypothesis.py`: `TypedStateRef[T]` と `WorldStateHypothesis[T]`。ゲーム状態の型と canonical identity を固定。
- `belief_snapshot.py`: 候補集合の版、残余 unknown、再観測要求、診断を保持する不変 Snapshot。
- `belief_candidate_store.py`: 一つの集合だけを保持し、型・scope・質量・TTL・入力数・K・byte・時間予算を検証して置換。
- `belief_validation.py`: ID / 数値 / 不変性 / 循環 / 深さ / node / scalar byte の検証。
- `config/belief_snapshot.schema.json`: JSON Schema 2020-12、契約版1。`config/belief_store.default.json` は既定予算。

Core は盤面寸法、tile、カード、座標の意味を持ちません。型付き state にはゲーム上の抽象状態だけを入れます。raw frame、画像、credential、利用者の秘密 metadata、自由文のモデル応答は入れません。証拠は opaque ID、recognizer / model の版、Frame / Snapshot の参照だけを持ちます。

`TypedStateRef[T](game_id, state_schema_id, state_type, state)` は state の型の完全一致を要求し、frozen dataclass、tuple、frozenset、有限 scalar などを再帰検証します。frozen dataclass の中の list / dict、NaN / inf、循環、深さ16超、8192 node超、2048要素超のcontainer、256 bit超の整数、累積scalar / canonical state 64 KiB超を拒否します。serialize前にも予算を課します。decoratorを持たない可変subclassや、dataclass fields外へ追加したstateも拒否します。mutableな別名を残す state は受理しません。これは同じprocess内の信頼された Adapter 向け契約で、任意pluginを隔離するsecurity sandboxではありません。

canonical typed JSON は型tagとrecord名を保持し、game / schema とともに SHA-256 identity を作ります。異なる型やschemaの状態を同じ world として統合しません。JSON投影は診断・Journal向けで、型名から任意importやobject生成は行いません。復元には利用側の明示的な型付き Adapter が必要です。

## 重み・残余・dedup

`BeliefWeight` は次を区別します。

| 種別 | 値 | 確率としての扱い |
|---|---|---|
| CALIBRATED_PROBABILITY | 0..1、外部 `calibration_id` 必須 | 候補と残余の和1。参照する分布・校正の意味は Adapter が保証 |
| RELATIVE_SCORE | finite・非負。校正参照なし | 順位だけに使用。`residual_unknown_mass=1` を維持 |
| UNKNOWN | Snapshotの空集合に使用 | 候補なし、unknown1、再観測要求 |

Core は校正器を学習しません。`calibration_id` は外部の校正 / 分布契約の版の参照です。異なる校正版・重み種別・state schema・origin を同じ入力分布へ混ぜることを拒否します。

`publish(context, candidates, unknown_mass=..., deadline_ns=...)` は **一つの入力分布を置換**します。前 Snapshot の票を加算しません。

1. 同じ `hypothesis_id` の完全一致再送だけを除去します。同ID異内容は拒否。
2. 校正済み候補では、再送除去後の入力確率和 + 明示unknownが1を超えれば拒否します。
3. 異なるIDが同じ canonical world を指す場合は最大重みの候補を残します。残した候補の完全な provenance を保持し、他の重みや証拠を独立票として加算しません。
4. TTL、zero weight、Top-K、Snapshot byte予算で除外した後も、確率は元の絶対値を保持します。`unknown=1−保持した確率和`。切り捨てた確率質量は unknown に残ります。
5. score の順位表示 `ranking_weights` は数値overflowを避けて和1にしますが、元scoreとunknown1を変更しません。

例えば同worldの別IDが0.3 / 0.4、元unknown0.3なら、Storeは0.4を保持してunknown0.6にします。**互いに排他的なTransition経路が同じworldへ到達する場合**は、#564がその根拠を確認して0.7へ合法に合算した一候補をStoreへ渡せます。Storeのmax dedupをBayesianな経路合算の代わりに使いません。

`evidence_id` の同一参照は除去し、同IDで別内容の証拠は拒否します。異なるrecognizerが同じFrame由来なら同じ `correlation_id` を使い、診断では同Snapshot内のgroupを一つとして数えます。Storeは独立性を仮定した尤度乗算を行いません。相関Frameのfusion自体は #405、posteriorの計算は #564 の責務です。

校正済み分布だけに `entropy_lower_bound_bits` を定義します。unknownを一bucketへまとめた粗いentropyの下限です。粒子数の有効標本数として解釈せず、未校正scoreのentropyやESSは作りません。空候補、score、unknownが半分以上、未観測predictionでは `reobserve_requested=True` を返し、hostがabstain / 再観測を選べます。Store自身はActionを決めません。

## 予測と実観測

`HypothesisOrigin` は `OBSERVATION_HYPOTHESIS / PREDICTION / POSTERIOR_HYPOTHESIS` です。どの値も観測事実そのものを表しません。observation / posteriorには実Frameを参照する observation evidence が必要です。predictionには prediction evidence が必要で、観測済みtarget Frameを付けられません。

#564のPredict段階は新Frameがまだなければpriorのcontextを保持できます。同一contextのままoriginを変更して集合を置換することは可能です。新Frameのposteriorでは新しいFrame sequence / Snapshot ID / contextを使用します。異なる時点のprior・Action・Outcomeは `prior_action_ref / predicted_outcome_ref` のopaque参照へ結び、旧Frameの証拠を新contextへ付け替えません。

`BeliefFreshness.observed_at_ns` と evidence の `captured_at_ns` は元証拠の時刻です。predictionのみで新しい観測時刻へ更新しません。予測計算の生成時刻は Snapshot の `created_at_ns` です。prediction evidence にはpriorの元時刻を保持します。

## scope・鮮度・並行更新

contextは game / scene / session / snapshot / frame IDs（最大16）/ frame sequence / turn / action epoch / source ID・generation / clock IDを持ちます。候補と全証拠はそのcontextに完全一致する必要があります。

- 同じrunで Frame sequence / turn / action epoch が後退する入力を拒否します。
- 同じFrame sequenceへ別Snapshotを付けること、新Frameへ旧Snapshot IDを再使用することを拒否します。
- game / scene / session / source generationが変わる場合、`reset(new_context)` で旧集合を空unknownへ破棄します。reset後も履歴を溜めません。
- `clock_id` は同じmonotonic時計originを共有するprocess / producerが明示して渡します。別processのmonotonic数値は比較できません。新processでは新clock IDのStoreを作り、旧Snapshotの時刻を再利用しません。
- future evidence、時計の後退を拒否します。候補期限だけ延ばして古い証拠を新鮮にできず、全証拠ageと複数Frameの時間幅を検査します。
- `read(context)` は完全一致contextかつ未失効の集合だけを返し、stale / 別Frameは `None` です。

`publish(..., expected_version=prior.hypothesis_set_version)` は計算元priorの版を開始時に照合します。空Storeは版0です。指定しない既存の呼出しも利用できます。同contextでも新しい集合が既に公開されていれば、古いpriorに基づく更新は `stale_expected_version` で拒否します。

更新はStore lock外で計算し、最終版CASで公開します。計算中にresetや別publishが完了した場合、古い更新は `concurrent_snapshot_update` で拒否します。不正入力・producer例外・timeoutでは前版を変更しません。hostは失敗した更新を成功として扱わず、必要なら空unknownをpublish / 再観測します。新contextに対するreadは旧contextを返しません。

## 既定予算とコスト

| 予算 | 既定 |
|---|---:|
| 保存候補K / 一更新入力上限 | 32 / 256 |
| canonical Snapshot / 単体state bytes | 262144 / 16384 |
| 元証拠TTL / 複数Frameの時間幅 | 1秒 / 250ms |
| 更新deadline | 100ms |
| 証拠参照 / Frame参照 | 1候補16 / context16 |

候補入力は上限+1件まで読み、無限に候補を返すiteratorも拒否します。deadlineは同じclockで各段階と公開前に確認します。入力iteratorや時計callback自体が戻らない場合を強制中断するthreadは作りません。hostはboundedで信頼されたproducerを使い、重い認識・simulation・IOをStoreの外へ分離します。Storeは履歴・raw frame・OS resource probe・LLM・入力Executorを持ちません。

CPU測定は次で再実行できます。`tracemalloc` を有効にし、fake Adapterの候補構築・不変性検査・canonical化・Store publishを含めます。

```bash
PYTHONPATH=src:. python tools/benchmark_belief_store.py --iterations 100 --candidates 16 --capacity 8 --output build/performance/belief-store.json
PYTHONPATH=src:. python tools/benchmark_belief_store.py --iterations 100 --candidates 64 --capacity 32 --output build/performance/belief-store-64.json
```

2026-11-01 Linux cloud / CPU、各100更新の測定:

| 入力 / K | update p50 / p95 | CPU合計 | traced peak | peak保存候補 / Snapshot bytes |
|---|---:|---:|---:|---:|
| 16 / 8 | 4.98 / 5.31ms | 637.1ms | 585097B | 8 / 10741B |
| 64 / 32 | 18.52 / 19.65ms | 2433.5ms | 701303B | 32 / 40814B |

測定値はこのfixtureと環境の結果です。重いゲーム状態・認識器・Transition計算や実プレイのlatencyを保証しません。Pythonの既存typed state / high-level reasoningへまとめて渡す契約として実装し、画素処理・OS入力・Safety hot pathには配置しません。Native移管を判断する際もこの全体コストと境界コピーを比較します。

## Method Fidelity Matrix と接続範囲

| #478 / #563 の指定 | 実装・証拠 | 未達・後続 |
|---|---|---|
| POMDP beliefの複数plausible world | typed Hypothesis / Snapshot、8/16曖昧2048とcard / position fixture | 統合版#564のCoreで実装。本番の観測尤度と接続は#405/#407/#567 |
| particle / weighted approximationの境界 | 有限weight付き状態と未知質量、origin / refs | 元Issue#564はParticle Filterへ交換可能な境界が要件。参照実装は有限全列挙で、resampling/POMCP/POMCPOW本体は実装対象外 |
| 比較scoreと校正確率の区別 | weight kind + calibration ref、scoreではunknown1 | #564は校正済みjoint尤度を消費。実Recognizerの尤度校正は#405/#407 |
| dedup / Top-K / 残余 | 厳密再送 / world max / K・byte drop、絶対確率保持 | 統合版#564のCoreで合算・矛盾時unknownを実装。本番接続は#567 |
| scope / TTL / source整合 | 混在・stale・future・deadline・時計origin・reset CAS negative test | 本番 #405/#407 evidence Adapterは未接続 |
| bounded memory / cost | 300回churn・12回restart、上限iterator、2規模benchmark | 実ゲーム状態での長時間Runtime計測は後続 |
| Safety / Verifier維持 | 高確信Belief payloadがあっても既存allowed IDs・Verifierがunsafe Actionを拒否するtest | Planner / OS入力の接続は対象外 |

**Core:** このPRの型付き不変契約・Candidate Store・Schema・診断を実装済み。

**Runtime:** #479 の実在 `Game2048State` を任意fixture Adapterが使い、DecisionContext / Verifierの既存APIとCPU結合テスト済み。本番Recognition / Decision / Journalへ自動接続しません。Snapshotの `to_dict/to_bytes` を明示的にJournalへ投影するhostの配線、#405/#407 evidence、#567のtrusted Runtime Adapterは未達です。#564 Estimatorは統合版のCoreとして追加されていますが、既存Outcomeや実行受領記録を置き換えません。RegistryやTransitionModelの登録は必須にしません。

**Real-play:** このPRでは実施していません。fixtureの内部盤面は合成認識データで、実ゲームDOMや内部状態を認識器の正解として使いません。ゲーム上での仮説更新・操作の改善や勝率の受入れ完了にはしません。

研究参照は親Issueの指定に従います: [POMDP belief](https://www.pomdp.org/talks/who-needs-pomdps/index.html)、[POMCP](https://papers.nips.cc/paper_files/paper/2010/hash/edfbe1afcf9246bb0d40eb4d8027d90f-Abstract.html)、[POMCPOW](https://ojs.aaai.org/index.php/ICAPS/article/view/13882)。このPRはこれらの探索器を実装済みと主張しません。
