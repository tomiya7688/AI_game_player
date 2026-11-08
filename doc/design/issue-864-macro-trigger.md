Spec-Status: READY
Depends-On: none
Decision-Gaps: 各triggerのしきい値・不応期・TTLの数値は、実装Issueで実測してから決める（この資料は条件と構造だけを決める）
Parent: #19
Last-Checked: 2026-11-08

# Issue #864 重い判断機の非同期化と、判断・操作の層分離（MacroTrigger）

## 成果物
設計資料のみ。コードは変更しない。実装は「実装Issueの分割案」の単位で別Issueにする。

## 利用者の操作
利用者の操作はありません。設計方針の合意だけが必要です。

## 背景
現状は1操作ごとに1判断です（`CampaignLoop`の各stepが`DecisionPipeline`を通る）。#19は、リアルタイムゲームでは重いLLMを非同期・低頻度のstrategic prior更新へ分離する原則を定めていますが、「いつ重い判断を依頼するか」と、重い判断機一般（LLM以外も含む）の扱いは未定義でした。さらに、画面の認識から改善までの段階は、公式stage IDでは9つ（capture / perception / state / reasoning / decision / safety / execution / outcome / evaluation）に粗くまとめられており、意味論の変換・判断結果の解釈・知識の蓄積と呼び出し・改善が独立した層になっていません。

## 対象範囲
対象: 判断・操作の層分離と判断機の名簿、重い判断機に共通する非同期の規則、マクロ層のLLM呼出し条件(MacroTrigger)と出力(StrategicPrior)、呼出し制御、LLM不在時の動作の設計。対象外: コード、しきい値の数値、モデルの選定、Safetyの変更。

## 目的
重い判断機はLLMだけではありません（VLM、全文OCR、深い探索、学習・評価の更新なども含む）。判断機のうち**そのstepの期限に間に合わない可能性があるものは全て、hot pathの外で非同期に動かします**。hot pathは、期限内に終わる軽い判断機と、結果の取り込みだけで動きます。人間も常に大局を考え直してはいないのと同じで、重い判断は「必要な時」にだけ依頼します（秒数固定ではない）。

「重い」は固定の名前ではなく、実測latencyとその層の期限から判定します（#19の「重い処理は実測とResource Budgetで判定する」と同じ考え方）。

## 判断・操作の段階（全て別の層）
画面の認識から改善までを、次の段階に分けます。各段階は入力と出力の型が違うため、別の層として扱い、段階間はStageEnvelopeで受け渡します。

| # | 段階 | 役割 | 期限の目安 | 同期/非同期 |
|---|---|---|---|---|
| 1 | 画面の認識 | 画面取得、前処理、文字・図形の認識 | frame周期 | 軽い認識は同期。全文OCR・VLMは非同期 |
| 2 | 要素の抽出 | 領域・要素・候補の抽出と同一性の判定 | frame周期 | 同期 |
| 3 | 判断可能性の抽出 | 抽出結果から「ここで何ができるか」（できる操作の種類と条件）を導く | step周期 | 同期 |
| 4 | 意味論の変換 | 要素・状態を、ゲーム上の意味（状態の種類、候補の意味カテゴリ）へ変換する | step周期 | 同期。重い解釈は非同期 |
| 5 | 判断 | 次に何をするかを決める。マクロ（方針）とミクロ（候補選択）に分かれる | マクロ: 数秒〜数十秒。ミクロ: step周期 | マクロは非同期。ミクロは同期（深い探索は非同期のanytime worker） |
| 6 | 判断結果の解釈 | 判断の検証、信頼性、保留、候補との整合 | step周期 | 同期 |
| 7 | 操作の決定 | 解釈された判断から、実行する操作を1つに確定する（Safety検査を含む） | step周期 | 同期、決定的 |
| 8 | 実操作 | 操作の入力への変換、経路・間隔・待機、実行、緊急停止 | 入力の実時間（最も厳しい） | 同期、決定的、モデルなし |
| 9 | 結果の評価 | 操作の結果の検出と評価 | 結果の観測後 | 軽い検出は同期。意味的な評価は非同期 |
| 10 | 改善 | 評価からの診断、更新提案、Gate、Challenger評価 | 分〜時間 | 非同期（hot pathに入れない） |
| 11 | 知識の蓄積 | 経験から知識を作り、検証して公開する | 分〜時間 | 非同期 |
| 12 | 知識の呼び出し | 蓄積した知識を1回の判断へ固定して投影する | step周期 | 同期（事前集約されたsnapshotの参照のみ） |

