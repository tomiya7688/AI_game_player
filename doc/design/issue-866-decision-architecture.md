Spec-Status: READY
Depends-On: #864（MacroTrigger設計。段階表と判断機の名簿の正本）
Decision-Gaps: #800のD1〜D21の採否（設計者）。#440と#162の実装の統合方法、#171と#532のGateの共通化は、子Issueで評価する
Parent: #19
Last-Checked: 2026-11-08

# Issue #866 判断・操作の層構造と、モデル運用・学習・知識増築・データ圧縮の統合設計

## 成果物
設計資料のみ。コードは変更しない。既存の設計資料・Issueの責務境界と、空白の所有者を決める。実装は子Issueに分ける。

## 利用者の操作
利用者の操作はありません。設計方針の合意だけが必要です。

## 背景
[#864の設計](issue-864-macro-trigger.md)（判断・操作の12段階、判断機の名簿、MacroTrigger）をレビューする中で、判断機の層構造・モデル運用・学習・知識増築ループ・データ圧縮が互いに依存し、既存のIssue・設計書と重複・矛盾・空白があると分かった。個別に設計すると食い違うため、統合設計として責務の境界を先に決める。

## 対象範囲
対象: 呼出しの仕組みの責務分割、資源予算とモデルlifecycleの所有者、学習の実行段階、StrategicPriorとKnowledgeの関係、データの圧縮・忘却の契約、Provider APIへの載せ方、privacy区分、子Issue案。対象外: コード、数値、モデル選定、学習・知識増築の本体の設計（[#800](https://github.com/tomiya7688/AI_game_player/issues/800)が扱う）。

## 決定事項

### D1 呼出しの仕組みは1本の流れに整理する
呼出しタイミングに関わる仕組みが3つ並存していた（#440 同期Reasoner Cascade、#162 非同期Background Queue、#864 MacroTrigger）。競合させず、責務で分ける。

| 責務 | 担当 | 内容 |
|---|---|---|
| 呼ぶべきか | MacroTrigger（#864） | 決定的な証拠だけでマクロ判断の要否を決める。呼出しの実行はしない |
| どのモデルか | Router（#161、#441/#527/#528のRegistry） | capability・cost・latency・healthで選ぶ |
| どう実行するか | Background Queue（#162） | priority・deadline・cancel・stale結果の破棄・結果の公開・queue計測。#864の`request_id`による遅延応答の拒否、飛行中Triggerの統合、完了ごとの再評価は、#162のqueue契約として定める |
| 実行してよいか | Admission（#425、#544〜#546） | 資源予算に基づく承認（D2） |
| 同期で昇格するか | Cascade（#440） | 同一の手番内で時間があり、安い判断機の確信が足りない場合の同期の昇格。手番ゲームの`turn_decision_required`はこちらが主 |

流れ: Trigger成立 → Queueへ提出（理由・deadline・priority・予算クラス） → Admission承認 → Routerがモデルを選択 → Providerを呼ぶ → Queueが結果を公開 → `PriorCompiler`で検証してprior化。#864の設計資料で「呼出しの制御」とした内容のうち、実行機構は#162に所属させ、#864はTriggerと、priorの検証・失効（結び付け）だけを持つ。

### D2 資源予算はAdmission（#425）に一本化する
バックグラウンドの重い判断機（Macro LLM、VLM/OCR、Plannerのworker、軽量適応、LEARN）は、現在それぞれ別の予算（軽量適応のCPU 5%、Planner workerの予算、LEARNの資源方針）を持ち、1つに束ねられていない。全てを#425のAdmissionで承認する。#864の「回数・遅延・失敗」は、予算の実体ではなく、Admissionへ渡す要求と、結果を記録する計測とする。#864が参照した「Resource Budget設定」の実体は#425とする（文書の定義は#425の子で補う）。

### D9 判断は「タスク」、実行は「worker」。LLMは最も高価で汎用的なworkerの一つ
上位（マクロ）の判断機はLLMである必要がありません。言語入力からクラス分類に特化したモデルの方が向く判断なら、それを採用します。LLMの資源は、目的に応じて割り振ります。

| タスク（何をさせるか） | 例 |
|---|---|
| 行動の言語化 | 実行した操作と結果を、言葉で記述する |
| 辞典の作成 | ゲームの語彙・操作名・用語の対応を作る |
| 操作可能性の判断 | 画面から「ここで何ができるか」を導く（#864の段階3） |
| 状況の分類 | 画面や状態を、意味のある種類（state class）へ分類する |
| 何をするかの判断 | マクロ判断（方針の選択） |
| 結果の解釈 | 操作の結果の意味づけ |

| worker（実行主体） | 特徴 |
|---|---|
| 規則・ルール | 最も安く速い。決定的 |
| 分類モデル（言語入力→クラス） | 安い。種類の判定に特化 |
| 小型の言語モデル | 中程度のコスト |
| LLM | 最も高価で最も汎用的 |
| VLM | 画面を直接扱う。高価 |
| 探索（Planner） | 盤面など構造のあるゲーム向け |

- **割当てはRouter（#161）が、タスクごとに決める。** 能力・コスト・遅延・確信度・Admission（#425）の承認で選ぶ。LLMへの割当ては、他のworkerでは足りない場合か、汎用性が必要な場合に限る。
- **安いworkerから試し、足りなければ上位へ昇格する**（#440のCascadeの考え方）。同じタスクを複数のworkerが担えるように、**タスクごとに入出力のスキーマを固定する**（worker非依存）。
- **資源の割振りも目的単位。** LLMの資源を辞典作成に回すか、操作可能性の判断に回すか、何をするかの判断に回すかは、Admissionの予算クラスで表し、状況に応じて動的に決める。
- **辞典などの成果物。** 作成した辞典は、Knowledge側の成果物として検証・保存する。扱いは子Issueで決める。
- #864のMacroProviderは、マクロ判断タスクのスキーマを満たす任意のworkerが実装する契約で、LLM専用ではない。

### D3 ローカルモデルのlifecycleの所有者
（対象はLLMに限らない。ローカルで載せる全てのモデルが対象。）常駐・退避・warmの決定は現在どの文書も持たず、Ollama任せになっている（`provider.py`に記述なし）。所有者を次のとおり分ける。

| 内容 | 所有者 |
|---|---|
| 取得・配置・版・キャッシュ | Model Artifact Manager（#242） |
| 載せてよいか・どれだけ載せるか | Admission（#425）。VRAM/RAM予算 |
| どのモデルを載せるか・warmにするか | Router（#161）。マクロ用の大きいモデルは呼出し頻度が低いため退避を許し、loadの遅延は呼出しのdeadlineに含める |
| 実際のload/unload | Provider adapter（Ollama等）または#241の同梱service |

loadを待つ呼出しはQueueで待たせ、deadlineを超えれば失敗として降格する（#864の降格表に従う）。

**取消しと停止の確認（QueueとAdmissionの契約）。** `request_id`による応答の拒否は、結果の安全のための手段で、処理を止める手段ではない。timeout・世代変更・LEARN移行で呼出しを破棄する時は、(1) Provider API v1のcancel要求を送る。cancel応答の`accepted`は**受付**であり、処理の停止完了ではない。`unsupported`のProviderもある。(2) **停止完了の判定根拠**は次のとおり。`already_finished`は完了。`not_found`は「Providerがその要求を知らない」ことを示すだけで、処理が始まっていない保証ではない（受信前か、忘却後か区別できない）ため、停止の確認にはならず、停止未確認として扱う（adapterの記録で「送信前」と分かる場合に限り完了とする）。`accepted`は、ローカルのProviderでは、adapterが管理するworkerのprocess終了、またはhealth/probeが「処理中の要求なし」を報告した時に完了とする（Provider adapterがこの確認手段を提供することを契約とする）。確認手段がないProviderは、停止が確認できないものとして扱う。(3) 停止が確認できるまで、そのworkerが使っている資源はAdmissionの承認から**解放しない**（実際には占有されているため）。(4) 一定時間内に確認できない、またはcancel非対応のローカルProviderは、adapterがworkerを強制的に終了・再起動する。強制終了できないか、遠隔のProviderで停止を確認できない場合は、**1要求あたりの最大実行時間**が経過するまで資源を占有しているとみなし、その後に解放する。最大実行時間は、Provider API v1のmanifestに標準のfieldがない（`default_timeout_ms`はクライアント側のtimeoutで、処理停止の保証ではない）ため、Admissionの設定（Providerごとの宣言）か、Provider API v2（#447）のmanifest拡張で与える。上限の根拠がない場合は、占有中として扱い、解放せず「回収不能」に計上する。(5) それでも解放できない資源は「回収不能」として記録し、以後の承認から差し引く。次の要求や学習は、この差し引き後の資源で承認する。

### D4 学習は非同期のhot-path workerではない
#864の段階表で段階10（改善）・11（知識の蓄積）を「非同期」と書いたが、正確には次のとおり。重い学習は`runtime_modes`のLEARN段階で、ゲームプレイを止めて行う。EVALUATEを経由しないとPROMOTEに入れない。推論中に並行して動くのは軽量適応（#422）だけである。したがって、

- 段階10・11は「Modeに従う（LEARN/EVALUATE/PROMOTEの各段階、または軽量適応のworker）」とする。#864の資料を修正する。
- LEARN中は新しいマクロ呼出しを提出しない。飛行中の呼出しは破棄し、priorは失効してBaselineに降格する。
- Gateの二重（Knowledge昇格の#532と、認識・判断parameterのChallenger昇格の#171）は、対象が別（Knowledge snapshotと、confidence_scale等のparameter）なので重複ではない。ただし、世代・rollback・監査の形式が揃っていないため、共通の契約にできるかを子Issueで評価する。

### D5 StrategicPriorとKnowledgeの関係
| | StrategicPrior | Knowledge |
|---|---|---|
| 寿命 | 短い（TTL、episode内） | 長い（昇格済みの不変snapshot） |
| 作り方 | オンラインのマクロ判断 | Experience → Builder → Gate |
| 保存 | 失効記録を除き永続しない | 永続 |

- **注入経路は1本にする。** priorは、Knowledge Feature Adapter（#531）と同じ「step固定のsoft evidence」の投影経路に、`provenance=macro`・TTL付きの別sourceとして載せる。priorのための別の注入経路は作らない。Safetyが常に優先する。
- **priorをそのままKnowledgeにしない。** マクロ判断とその結果は`macro_decision`というExperience eventとして記録し、Builder（検証済みの`action_outcome`だけを根拠にする既存規則）→ Gate → Knowledgeの成長ループに乗せる。優れたpriorは、結果として証拠が積み上がった時にだけKnowledgeになる。
- **意味カテゴリと恒久ID。** `LearningIdentityPolicy`は既定で`DISABLED`で、恒久IDは信頼済みprofileが保証するものに限る。#864の候補の意味カテゴリは、sessionの中で有効な決定的カテゴリであり、恒久IDではない。Knowledgeへ昇格する時は、信頼済みprofileが保証するIDだけを使う。
- 成長ループの全体像（診断・提案・Challenger・昇格のつながり）は[#800](https://github.com/tomiya7688/AI_game_player/issues/800)の`self_improvement_loop.md`に委ね、本資料は境界だけを決める。

### D6 データの圧縮・忘却の契約
データには階層があり、下ほど量が減り、価値が濃くなる。

```text
frame・録画（rolling、#256/#257）
  → Experience episode（canonical schema）
    → 圧縮されたExperience（価値ベクトル#793、Reservoir#796、近似重複#799、#165）
      → Knowledge（蒸留、#529〜#532）
```

「蒸留したら原本を忘れる」を安全に行う契約:
1. Knowledgeは、構築に使った原本の範囲を**source-range manifest**（range、hash、watermark、件数）として保持する。原本を圧縮・削除しても、manifestは残る。
2. **出所の記録（manifest）と、保持の参照（retention hold）を区別する。** manifestは出所の記録であり、それだけでは原本を保護しない。原本を保護するのは、(i) 昇格済みKnowledgeが再構成のために必要と宣言した範囲（retention hold）と、(ii) `retention_core`の既存のProtected・外部参照だけ。
3. 原本の圧縮・削除は、(a) Builderがその範囲を消費済み（watermarkを超えている）、(b) 上記(i)(ii)に該当しない、(c) 再構成が不要と宣言されている、の全てを満たす場合だけ許す。削除後もmanifestは残り、再構成が不可能になった範囲はmanifestに明示する。
4. 参照中・Protectedは削除しない（`retention_core`の既存規則）。fail-closedを維持する。

Champion・Challenger・Knowledgeの履歴:
- Champion世代は直近N世代と、rollback履歴（#171は32）を上限とする。
- 不採用のChallengerは、評価の証跡（evaluation event）を残し、parameter本体は保持期限の後に削除する。
- Knowledge/promotion/challengerの各Storeは現在Session rootの対象外（#788）。別rootのretention policyが必要で、所有者を#18（Retention）とする。

マクロの記録（D5の`macro_decision`、request・理由・prior・結果）は、Experience eventと同じretention classに乗せる。呼出し回数・遅延・失敗の計測は、小さい集計だけを永続する。

### D7 Provider APIへの載せ方
マクロ判断タスクを、Provider API v2（#447）のTask種別として`STRATEGIC_PRIOR`を追加する。D9のタスク（行動の言語化、辞典作成、操作可能性の判断、状況の分類）も同様にTask種別の候補とする。worker（LLMに限らない）はこの契約を実装する。v1では、LLMを使うworkerに限り、`text_generation`と固定スキーマ、`PriorCompiler`による検証で橋渡しする。cancelはv1で「データとして扱う」timeout/cancelで足りる（安全性は`request_id`による拒否で担保する）。遠隔のProviderへ送る場合は、privacy宣言を必須とする。

### D8 privacy区分
マクロの要求・応答・priorは、画面の文字を含み得るため、`privacy_data_inventory`に`macro_decision`を追加する（`local_bounded`、画面由来の文字はredactする）。所有者は#225/#123。

## 優先順（#19の実装順表への追記案）
1. 層の契約と判断機の宣言（#864の実装Issue 0）。Game Profile型。
2. Queue（#162）とAdmission（#425）の最小実装。
3. state classと候補の意味カテゴリ、MacroTrigger（#864の実装Issue 1）。
4. StrategicPriorと`PriorCompiler`、Adapterへの注入（#864の実装Issue 2。D5）。
5. MacroProvider（#864の実装Issue 3、D7）とRouter連携。
6. 未接続部品の主経路への接続（#864の実装Issue 4）。
7. データの圧縮契約（D6）は、#165と並行。想起(recall)と汎用World Modelは、#800の設計書の後。

## 子Issue案
| 案 | 内容 | 関連 |
|---|---|---|
| A | 非同期の重い判断機をAdmissionへ統合（D2）。Resource Budget設定の実体の定義 | #425 |
| B | ローカルモデルlifecycleの正本（D3） | #161/#242/#425 |
| C | PriorとKnowledge Feature Adapterの統合、`macro_decision` Experience event（D5） | #531/#419 |
| D | 蒸留後の原本忘却契約とsource-range manifest（D6） | #18/#165 |
| E | Champion履歴・不採用Challenger・Knowledge Storeの保持（D6） | #18/#788 |
| F | `macro_decision`のprivacy区分（D8） | #225/#123 |
| G | Knowledge recallと汎用World Model | #800の後 |
| H | #19の実装順表の更新 | #19 |
| I | Gate共通契約の評価（D4） | #171/#532 |
| J | タスクとworkerの登録（タスクごとのスキーマ、worker一覧、Routerへの割当て規則）（D9） | #161/#447/#441 |
| K | 辞典などのKnowledge成果物の検証・保存（D9） | #419/#800 |

### 子Issueの依存順
```text
H（#19の順序更新）、F（privacy区分）、E（履歴の保持） … 先行なしで並行可能
J（タスクとworkerの登録） → B（モデルlifecycle）、A（Admission統合）
A（Admission統合） → B、#864の実装Issue 3（呼出し制御とMacroProvider）
C（PriorとAdapter統合、macro_decision） → F（privacy区分）、D（蒸留後の忘却）  ※macro_decisionの型が先
D（忘却の契約） → E（履歴の保持）と並行、#165より後に実装
I（Gate共通契約の評価） … 独立。結果次第でE・Cに影響
G（recall・World Model） … #800の設計書の後
K（辞典の検証・保存） → C、J
```
先行条件のない子Issueから順に起票し、依存先が決まるまで実装には入らない。

## #800との境界
[#800](https://github.com/tomiya7688/AI_game_player/issues/800)は、自己成長ループの設計書6本（`self_improvement_loop.md`、`self_evolving_reasoner.md`、`generic_evaluation_v2.md`、`outcome_interpretation.md`、`gameplay_capability_benchmark.md`、`adaptive_compute_reliability.md`）と、設計者の採否待ちの提案D1〜D21を持つ。本資料（#866）と[#868](issue-868-human-cognition-tasks.md)は、次の分担とする。

| 担当 | 持つもの |
|---|---|
| #800の設計書 | 成長ループの本体（診断→Challenger→昇格）、評価（`generic_evaluation_v2`）、結果の解釈、能力のbenchmark、計算資源と信頼性（`adaptive_compute_reliability`。#404〜#411、#425、#539〜#555を含む） |
| #866 | 判断・操作の層構造、呼出しの仕組みの責務分割（Trigger/Router/Queue/Admission/Cascade）、各空白の所有者の割当て、子Issue案 |
| #868 | 人間のゲームプレイ中の認知タスクの一覧と、タスク別のモデル・手法 |

**重なる箇所は、#800の設計書を優先する。** 本資料と#868の記述が#800の設計書と食い違う場合は、#800側に合わせて直す。具体的な重なりは次のとおり。

| #800の提案・設計書 | 本資料・#868の該当 | 扱い |
|---|---|---|
| D2 Holdout消費台帳、D3 学習信号の分離 | #868 E3（保留データ、借り物の勝利の防止） | #800のD2/D3が採用されたら、E3はそれに従う |
| D4 自己発見GoalのPROVISIONAL化 | #868 M5、StrategicPriorの`goal_ref` | goal authorityとD4に従う |
| D8 評価器の相関・多様性 | #868 E3（独立性） | D8の測り方を採用 |
| D9 評価式をRust VMへコンパイル | #868 E1（Typed DSL） | D9と整合させる |
| D12 探索結果の蒸留、D13 Skill/Evaluator Library | #868 X2、M7（スキルの保存） | D12/D13に従う |
| D14 Quality-Diversity Archive、D19 PolicySnapshotの昇格Gate | #866 D6（履歴の保持）、D4（Gateの共通契約） | 子Issue Iで評価 |
| D16 cheap-firstの探索予算 | #866 D1/D9（Cascade、安いworkerから） | 同じ方針 |
| `adaptive_compute_reliability`（#425、#411） | #866 D2（Admissionの一本化） | #800の設計書に合わせる |

D1〜D21の採否は設計者の判断であり、本資料は採否を決めない。

## 決定と未決
**決定（設計者の指示「それを元に進める」による）**
1. #800の設計書との重なりは、#800を優先する（上表）。
2. #440（同期Cascade）と#162（非同期Queue）は、D1の責務の分割で進める。実装を1つにまとめるかは、子Issueで評価する。
3. #171と#532のGateの共通化は、子Issue Iで評価する。

**未決**
- D1〜D21の採否（設計者）。
- #440と#162の実装の統合方法（子Issueで評価）。

## テスト（子Issueでの受入の方向）
- 非同期の重い判断機が全てAdmissionを通ること、予算超過でミクロ層だけで継続すること。
- LEARN中にマクロ呼出しが提出されず、飛行中の呼出しが破棄されること。
- priorが、Knowledgeと同じ投影経路でstep固定されること、priorが直接Knowledgeにならないこと。
- 原本の圧縮・削除で、source-range manifestが残り、Protectedの範囲が削除されないこと。**正例**: Builderが消費済みで、retention holdもProtectedもない原本の範囲は、実際に削除され、manifestだけが残る。一律に削除を拒否する実装は、この正例で落ちる。**負例**: retention holdのある範囲、Builderが未消費の範囲は削除されない。
- `macro_decision`が、privacy区分に従って保存・redactされること。

## 完了条件
この資料がレビューされ、子Issueが起票されること。#864の設計資料が、D1・D4の修正（実行機構の所属、段階10・11の表記）に整合すること。コードは変更しない。
