# Runtime Evaluation (#491 / 元#8)

既存 `PrimitiveEvaluator` の6軸を、既定の実Session / `GamePlayerEngine` / Decision Context /
Traceへ接続する。評価の追加はModeのARTIFACT_EVALUATIONとは別であり、INFERENCE_ONLYでも
通常の評価・Outcome・Verifier・Safetyを継続する。gradient/Model推論/GPU/IOをAdapterへ追加しない。

## 本番経路と境界

```text
明示完了した前実操作のreceipt + Outcome ── previous_actual
                                                         ┐
既存ContextBuilderのSafety/repetition ── RuntimeEvaluationAdapter ── Context/Trace
                                                         ┘
信頼済み構成の同frame数値予測 ─────────── candidate_hypothetical ── Baseline/Planner
```

EngineはContextBuilderの後、Knowledge投影の前にAdapterを一度適用する。従来の候補・許可集合・
Safety/repetition evidence・utilityを変更せず、任意 `evaluation_projection` を追加する。
RuntimeCompositionはSession configのprofile/scope/goal/予測batchを注入する。standalone Pipelineも
既定Adapterを持つ。bootstrapは6軸をavailable=False/value=0/confidence=0で記録する。
既存PrimitiveEvaluatorの「ongoingならsurvival=.5」という欠損時の値を根拠のある観測にしない。

projectionはcanonical bytesを保持し、外部dictの変更や次stepのprofile交換では変わらない。
schemaは `runtime-evaluation/v1`、最大64候補/128 KiB。scope、goal、profile/sourceのIDと版、
適用6軸weight、axis confidence/availability/evidence_kind、品質と出所、元receipt参照を保持する。
入力障害、範囲不一致、容量超過は未知品質のstatic fallbackとし、候補を消さない。

## 実操作後の値と候補予測

- `previous_actual`: 正常送信・dispatch済み・同Stop/rearm世代のExecutionObservationのみ。
  元snapshotが同Adapterのscope/goalに結び付いていることも照合する。直近32判断を保持し、
  失効・未送信・dry-run・旧scene/goal・不明な元判断は利用不可にする。
- `candidate_hypothetical`: 明示注入されたtyped prediction batchのみ。前Actionの成功/失敗や
  その6軸を各候補へコピーしない。Model自己申告を観測事実へ変えない。

actualの既定で受ける根拠は既存core detectorの限定されたもの。

| 根拠 | 6軸への意味 | 根拠が不足する軸 |
| --- | --- | --- |
| state_delta/v1 のcanonical progress/resources数値差 | observed_metric、差を[-1,1]へ制限 | survival/irreversible_loss等は未知 |
| state_delta/v1 の構造化state/text/elementの変化・安定 | novelty/repetitionのperceptual_cue | 意味的な進捗/成功を作らない |
| terminal_text/v1 の非矛盾・非保留で支持される終端pattern | progress/survivalのterminal_pattern_inference | 正解ラベルや校正済み成功確率とは称さない |
| pixel差分だけ | actualのperceptual_cues欄 | progress/survival等へ転記しない |
| semantic Model主張だけ | 6軸のactual根拠として不採用 | confidence=1でもactualは未知 |

score/levelの増加を無条件に意味的progressへ変換しない。canonical progress/resourcesが得られない
観測では利用不可を記録する。根拠のconfidence×reliabilityはheuristic supportであり、ゲームの
実際の成果や因果的効果を保証しない。#422のverified no_effect学習とは別の経路である。

`actual_evaluation(execution_observation, current_generation)` はterminal直後にも使用できる。
元判断のprofileをcaptureして評価し、次判断・候補予測・追加入力を必要としない。次Contextの
previous_actualも元判断の独立profile/weight/版を保持し、途中で価値基準を切り替えても両経路が
同じactual品質を返す。現在候補のhypotheticalは現在のprofileを使う。

## 信頼済みprofileとframe binding

`EvaluationProfile` は順序付き6軸非負weight（各0..1000）、任意の完全KnowledgeScope/goal制約、
profile/source ID・版を持つ不変設定。game固有コードやLLM生成コードは受け取らない。
未指定は汎用weight、不一致は汎用static fallback。全weight0は未知品質であり確信ある中立ではない。

`AxisPrediction → CandidatePrediction → EvaluationPredictionBatch` は仮説専用の型。
scope/goalと `evaluation_observation_reference(observation)` に一致したbatchだけを適用する。
参照はscreen/signature/capture frame IDのhashでありraw labelや画素を保存しない。capture IDが
無いfixtureでは同一observational signatureの対応であり、未観測のhidden state追跡を保証しない。
未知providerのOCR IDを永続IDと仮定せず、現在の観測の候補IDへだけ結び付ける。

Session構成の注入点:

```python
configuration = SessionRuntimeConfiguration(
    # 既存のProvider/保存先/入力設定に加えて:
    ..., knowledge_scope=trusted_scope,
    evaluation_profile=trusted_weight_profile,
    evaluation_goal_id="goal.explore",
    evaluation_predictions=typed_same_frame_batch,
)
```

hostは `select_profile(profile)` または `select_context(scope, goal_id, profile)` で次stepからの
方針を選べる。scene/goal変更は旧frame予測を破棄し、旧scope actualを不採用にする。
`set_predictions(batch)` は別経路で得た不変数値データの交換だけで、推論callbackを起動しない。
observation.features内の予測dict、scene名やModelの自己申告から自動選択しない。認識/Scene選択/
校正済み予測器の完成はこのAdapterの存在から推論しない（#402/#409/#433/#480の残件）。