反射（フレーム単位の反応・タイミング合わせ）は、5のミクロの下位として分けられます。盤面ゲームでは不要で、アクションゲームでは必要になるため、独立の層にするかは実装Issueでゲームの種類別に決めます。

認識（1・2）も同じ規則に従います。軽い認識はhot path、重い認識は非同期で、結果はsnapshotに結び付けて取り込みます。

### 全ての非同期判断機に共通する規則
1. **依頼はhot pathを止めない。** 結果を待つstepは、現在の有効なprior/hintか、Baselineで進む。
2. **結果は直接実行しない。** 古い画面に対するActionになり得るため、結び付け情報（下記）を持つprior/hintとして、次step以降へ反映する。
3. **同時1依頼まで（判断機ごと）、統合、世代破棄、予算。** 「呼出しの制御」に従う。
4. **Safetyと実操作は、どの非同期結果よりも優先する。**
5. **失敗しても下位の層は止まらない。** 降格先は常にモデルなしの層。
6. **Triggerの検出自体はLLM/VLMを呼ばない。** 検出には決定的な証拠だけを使う（後述）。

## 判断機の名簿（2026-11-08時点の接続状況）
各段階について、既存の部品と、主経路（`DecisionPipeline`/`GamePlayerEngine`）への接続状況です。「接続」は、モジュールのimport関係の調査によるもので、実機での動作確認ではありません。

| 段階 | 既存の部品 | 接続状況 |
|---|---|---|
| 1 画面の認識 | `screen_capture`、`frame_analyzer`、`ocr_detector`、`perceptual_hasher` | 接続済み |
| 1・2 高度な認識 | `ui_recognition`、`ui_embedding`、`ocr_provider` | **未接続**（部品はあるが、主経路からもtoolsからも使われていない） |
| 2 要素の抽出 | `region_detector`、`bright_region_detector`、`candidate_merger`、`candidate_identity` | 接続済み |
| 3 判断可能性の抽出 | （候補の統合までで、「何ができるか」を状況から導く専用の部品は無い） | **不足** |
| 4 意味論の変換 | `decision_context`（候補単位の整理）、`knowledge_feature_adapter` | 接続済み。ただし候補の意味カテゴリ（下記）は無い |
| 4 状態仮説 | `world_state_hypothesis`、`belief_*` | **未接続**（ベンチマーク用ツールからのみ使用） |
| 5 判断（ミクロ） | `provider`（Rule/Ollama）、`baseline_provider`、`bounded_planner`、`planner_provider` | Rule/Ollama/Baselineは接続済み。Plannerは2048のcampaign用toolsからのみ使用（GUI経路は未接続） |
| 5 判断（マクロ） | （無い。現状のOllamaは1操作ごとの候補選択） | **不足** |
| 6 判断結果の解釈 | `decision_verifier`、`decision_reliability`、`decision_withheld` | 接続済み |
| 7 操作の決定 | `engine`、`decision_orchestrator`、`action_safety`、`safety_guard` | 接続済み |
| 8 実操作 | `action_executor`、`windows_input`、`execution_receipt`、`fail_safe_runtime` | 接続済み |
| 9 結果の評価 | `outcome`、`outcome_fusion`、`outcome_detectors`、`evaluator`、`evaluation_*` | 接続済み。意味的評価でOllama等を呼ぶ経路がある（下記） |
| 回復 | `recovery_controller`、`self_check`、`recovery_session_adapter` | `recovery_session_adapter`は**未接続**（`recovery_controller`は`self_check`経由のみ） |
| 未知の検出 | `unknownness` | **未接続**（どこからも使われていない） |
| 10 改善 | `training_diagnosis`、`challenger_update`、#171 Gate、#173 評価ランナー | 部品は存在。主経路には自動では接続されていない |
| 11 知識の蓄積 | `knowledge_builder`、`knowledge_snapshot_store`、`knowledge_promotion` | `light_adaptation`経由で接続 |
| 12 知識の呼び出し | `knowledge_projection`、`knowledge_feature_adapter` | 接続済み |

この名簿から分かる不足: 段階3（判断可能性）、マクロ判断、候補の意味カテゴリ、段階1・2の高度な認識、状態仮説・未知の検出・回復の主経路への接続、改善の主経路への接続。これらは実装Issueで順に埋めます。この資料のTriggerは、**接続済みの部品だけで成立するもの**を既定とし、未接続の部品を前提にしません。

