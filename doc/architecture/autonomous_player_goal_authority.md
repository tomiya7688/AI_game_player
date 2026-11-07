# 自律プレイヤーのGoal所有権とGUI「友達」メッセージ契約

> 正本：Issue #19 / #843 / #844。作成日：2026-11-07。対象：賢者ミスクおよび同じCoreを利用する自律Player。
>
> **これは目指す完成仕様であり、#843/#844等のRuntime実装が完了したことを示さない。**

## 製品はユーザー操作のマクロではない

賢者ミスクは、利用者が「次に何をしろ」と指示した操作を実行するための自動マクロではない。**ゲームを最初からプレイする本人**である。自ら画面を観測し、ルールを仮説化し、目標候補を作り、何を良い結果とみなすかを計画し、戦略を作り、操作後の結果を解釈する。失敗した時はActionの選び方だけでなく、評価計画、場合によっては目標仮説まで自分で立て直す。

GUIで人間が送る文章は、隣でプレイを見ている友達が「こうしたらいいんじゃね？」と話しかけるもの。賢者ミスクは聞いてもよいし、断ってもよいし、保留・質問してもよい。**人間が文章を送ったことはゲーム内Goalの採用条件でも実行許可でもない。**

## 混同してはいけない3種類の権限

| 主体 | 持つ権限 | 持たない権限 |
| --- | --- | --- |
| **自律Player（賢者ミスク）** | ゲーム内Goalの発見/選択/改訂、評価基準/評価Planの設計、Taskの採否、行動・戦略・学習の選択 | OS Input Safety、Stop、ホスト資産保護、外部送信の同意を上書きする権限 |
| **GUIの友達（利用者の自然文）** | 会話、助言、提案、質問、観測に関する情報提供 | Game Goal/TaskQueue/Interrupt/Priorityの無条件変更、モデルに実操作を強制する権限 |
| **ホスト制御を行う利用者/管理者** | 対象Window、実入力許可、Stop/Pause/Emergency Stop、通信/Privacy/課金同意、Resource上限、ファイル保護 | ゲーム内Goalを「普通のChat発言だけ」でプレイヤーに強制する権限 |

「出所を認証できる」と「その出所がゲーム内Goalの上位権限である」は別。USER_CHAT/USER_PHYSICALのprovenanceは真の発言者を識別するものであって、賢者ミスクへの命令権ではない。ゲーム画面のOCR・モデル出力は友達のChatへ自動昇格しない。

**Chatの「止まって」は友達の発言**である。賢者ミスクがプレイを止めるか提案として検討してもよいが、ホストのStop/Emergency Stopボタン・物理ホットキーは別経路で必ず即座に制御する。安全停止は賢者ミスクの同意を待たない。

## 自分で遊ぶための基本ループ

    画面/ルールの手がかり/既存経験
       ↓
    Player Goal Discovery（#843）
      - 自分は何を目指すべきか？
      - 他の目標候補は？
      - どう観測すればGoal仮説を反証できるか？
      - 自分が受けた友達の提案を採用/拒否/保留/質問するか？
       ↓
    Evaluation Design Planning（#844）
      - 何が良い結果か？
      - 何を測定/観測する必要があるか？
      - 手前のScoreやHPと最終結果は一致しているか？
      - 評価方法をどう反証/改良するか？
       ↓
    Evaluator Hypotheses（#771）/ Rewrite（#772）/ Reliability（#773）
       ↓
    Model/Planner/SearchProgram → Candidate Decision
       ↓
    Verifier → Execution Safety Kernel → OS入力
       ↓
    新Observation + Outcome/因果的なEffect（未確認を既知にしない）
       ↓
    誤評価/失敗/停滞なら GoalPlan・EvaluationDesignPlan まで再計画
       ↓
    Challenger → 独立Replay/実Play比較 → Promote/Rollback（#171/#173/#780）

ゲーム画面やルールの理解が足りない場合も、モデルの確信で勝手にGoal確定とせず、観測・探索・別手法を有限Budgetで選ぶ。全UNKNOWNを永続停止とするのも完成仕様ではない。

## GUIの発言からGoalに至る状態遷移