## MCDAと欠損を保持した共通品質

PrimitiveEvaluatorで軸の数値・penalty符号を計算する。全weight合計をW、有効軸のweight合計をA、
支持付きweight合計をC=Σ(w×axis_confidence)、V=Σ(w×axis_confidence×axis_value)とする。

- coverage=A/W、confidence=C/W。欠損軸を除いて分母を再正規化しない。
- 条件付きvalue=V/C、soft寄与effective_value=value×confidence=V/W。
- W=0またはC=0はavailable=False/value=0/confidence=0。
- missing_axesを保持し、中立の実測0と未知0を区別する。

共有契約 `evaluation_quality.NumericQuality` は標準Pythonだけを使うfrozen/pickle可能な値。
projection/profile版、最大8つのopaque provenance、coverage、欠損参照を持つ。`bounds` は
effective_value±(1-confidence)を[-1,1]に制限したsupport envelope。未支持部分を[-1,1]へ残す
代数的な範囲であり、統計CI・校正済み成功確率・world/chance branch確率ではない。
仮説leafの型として#565/#566も共用し、意味は外側のactual/hypothetical欄で分離する。

数値は選択した価値基準の無次元[-1,1]であり、ゲーム得点・勝率・物理報酬の単位ではない。
予測producerはscale/極性を宣言し正規化した値を渡す。既定actual差分のclampは外れ値制限で、
異なるゲーム/単位の統計的正規化ではない。単位・scale・軸定義が違う品質をそのまま横断比較せず、
同じscope/profile/source版と条件で比較する。scale変更はsource/profile版へ反映する。異なる
軸欠損の比較にはcoverage/missingnessを残し、実験の適用範囲を限定する。

Baselineはcandidate_hypotheticalだけのeffective_valueを有限なsoft加算として使い、actual欄を
次候補の見込みへ流用しない。RuleProviderも同じBaseline scoreを使える。allowed/Safety/入力権限/
abstain閾値をweightで変更しない。Projectionの復元時は軸・weightからqualityを再計算して改変を拒否する。

## 監査・外部送信

local Trace/Event JournalのContextに全projectionを保存し、候補ID・予測版・profile/source・
最終Decisionを追える。既存offline paired evaluationのsuite digestもlocal projectionを含むので、
数値根拠が異なるケースを同じsuiteとして扱わない。

新しいscope/receipt/profileの情報をModelへ既定で送らない。明示opt-in
`evaluation_expose_to_models=True` 時だけ、scope/receipt/private sourceを省く4 KiB以内の候補品質
要約を既存Provider要求へ含める。省略候補を非許可とはしない。

## 検証・費用

`test_runtime_evaluation_adapter.py` は実Session/Pipeline/Traceで、profileのprogress重視とresources
重視が同じ候補の選択を逆転させること、両候補allowed、範囲外高品質候補もSafetyで除外、
実receipt→post-observe→previous_actualのみ、INFERENCE_ONLY更新0、pixel/Model主張の排除、
世代/scene/goal/frame不一致、欠損・版固定・改変・suite digestを検証する。実OS入力は送らない。

`PYTHONPATH=src:. python tools/benchmark_runtime_evaluation.py --iterations 500
--session-iterations 50 --output build/performance/runtime-evaluation.json` の2026-11-01 Linux実測:

| 経路 | p50 / p95 ms | availability |
| --- | --- | --- |
| Adapter bootstrap、2候補 | .165 / .214 | actual=False / candidate=0 |
| Adapter previous actual、2候補 | .190 / .233 | actual=True / candidate=0 |
| Adapter hypothetical、2候補 | .189 / .235 | actual=False / candidate=2 |
| Adapter hypothetical、64候補 | 3.384 / 3.709 | actual=False / candidate=64 |
| 実Session50判断、測定対照passthrough | 1.726 / 2.456 | 予測根拠なし |
| 実Session50判断、既定評価有効 | 3.915 / 5.658 | 予測根拠なし、6軸利用可否を保存 |

SessionはJSON/Journal記録の増分を含む。対照はbenchmarkだけのfixtureで製品の無効化APIではない。
capture/ML/Windows入力/ゲーム精度を含まず、500回または50判断の有限計測である。hard realtimeや
全ジャンルの勝率改善を保証しない。実プレイ比較は#30/#173/#567の別受入れが必要。

## 手法・配置と残件

既存6軸primitive、confidence-weighted additive utility（線形MCDA）、欠損明示とinterval envelope、
immutable step projectionを組み合わせる。低頻度・最大64×6の型変換/JSON/価値評価はPythonに置き、
OS入力・高頻度Safetyを移さない。Valueの線形加法性は選択したprofileの仮定であり、軸間相互作用や
ゲーム勝率の学習を完成させたとはしない。

- Keeney & Raiffa, *Decisions with Multiple Objectives*（1976/1993）:
  [multiattribute utility](https://doi.org/10.1017/CBO9781139174084)。ここは線形価値基準の注入である。
- Moore, Kearfott & Cloud, *Introduction to Interval Analysis*（2009）:
  [interval arithmetic](https://doi.org/10.1137/1.9780898717716)。ここは未支持量の有界伝播で、統計的校正ではない。
- #409の完全なUtility Estimator、#480/#565の探索、#566の確率的risk、一般Scene/Player解決#433、
  信頼できるOutcome予測/認識#402は独立した完成条件を持つ。基盤portをもって完成扱いにしない。
