Spec-Status: READY
Depends-On: none
Decision-Gaps: 各triggerのしきい値と不応期の数値は、実装Issueで実測してから決める（この資料は条件と構造だけを決める）
Parent: #19

# Issue #864 LLMのマクロ判断を「必要な時だけ」呼ぶMacroTriggerと、LLM非依存のミクロ層

## 成果物
設計資料のみ。コードは変更しない。実装は「実装Issueの分割案」の単位で別Issueにする。

## 利用者の操作
利用者の操作はありません。設計方針の合意だけが必要です。

## 背景
現状は1操作ごとに1判断です（`CampaignLoop`の各stepが`DecisionPipeline`を通る）。#19は、リアルタイムゲームでは重いLLMを非同期・低頻度のstrategic prior更新へ分離する原則を定めていますが、「いつLLMを呼ぶか」は未定義でした。

## 対象範囲
対象: LLM呼出しの条件(MacroTrigger)、LLMの出力(StrategicPrior)、呼出し制御、LLM不在時の動作の設計。対象外: コード、しきい値の数値、モデルの選定、Safetyの変更。

## 目的
人間も常に大局を考え直してはいません。LLMを呼ぶのは秒数が来た時ではなく、**マクロ判断が必要になった時**だけにします。それ以外の全ての判断（操作粒度の判断、候補の絞り込み、タイミング、失敗からの立て直し）は、LLM以外の部品だけで完結させます。#19の「重いLLMは非同期・低頻度のstrategic prior更新へ分離し、結果はsnapshot/generation/TTLへbindしたhintとして次step以降へ反映する」を、呼出し条件まで具体化します。

この資料は、各stepの判断をLLM以外が担い、LLMはその上の層へ移す方針を決めます。

## 2層の分担
| 層 | 担当 | 速度 | LLMへの依存 |
|---|---|---|---|
| マクロ（上位） | 目標・方針・計画の選択、未知の状況の解釈、評価軸の見直し | 必要な時だけ。数秒〜数十秒かかってもよい | あり |
| ミクロ（下位） | 各stepの候補選択、操作のタイミングと粒度、失敗後の即時の立て直し | 毎step。観測と同じ周期 | **なし** |

ミクロ層は既存の部品で構成します: Planner（`bounded_planner.py`）、`baseline_provider.py`、Recovery（`recovery_controller.py`）、Safety、候補生成。これらはLLMなしで動くため、LLMが不在・遅延・失敗でも停止しません。

## マクロの出力はActionではなくStrategicPrior
LLMの結果をそのまま実行しません（応答が届く頃には画面が変わっているため）。結果は、次を満たす**StrategicPrior**に限ります。

- 内容: 目標、優先する候補の種類、避ける候補の種類、ミクロ層への重み付けや制約。具体的なAction IDや座標は含めない。
- 結び付け: 作成時のsnapshot、episode世代、有効期限(TTL)。
- 反映: ミクロ層は次step以降、事前集約されたprior参照（条件分岐/lookup）で使う。priorの適用はstepごとの高速な処理に限り、LLMを待たない。
- 検証: 現在の候補集合に対して検証し、候補外を指すhintは捨てる。Safetyは常にpriorより優先する。
- 変換: LLMの応答は、LLMを使わない`PriorCompiler`が、固定スキーマで検証してからpriorへ変換する。lookupのキーは「画面の種類(state class)×候補のkind」で、値は重みと禁止フラグ。DecisionContextの候補が持つ`kind`にだけ結び付けるため、候補の実体が変わってもpriorは安全に適用できる。スキーマ違反・解析不能な応答はpriorを作らず、失敗として扱う。

## MacroTrigger（いつLLMを呼ぶか）
Triggerは全てLLM以外の監視部品が計算します。LLMを呼ぶかどうかの判断にLLMを使いません。

| Trigger | 発生条件 | 検出する既存部品 |
|---|---|---|
| `no_valid_prior` | 開始直後、または結果の破棄・失敗の直後でpriorが無い | priorの結び付け情報 |
| `scene_changed` | 画面の種類(state class)が変わり、連続2観測以上その種類が続いた（スクロールやアニメーションの揺れは数えない） | observation / DecisionContextのstate |
| `unknownness_high` | 未知の状況がしきい値を超えて継続した | `unknownness.py`（UnknownnessMonitor） |
| `outcome_surprise` | 操作の結果が予測と食い違った状態が続いた | `outcome_fusion.py` |
| `no_progress` | ミクロ層の立て直しでも停滞が解けない | `recovery_controller.py` |
| `micro_abstain` | ミクロ層が連続して棄権した（単発の棄権は数えない） | Planner / Baselineのabstain |
| `goal_event` | 目標の達成・失敗、terminal状態、利用者からの新しい指示 | episode状態 |