## 判断の層（マクロ／ミクロ／反射／実操作）
| 層 | 担当 | 期限の目安 | 同期/非同期 | モデルの利用 |
|---|---|---|---|---|
| マクロ | 目標・方針・計画の選択、未知の状況の解釈、評価軸の見直し | 数秒〜数十秒 | 非同期（イベント駆動） | LLM/VLM可 |
| ミクロ（戦術） | 各stepの候補選択、短い先読み、候補の絞り込み | step周期（数百ms級） | 同期。深い探索は非同期のanytime workerが途中結果(checkpoint)を公開し、期限内に採用 | LLM/VLMは使わない。軽い非LLMのモデル（小型の分類器・価値関数など）/Planner/Baseline/ルール |
| 反射（ミクロの下位） | フレーム単位の反応、タイミング合わせ、危険の回避 | フレーム周期 | 同期 | モデルなし（決定的な規則・既存のチェック） |
| 実操作 | 段階7・8 | 最も厳しい | 同期、決定的 | **モデルなし**。Safetyが常に優先 |

ミクロの重い探索(Planner)は、既に非同期workerがcheckpointを公開して期限内に採用する形で、共通規則を満たしています。

## マクロの出力はActionではなくStrategicPrior
LLMの結果をそのまま実行しません（応答が届く頃には画面が変わっているため）。結果は、次を満たす**StrategicPrior**に限ります。

- **内容**: ミクロ層への重み付けと制約（優先する／避ける**候補の意味カテゴリ**、探索の方向づけ）と、採用するGoalへの参照(`goal_ref`)。具体的なAction IDや座標は含めない。
- **Goalの扱い**: Goalの所有者はPlayerのGoal権限（[自律Playerと権限分離](../architecture/autonomous_player_goal_authority.md)）で、priorではない。priorは`goal_ref`で**既存のGoalを選ぶ**だけで、新しいGoalの提案はGoal権限の手続きに渡す。priorがGoalを直接作る・書き換えることはしない。ミクロ層は、現在のGoalに紐づく重み付けとしてpriorを使う。
- **結び付け**: 作成時の(a)状態の種類(state class)、(b)episode世代、(c)`goal_ref`、(d)手番ID（手番ゲームのみ）、(e)TTL、(f)`request_id`。厳密なsnapshot一致は要求しない（応答が届く頃には新しいsnapshotになっているため）。priorは、(a)(b)(c)(d)が現在と一致し、TTL内の間だけ有効で、どれかが変われば**即座に失効**する。
- **反映**: ミクロ層は次step以降、事前集約されたprior参照（条件分岐/lookup）で使う。priorの適用はstepごとの高速な処理に限り、LLMを待たない。
- **検証**: 現在の候補集合に対して検証し、候補外を指すhintは捨てる。Safetyは常にpriorより優先する。
- **変換**: LLMの応答は、LLMを使わない`PriorCompiler`が、固定スキーマで検証してからpriorへ変換する。lookupのキーは「state class × 候補の意味カテゴリ」で、値は重みと禁止フラグ。スキーマ違反・解析不能な応答はpriorを作らず、失敗として扱う。
- **候補の意味カテゴリ**: 現在の候補の`kind`はclick/keyなど操作の種類で、戦略上の意味（例: 進む・戻る・購入・攻撃）を区別できない。意味カテゴリは`kind`とは別のfieldとして、候補生成側（段階4）が決定的な規則で付与する。LLMが付与することはしない。
- **state class**: scene_changedの検出とpriorの参照が同じ値を使うよう、`DecisionContext.state`のscreen_id・signatureを入力に、LLMを使わない1つの関数でstate classを算出する。生の視覚要約をそのままキーにしない。算出規則と、揺れを吸収する正規化は実装Issue 1で定める。

## MacroProvider（マクロ専用の契約）
マクロの呼出しは、既存の`provider`（`ActionDecision`を返す1操作ごとの判断）とは別の**MacroProvider**契約にします。入力は整理済みのDecisionContext要約と呼出し理由の集合、出力は固定スキーマの応答（`PriorCompiler`が`StrategicPrior`へ変換）です。`GamePlayerEngine.step`の既存の判断経路は置き換えず、Ollama等の既存providerはMacroProviderのadapterを別に用意して接続します。

## MacroTrigger（マクロ層で、いつ重い判断を依頼するか。ここではLLM/VLM）
Triggerは全てLLM以外の監視部品が、**決定的な証拠だけ**で計算します。LLMを呼ぶかどうかの判断にLLM/VLMを使いません。特に、`outcome_fusion`の意味的評価（`semantic_provider.assess_outcome`でOllama等を呼ぶ経路）は、Trigger検出では**無効化**し、局所的・決定的な証拠だけを使います。