1. **FRIEND_MESSAGE**：Chat出所と日時・Session・ROI等を記録。まだTaskではない。
2. **INTERPRETED_OFFER**：自然言語から意味/対象/不明点を解釈。命令の確定ではない。
3. **PLAYER_DISPOSITION**：賢者ミスク自身が ACCEPT / DECLINE / DEFER / CLARIFY を決定。決定理由・GoalPlan versionを保存。
4. **PLAYER_GOAL_ADOPTED**：ACCEPTと同一Session/epoch/TTL/Targetを確認して、次Decision Step境界で本人のGoalPlanに組み込む。採用済でも目標の再考はできる。
5. **EXPIRED/REJECTED**：不採用/期限切れの文がTaskQueue/Planner/OS Inputへ直接届かない。

既存のUserCommand、PendingInstruction、TaskRequestという名前は移行中の型名として残っていてもよいが、**それらを持つだけでゲーム内の命令権限が付くわけではない**。将来の契約では FriendMessage/PlayerGoalOffer/PlayerDisposition/GoalPlanSnapshotを区別する。

通常のゲームGoalのpriorityは賢者ミスク本人が決める。GUIの友達はUSER_HIGHで自律Goalを押しのけられない。ホストの緊急停止等はGoal priorityではなく独立した制御権限として処理する。

## Goal、Value、Evaluator、Reliabilityを別の事実として保持する

- **Observation/Belief**：どんな画面/値が観測されたか。HPの増減そのものに善悪はない。
- **Goal hypothesis**：賢者ミスクは何を目指そうとしているか。ユーザーの文章は採用前は別の提案。
- **Evaluation design plan**：良し悪しをどう定義・観測・反証するか。賢者ミスクが自分で立案する。
- **Evaluator candidate**：具体的な計算式/順位付け。複数候補・別モデル・相関を保持する。
- **Reliability**：独立したOutcomeで候補の当たり具合を計測する。自分で作ったScoreを自分の正解にしない。
- **Execution safety**：入力先・停止・許可・プライバシー等のhard rule。評価スコア/多数決で上書き不可。

評価器が間違ったら関数を改良する。さらに「何が良い結果か」の仮説が間違っていたらEvaluationDesignPlanを作り直す。ゲーム自体の目標の解釈が誤っていたらGoal Discoveryに戻る。

## 必須の回帰例（最終Runtimeの受入条件）

- GUI Chatが無くても、未知ゲームから異なるGoal候補を複数考え、Player自身が採用して遊び始める。
- 友達が「回復アイテム探して」と言ってもDECLINE/DEFERなら現Goal/TaskQueue/入力は変わらない。ACCEPTなら理由と版を記録して次Stepから反映。
- 友達の助言に疑問があればCLARIFYし、会話だけで完了できる。自然文「止まって」で強制Emergency Stopを起動しないが、専用Stopボタンは即遮断する。
- OCRの「ボスを倒せ」やモデルの自由文が直接ユーザー命令権限を得ない。ゲーム内Goalの手がかりとしては観測できる。
- HPを守る評価で進行できず、HP消費によって扉が開いた結果から本人がGoal/価値Planを再考する。
- Scoreが増え続けても独立したゲーム進行・達成がない場合、Proxy Objective Divergenceを検出し評価Planからやり直す。
- Stop/Target/Source世代変化後、古い友達提案・旧Goal Plan・旧モデル回答が新Sessionへ混入しない。
- 対話なし/受諾あり/拒否ありの実Play Quality、追加計算のp50/p95/CPU/RAM/VRAM/予算とSafety違反0を分離して検証。
- Runtime実装前は設計だけを完成扱いにする。既存6軸評価や候補順位付けが動いてもGoal Discovery完成と称しない。

## 改訂対象Issue

- 正本・製品：#19 #710 #760 #769 #804
- Goalと評価Plan：#843 #844 #763 #764 #765 #766 #767 #768
- 自己生成・学習：#770〜#774 #780 #781 #782 #800
- GUI/Chat/Task：#436 #437 #536〜#538 #584 #12 #438 #439
- 自律Player Builder/Lineage：#431 #433
- Safety・品質：#206 #122 #735

実装を担当する各Issueはこの正本と一致した契約・負例テストを持つこと。矛盾する旧文面を残す場合は、実装前に修正しないまま採用しない。