TTLが切れただけではLLMを呼びません。TTL切れのpriorは失効扱いにしてミクロ層はBaselineで続け、上の表のTriggerのどれかが成立した時にだけ呼びます。したがって、状況が安定していればLLMは何回も呼ばれません（時間で呼ぶ経路は無い）。

## 呼出しの制御
- **同時に1呼出しまで。** 飛行中に新しいTriggerが来た場合は、呼出しを増やさず、1つの待ち状態へ統合する（理由の集合として保持）。
- **結果の破棄。** 応答時にepisode世代が変わっている、またはsnapshotが結び付け範囲を外れた場合は結果を捨てる。捨てたことは記録し、必要なら統合済みの理由で次の呼出しを1回だけ行う。場面の変化が速く、結果が続けて捨てられる場合は、その連続を記録し、不応期のあとも続くならミクロ層のBaselineだけで継続して、その場面ではLLMを使わない。
- **不応期と予算。** 理由に関係なく全体で、直前の呼出しから最小の不応期を空け、一定時間あたりの呼出し回数にも上限を置く（理由を交互に変えても回避できない）。これは頻度の目標ではなくコストと暴走を防ぐ上限で、数値は実測で決める。Safetyと緊急停止は不応期の対象にしない。
- **予算。** 呼出し回数・遅延・失敗を計測して記録し、利用者のResource Budget設定(#19)を超える場合はミクロ層だけで継続する。

## LLMが使えない時の動作
| 状況 | ミクロ層の動作 |
|---|---|
| 応答待ち | 現在のpriorがTTL内ならそれを使う。なければ既定のBaselineで継続する |
| TTL切れ・失敗・timeout | Baselineへ降格する。停止はしない。降格を記録する |
| LLMなし（設定で無効） | 常にBaseline/Plannerのみ。Triggerは記録だけして呼出しはしない |
| 応答がスキーマ違反・解析不能 | priorを作らず失敗として記録し、Baselineで継続する。同じ入力での即時の再呼出しはしない |
| priorが現在の全候補を除外する | そのpriorを捨て、Baselineで継続する。`micro_abstain`相当として記録する |
| 応答がgoal_event・terminal状態のあとに届く | 捨てる |
| Baselineも棄権する | Recoveryに渡す。回復できなければ既存のfail-safeに従って停止し、エスカレーションを記録する（黙って続けない） |
| 降格状態からの復帰 | 次のTriggerが成立し、予算内で呼出しが成功して有効なpriorが得られたら復帰する |

## 既存仕様との関係
- #19: 本設計はその低頻度strategic priorの具体化で、方針（hot pathにLLMを置かない、結果はstale Actionとして実行しない）は変えない。
- Level 1 runner / 2048 Planner: 既にLLM不要のミクロ層として動く。変更不要。
- DecisionContext / Decision LLM: 既定は、マクロの呼出しでpriorを返す役です。例外として、Game Profileで`turn_based`と明示された（リアルタイムでない）ゲームに限り、手番の開始を`turn_decision_required`というTriggerとして扱い、手番ごとに1回の呼出しを許します。この場合も、同時1呼出し・不応期・予算の制御は同じに適用され、手番の間のミクロな操作（クリックの分割など）はLLMなしで行います。リアルタイムゲームには適用しません。
- Safety / Recovery: 変更しない。priorはSafetyを緩めない。

## テスト（実装Issueでの受入の方向）
- Triggerごとに、条件成立でだけ呼出しが要求され、成立しなければ要求されないこと。
- 飛行中のTriggerが1つの待ち状態へ統合され、呼出しが同時に2つにならないこと。
- 応答時に世代が変わっていたら結果が破棄されること。
- LLMを遅延・失敗・無効にしても、ミクロ層が止まらずBaselineで継続すること（`noisy_provider.py`の`NoisyLanguageProvider`の考え方を流用）。
- 揺れる画面で`scene_changed`が毎stepは成立しないこと（デバウンス）、理由を交互に変えても不応期を回避できないこと。
- 失敗時の動作表の各行（スキーマ違反、全候補除外、遅れて届く応答、Baseline棄権）。
- LLM呼出し回数がstep数に比例しないこと（安定した場面で0回、場面転換で増える）を計測で確認すること。

## 実装Issueの分割案
1. MacroTrigger型と監視（既存部品からの条件算出、統合、不応期）。LLMは呼ばない。
2. StrategicPrior型（結び付け、TTL、検証）と、ミクロ層への反映点。
3. 呼出し制御（同時1つ、世代破棄、降格、計測）とDecision Providerへの接続。
4. 実測（呼出し回数・遅延・品質）と、しきい値・不応期の確定。

## 完了条件
この資料がレビューされ、上記の分割案が実装Issueとして起票されること。コードは変更しない。
