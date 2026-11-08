Spec-Status: READY
Depends-On: none
Decision-Gaps: 各triggerのしきい値と不応期の数値は、実装Issueで実測してから決める（この資料は条件と構造だけを決める）
Parent: #19

# Issue #864 LLMのマクロ判断を「必要な時だけ」呼ぶMacroTriggerと、LLM非依存のミクロ層

## 成果物
設計資料のみ。コードは変更しない。実装は「実装Issueの分割案」の単位で別Issueにする。

## 利用者の操作
利用者の操作はありません。設計方針の合意だけが必要です。

## 目的
人間も常に大局を考え直してはいません。LLMを呼ぶのは秒数が来た時ではなく、**マクロ判断が必要になった時**だけにします。それ以外の全ての判断（操作粒度の判断、候補の絞り込み、タイミング、失敗からの立て直し）は、LLM以外の部品だけで完結させます。#19の「重いLLMは非同期・低頻度のstrategic prior更新へ分離し、結果はsnapshot/generation/TTLへbindしたhintとして次step以降へ反映する」を、呼出し条件まで具体化します。

現状は1操作ごとに1判断です（`CampaignLoop`の各stepが`DecisionPipeline`を通る）。この資料は、その各stepの判断をLLM以外が担い、LLMはその上の層へ移す方針を決めます。

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

## MacroTrigger（いつLLMを呼ぶか）
Triggerは全てLLM以外の監視部品が計算します。LLMを呼ぶかどうかの判断にLLMを使いません。

| Trigger | 発生条件 | 検出する既存部品 |
|---|---|---|
| `no_valid_prior` | 開始直後、またはpriorがTTL切れ・世代不一致になった | priorの結び付け情報 |
| `scene_changed` | 画面の種類が変わった（画面識別・signatureの大きな変化） | observation / DecisionContextのstate |
| `unknownness_high` | 未知の状況がしきい値を超えて継続した | `unknownness.py`（UnknownnessMonitor） |
| `outcome_surprise` | 操作の結果が予測と食い違った状態が続いた | `outcome_fusion.py` |
| `no_progress` | ミクロ層の立て直しでも停滞が解けない | `recovery_controller.py` |
| `micro_abstain` | ミクロ層が確信を持てず棄権した（候補に十分なものがない） | Planner / Baselineのabstain |
| `goal_event` | 目標の達成・失敗、terminal状態、利用者からの新しい指示 | episode状態 |

時間に由来するのは`no_valid_prior`のTTLだけです。これは「呼ぶ間隔」ではなく「古い方針を使い続けない上限」で、状況が安定していればLLMは呼ばれません。

## 呼出しの制御
- **同時に1呼出しまで。** 飛行中に新しいTriggerが来た場合は、呼出しを増やさず、1つの待ち状態へ統合する（理由の集合として保持）。
- **結果の破棄。** 応答時にepisode世代が変わっている、またはsnapshotが結び付け範囲を外れた場合は結果を捨てる。捨てたことは記録し、必要なら統合済みの理由で次の呼出しを1回だけ行う。
- **不応期。** 同じ理由による連続呼出しは、コストと暴走を防ぐ予算ガードとして最小の不応期を置く。これは頻度の目標ではなく上限で、数値は実測で決める。Safetyと緊急停止は不応期の対象にしない。
- **予算。** 呼出し回数・遅延・失敗を計測して記録し、Resource Budgetを超える場合はミクロ層だけで継続する。

## LLMが使えない時の動作
| 状況 | ミクロ層の動作 |
|---|---|
| 応答待ち | 現在のpriorがTTL内ならそれを使う。なければ既定のBaselineで継続する |
| TTL切れ・失敗・timeout | Baselineへ降格する。停止はしない。降格を記録する |
| LLMなし（設定で無効） | 常にBaseline/Plannerのみ。Triggerは記録だけして呼出しはしない |

## 既存仕様との関係
- #19: 本設計はその低頻度strategic priorの具体化で、方針（hot pathにLLMを置かない、結果はstale Actionとして実行しない）は変えない。
- Level 1 runner / 2048 Planner: 既にLLM不要のミクロ層として動く。変更不要。
- DecisionContext / Decision LLM: 「候補IDを1つ選ぶ」役は、マクロの呼出しでpriorを返す役へ寄せるか、非リアルタイムのゲーム（盤面ゲームなど、1手ごとに考える意味があるもの）では従来どおり1手ごとの呼出しを許すかを実装Issueで決める。ターン制では「手番ごと」が自然なマクロ判断の単位になり得るため、この設計は手番ゲームの1手判断を禁止しない。
- Safety / Recovery: 変更しない。priorはSafetyを緩めない。

## テスト（実装Issueでの受入の方向）
- Triggerごとに、条件成立でだけ呼出しが要求され、成立しなければ要求されないこと。
- 飛行中のTriggerが1つの待ち状態へ統合され、呼出しが同時に2つにならないこと。
- 応答時に世代が変わっていたら結果が破棄されること。
- LLMを遅延・失敗・無効にしても、ミクロ層が止まらずBaselineで継続すること（既存のNoisy Provider CIの考え方を流用）。
- LLM呼出し回数がstep数に比例しないこと（安定した場面で0回、場面転換で増える）を計測で確認すること。

## 実装Issueの分割案
1. MacroTrigger型と監視（既存部品からの条件算出、統合、不応期）。LLMは呼ばない。
2. StrategicPrior型（結び付け、TTL、検証）と、ミクロ層への反映点。
3. 呼出し制御（同時1つ、世代破棄、降格、計測）とDecision Providerへの接続。
4. 実測（呼出し回数・遅延・品質）と、しきい値・不応期の確定。

## 完了条件
この資料がレビューされ、上記の分割案が実装Issueとして起票されること。コードは変更しない。
