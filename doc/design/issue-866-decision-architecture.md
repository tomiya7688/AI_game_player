Spec-Status: READY
Depends-On: #864（MacroTrigger設計。段階表と判断機の名簿の正本）
Decision-Gaps: #800（自己成長ループの設計書、設計者採否待ち）との境界、#440と#162の統合方法、#171と#532のGate共通化は設計者の確認が必要（本資料の「未決・設計者への確認」）
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

### D3 ローカルモデルのlifecycleの所有者
常駐・退避・warmの決定は現在どの文書も持たず、Ollama任せになっている（`provider.py`に記述なし）。所有者を次のとおり分ける。

| 内容 | 所有者 |
|---|---|
| 取得・配置・版・キャッシュ | Model Artifact Manager（#242） |
| 載せてよいか・どれだけ載せるか | Admission（#425）。VRAM/RAM予算 |
| どのモデルを載せるか・warmにするか | Router（#161）。マクロ用の大きいモデルは呼出し頻度が低いため退避を許し、loadの遅延は呼出しのdeadlineに含める |
| 実際のload/unload | Provider adapter（Ollama等）または#241の同梱service |

loadを待つ呼出しはQueueで待たせ、deadlineを超えれば失敗として降格する（#864の降格表に従う）。

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
2. 原本の圧縮・削除は、(a) Builderがその範囲を消費済み（watermarkを超えている）、(b) 昇格済みKnowledgeが参照している範囲はProtected、(c) 再構成が不要と宣言されたもの、の全てを満たす場合だけ許す。再構成が不可能になった範囲は、manifestに明示する。
3. 参照中・Protectedは削除しない（`retention_core`の既存規則）。fail-closedを維持する。

Champion・Challenger・Knowledgeの履歴:
- Champion世代は直近N世代と、rollback履歴（#171は32）を上限とする。
- 不採用のChallengerは、評価の証跡（evaluation event）を残し、parameter本体は保持期限の後に削除する。
- Knowledge/promotion/challengerの各Storeは現在Session rootの対象外（#788）。別rootのretention policyが必要で、所有者を#18（Retention）とする。

マクロの記録（D5の`macro_decision`、request・理由・prior・結果）は、Experience eventと同じretention classに乗せる。呼出し回数・遅延・失敗の計測は、小さい集計だけを永続する。

### D7 Provider APIへの載せ方
MacroProviderは、Provider API v2（#447）のTask種別として`STRATEGIC_PRIOR`を追加する。v1では`text_generation`と固定スキーマ、`PriorCompiler`による検証で橋渡しする。cancelはv1で「データとして扱う」timeout/cancelで足りる（安全性は`request_id`による拒否で担保する）。遠隔のProviderへ送る場合は、privacy宣言を必須とする。

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

## 未決・設計者への確認
1. **#800との境界。** 自己成長ループの設計書(`self_improvement_loop.md`)が設計者の採否待ち。本資料のD4・D5は境界だけを決めているが、重なる箇所があれば#800を優先する。
2. **#440（同期Cascade）と#162（非同期Queue）の統合方法。** D1は責務で分けたが、実装を1つのRouter/Queueにまとめるかは設計者の判断。
3. **#171と#532のGate共通化。** 共通の契約にするか、別のままにするか。

## テスト（子Issueでの受入の方向）
- 非同期の重い判断機が全てAdmissionを通ること、予算超過でミクロ層だけで継続すること。
- LEARN中にマクロ呼出しが提出されず、飛行中の呼出しが破棄されること。
- priorが、Knowledgeと同じ投影経路でstep固定されること、priorが直接Knowledgeにならないこと。
- 原本の圧縮・削除で、source-range manifestが残り、Protectedの範囲が削除されないこと。
- `macro_decision`が、privacy区分に従って保存・redactされること。

## 完了条件
この資料がレビューされ、子Issueが起票されること。#864の設計資料が、D1・D4の修正（実行機構の所属、段階10・11の表記）に整合すること。コードは変更しない。