| Trigger | 発生条件 | 検出に使う部品 |
|---|---|---|
| `no_valid_prior` | 開始直後（最初の1回）、または結果の破棄・失効の直後でpriorが無く、かつ状況が変化した | priorの結び付け情報 |
| `scene_changed` | state classが変わり、連続2観測以上その種類が続いた（スクロールやアニメーションの揺れは数えない） | state class算出（上記） |
| `unknownness_high` | 未知の状況がしきい値を超えて継続した | `unknownness`（**現在は未接続**。接続されるまでこのTriggerは無効） |
| `outcome_surprise` | 操作の結果が**予測と食い違った**状態が続いた | 期待結果と観測結果の比較。期待結果は`evaluation_prediction`/`transition_model`の予測を使う（予測を持たないゲームでは無効。`outcome_fusion`は観測のみで期待結果を持たないため、比較は新しい部品で行う） |
| `no_progress` | ミクロ層の立て直しでも停滞が解けない | `recovery_controller`（主経路への接続が前提） |
| `micro_abstain` | ミクロ層が連続して棄権した（単発の棄権は数えない） | Planner / Baselineのabstain |
| `goal_event` | 目標の達成・失敗、または利用者（友達）からの提案。**terminal状態は含めない** | episode状態、GUI入力 |

- **terminal状態**: terminalではLLMを呼ばない。episodeを再開し、新しいepisodeの`no_valid_prior`として扱う。
- **利用者の発言**: 友達の発言はGoalの命令ではなく提案（`GuiGoalOffer`）。`goal_event`はマクロ判断を**依頼する契機**にすぎず、Goalを変えるかどうかはPlayerのマクロ判断とGoal権限が決める。採用されるまで、提案の内容はprior・Goalに反映しない。
- **手番ゲーム**: 手番の開始を`turn_decision_required`というTriggerとして扱えるのは、Game Profileで`turn_based`と明示されたゲームだけ。Game Profileは「ゲームの種類（turn_based/realtime）」を持つ型で、候補生成側（ゲームadapter）が提供する。型の定義は実装Issue 0で行い、定義されるまでは全ゲームをrealtime扱い（手番ごとの呼出しなし）とする。

TTLが切れただけではLLMを呼びません。TTL切れのpriorは失効扱いにしてミクロ層はBaselineで続け、上の表のTriggerのどれかが成立した時にだけ呼びます。したがって、状況が安定していれば、開始時の最初の1回を除いてLLMは呼ばれません（時間で呼ぶ経路は無い）。

## 呼出しの制御
- **同時に1呼出しまで。** 各呼出しに`request_id`を割り当て、応答は`request_id`が一致する場合だけ採用する。timeout・キャンセルした呼出しの応答は、遅れて届いても`request_id`で拒否する（スロットを解放しても、古い応答が新しいpriorを上書きしない）。プロバイダ側のキャンセル確認は要求しないが、スロット解放後もそのrequest_idは拒否リストに保持する。
- **統合と再評価。** 飛行中に新しいTriggerが来た場合は、呼出しを増やさず、1つの待ち状態へ統合する（理由の集合として保持）。**どの終わり方（成功・失敗・破棄）でも、完了のたびに**待ち状態を再評価し、まだ有効なら追従の呼出しを最大1回だけ行う。
- **失敗の記録。** 失敗した呼出しの理由は消費済みにし、同じ理由での再呼出しは、**新しいTrigger成立または状態の変化**があるまで行わない。`no_valid_prior`が真のままでも、状況が変わらない限り再試行しない（予算を使い切るまでの繰り返しを防ぐ）。
- **結果の破棄。** 応答時にepisode世代・state class・`goal_ref`・手番IDのどれかが変わっている、またはTTLが切れている場合は結果を捨てる。捨てたことは記録し、必要なら統合済みの理由で次の呼出しを1回だけ行う。場面の変化が速く、結果が続けて捨てられる場合は、その連続を記録し、不応期のあとも続くならミクロ層のBaselineだけで継続して、その場面ではLLMを使わない。
- **不応期と予算。** 理由に関係なく全体で、直前の呼出しから最小の不応期を空け、一定時間あたりの呼出し回数にも上限を置く（理由を交互に変えても回避できない）。これは呼出しを生む条件ではなく、Triggerが成立した後のコストと暴走を防ぐ上限で、数値は実測で決める。Safetyと緊急停止は不応期の対象にしない。
- **予算。** 呼出し回数・遅延・失敗を計測して記録し、利用者のResource Budget設定(#19)を超える場合はミクロ層だけで継続する。

## LLMが使えない時の動作
| 状況 | ミクロ層の動作 |
|---|---|
| 応答待ち | 現在のpriorが有効ならそれを使う。なければ既定のBaselineで継続する |
| TTL切れ・失敗・timeout | Baselineへ降格する。停止はしない。降格を記録する |
| LLMなし（設定で無効） | 常にBaseline/Plannerのみ。Triggerは記録だけして呼出しはしない |
| 応答がスキーマ違反・解析不能 | priorを作らず失敗として記録し、Baselineで継続する。同じ入力での即時の再呼出しはしない |
| priorが現在の全候補を除外する | そのpriorを捨て、Baselineで継続する。`micro_abstain`相当として記録する |
| 応答がterminal状態のあとに届く | 捨てる |
| Baselineも棄権する | Recoveryに渡す。回復できなければ既存のfail-safeに従って停止し、エスカレーションを記録する（黙って続けない） |
| 降格状態からの復帰 | 次のTriggerが成立し、予算内で呼出しが成功して有効なpriorが得られたら復帰する |

## 既存仕様との関係
- #19: 本設計はその低頻度strategic priorの具体化で、方針（hot pathにLLMを置かない、結果はstale Actionとして実行しない）は変えない。
- 自律Playerと権限分離: Goalの所有はPlayerで、友達の発言は提案。priorはGoalを直接変えない。
- Level 1 runner / 2048 Planner: 既にLLM不要のミクロ層として動く。変更不要。
- 既存のDecision LLM（`provider`）: 既定のマクロは別契約のMacroProvider。既存の「候補IDを1つ選ぶ」経路は置き換えない。例外として、Game Profileで`turn_based`と明示されたゲームに限り、手番ごとに1回の呼出しを許す。この場合も、同時1呼出し・不応期・予算は同じに適用され、手番の間のミクロな操作（クリックの分割など）はLLMなしで行う。リアルタイムゲームには適用しない。
- Safety / Recovery: 変更しない。priorはSafetyを緩めない。

## テスト（実装Issueでの受入の方向）
- 各判断機が層・期限・同期/非同期を宣言し、期限超過の見込みがある判断機はhot pathで同期呼出しできないこと。
- 実操作の層（段階7・8）がモデルを呼ばないこと（依存の向きの検査）。
- Trigger検出が、LLM/VLM/意味的評価のproviderを呼ばないこと。
- Triggerごとに、条件成立でだけ呼出しが要求され、成立しなければ要求されないこと。
- 揺れる画面で`scene_changed`が毎stepは成立しないこと（デバウンス）、理由を交互に変えても不応期を回避できないこと。
- 飛行中のTriggerが1つの待ち状態へ統合され、呼出しが同時に2つにならないこと。完了のたびに待ち状態が再評価されること。
- timeoutした呼出しの遅れた応答が、`request_id`で拒否されること。
- 失敗した呼出しが、新しいTriggerまたは状態の変化なしに再試行されないこと。
- 応答時に世代・state class・`goal_ref`・手番IDが変わっていたら結果が破棄されること。terminal後の応答が捨てられること。
- 友達の発言が`goal_event`の契機になっても、採用されるまでGoal・priorが変わらないこと。
- 失敗時の動作表の各行（スキーマ違反、全候補除外、遅れて届く応答、Baseline棄権）。
- LLMを遅延・失敗・無効にしても、ミクロ層が止まらずBaselineで継続すること（`noisy_provider.py`の`NoisyLanguageProvider`の考え方を流用）。
- LLM呼出し回数がstep数に比例しないこと（安定した場面で、開始時の最初の1回を除いて0回。場面転換で増える）を計測で確認すること。

## 実装Issueの分割案
0. 層の契約: 各判断機がどの層に属し、期限と同期/非同期を宣言する型。Game Profile（turn_based/realtime）の型。既存のPlanner・Baseline・実操作が規則を満たすことの確認と、名簿の「不足・未接続」の項目のIssue化。
1. state class算出と候補の意味カテゴリの付与（決定的、LLMなし）。MacroTrigger型と監視（既存の接続済み部品からの条件算出、デバウンス、統合、不応期）。LLMは呼ばない。
2. StrategicPrior型（結び付け、TTL、`goal_ref`）、`PriorCompiler`、ミクロ層への反映点。
3. MacroProvider契約、呼出し制御（`request_id`、同時1つ、世代破棄、失敗の消費、降格、計測）とOllama等のadapter。
4. 未接続部品（`unknownness`、`recovery_session_adapter`、期待結果との比較）の主経路への接続。
5. 実測（呼出し回数・遅延・品質）と、しきい値・不応期・TTLの確定。

## 完了条件
この資料がレビューされ、上記の分割案が実装Issueとして起票されること。コードは変更しない。
