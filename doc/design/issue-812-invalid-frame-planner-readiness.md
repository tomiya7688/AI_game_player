Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: #729

# Issue #812 Windows planning comparisonの初回入力0 — plannerのworker準備完了を待つ

## 成果物
- `src/ai_game_player/planning_readiness.py`（新規）: `wait_for_planner_ready(...)` と `PlannerNotReady`。隔離した使い捨て `BoundedExpectimaxPlanner` へ合成beliefを渡し、**製品と同じworker起動経路・同じ `SearchBudget`** で「期限内に深さ1以上のcheckpointを受領できる」ことを連続 `required_consecutive`（既定2）回確認するまで、明示した起動待ち期限まで待つ。
- `src/ai_game_player/planner_2048_observation.py`: 固定盤面の合成belief生成 `reference_2048_readiness_belief(...)`（実画面・乱数・DOMを使わない）。
- `src/ai_game_player/planner_factory.py`: 上を組み合わせる `wait_for_reference_2048_planner_ready(budget, deadline_monotonic, ...)`。
- `tools/compare_2048_windows_planning.py`: planner providerの episode だけ、ゲーム起動直後・初回観測前の新phase `planner_readiness`（起動待ち期限 `PLANNER_READY_TIMEOUT_SECONDS` = 30秒、既存 `SCREEN_READY_TIMEOUT_SECONDS` の45秒とは別）でゲートを通す。結果は `planner_readiness`（有界JSON）として出力する。
- `tests/test_planning_readiness.py`（新規）と `tests/test_windows_planning_comparison.py` の追加。`doc/versions.md`、`doc/architecture` 等の該当文書。

## 入出力
- `wait_for_planner_ready(planner, belief_factory, allowed_action_ids, *, registration_id, budget, deadline_monotonic, required_consecutive=2, retry_interval_s=0.25, monotonic=time.monotonic, sleep=time.sleep) -> dict`
  - `required_consecutive` は1〜5、`retry_interval_s` は0.05〜2.0、`deadline_monotonic` は有限float。範囲外は `ValueError`。
  - 戻り値（成功）: `{"schema_version": 1, "status": "ready", "probe_count", "consecutive", "required_consecutive", "waited_ms", "wall_time_ms": budget, "probes": [最後の16件: {"index", "ready", "reason", "completed_depth", "elapsed_ms"}]}`。
  - 期限切れ: `PlannerNotReady(RuntimeError)`。message は `planner worker not ready within the startup deadline (probes=N, last_reason=R)`（reasonは固定語彙の先頭128字）。`.diagnostic` に同じ有界dict（`status: "not_ready"`）。
- probeの成功条件: `PlanningResult.completed_depth >= 1` かつ `limit_enforcement != "not_applied"`（資源制限の受領確認済みのworker checkpointを `cutoff` 前に受領）。それ以外（`compute_budget`、`worker_failed` 等）は失敗probeで、**連続カウントを0に戻す**。
- 一度に起動するworkerは1つ（既存の `_busy` / `_poisoned` を使う）。probeは入力・Safety・実Pipelineのcontrollerへ一切触れない。

## 手順
1. 責務境界: 本ゲートは「実行環境のworker起動・資源制限適用・import・初回checkpointの往復が、通常のdecision予算内に収まる状態か」を**待つ**だけ。結果は選択・入力・学習factに使わない（合成beliefは `state_available` な実画面ではなく、probeの結果を `PlannerDecisionProvider` へ渡す経路も無い）。
2. 隔離: ゲート専用の `BoundedExpectimaxPlanner`（`reference_2048_registration`、専用 `RunController` を `start()` 済み）を使う。実Pipelineのcontroller・Stop・世代・`last_worker_diagnostic` と共有しないため、Stop/stale検査とInput gateは影響を受けない（実入力ゲートはゲート通過後の `activate_preflight` まで SAFE_IDLE のまま）。
3. 待ち方: 各probeは `budget.wall_time_ms`（comparisonでは400ms）を上限にplannerが打切るので、`deadline - now < wall_time` になったら新規probeを始めず `PlannerNotReady`。probe間は `retry_interval_s` だけ待ち、負荷（ブラウザの描画・Defender走査・他job）が引くのを待つ。**budget・cutoff・判定値は変更しない。**
4. 位置: episode生成（ブラウザ起動）直後に実行する。`ready_deadline`（初回観測〜activation）はゲート**後**に開始するため、ゲートの待ち時間が観測のTTLや既存45秒期限を消費しない。
5. 失敗時: `failure_stage = planner_readiness`、`failure.type = PlannerNotReady`、`planner_readiness` に各probeの理由と所要msを残す。入力は1件も送られず、成功扱いにしない（fail-closed）。保存frameは未取得なので、failure headerの `invalid_frame` は従来どおり「保存frame無し」を示す診断で、判定には影響しない。
6. 非planner provider（baseline/cycling）は対象外（`planner_readiness` は `{"status": "not_applicable"}`）。

## 現状調査
- 根因（run 41945317245 attempt 1, seed 481, planner）: 12回のdecisionのうち11回が `compute_budget`（`completed_depth` 0、worker `ready.json` は受領済みで `checkpoint.json` 無し、`finished_exists` false）。成功run（同seed）は `ready` 約150ms・初回checkpoint 約175ms・以降も約400msで安定。失敗runは `ready` 160-280ms、親のpoll自体も `last_observed` が400msのcutoffを超えて411-850msに遅延し、runner全体のCPU飢餓（WGC timeoutのGDI fallbackも発生）で、Python起動→資源制限→import→探索→checkpointがcutoff内に収まらなかった。`invalid_frame` はこの失敗の原因でなく、診断headerに保存frameが無いという副次症状。
- #794（lease_expired）とは別根因（heartbeat ringは全てrenewed）。#729/#808/#811 と同じ「実時計予算が負荷時の所要時間を含む」種類だが、本件は製品経路そのもの（comparison CI）。#729はテストでworkerを事前起動して予算外へ出した。製品の `plan()` が起動を予算に含める意味論はIssue #729の方針どおり変えない。
- 再利用: `BoundedExpectimaxPlanner`（変更なし）、`reference_2048_registration`、`BeliefCandidateStore`、`SearchBudget`。

## 代替案と不採用理由
- `wall_time_ms` や cutoff を延ばす: Issueが明示的に禁止（単純な予算緩和）。deadline 内に受領できた checkpoint だけを採用する安全性の根拠でもある。
- 失敗decisionを試行回数に数えない／abstentionを増やす: 比較条件（同一step予算）を変え、入力0の原因を隠す。
- 実Pipelineで先に1回 `DecisionOrchestrator.run(execute=False)`: 観測TTL・Stop世代・ReferenceGameの状態に結合し、readiness probeが選択経路に漏れる恐れがある。合成belief＋隔離plannerのほうが入力経路から物理的に分離できる。
- productの `plan()` にworker事前起動プールを導入: 資源制限・cleanup・poison境界を全面的に変える大きな設計変更。範囲外（残件参照）。
- 初回frameの `invalid_frame` だけを直す: 症状はdiagnostic headerの欠落で、入力0の原因ではない。

## テスト
- 受入条件1（準備前を待つ）: 初めの数probeが `compute_budget` で、その後に深さ1が連続2回成功すると `ready` を返す（疑似planner＋疑似時計）。失敗probeの後は連続カウントが0に戻る。
- 受入条件2（予算を緩めない）: ゲートへ渡した `budget` がそのまま各 `plan()` に渡る（同一オブジェクト）。`wall_time_ms` を変更しない。
- 受入条件3（fail-closed）: 常に失敗するplannerでは期限で `PlannerNotReady`（probe数・last_reason付き）。期限が残り `wall_time` 未満なら新規probeを始めない。引数範囲外は `ValueError`。
- 実worker: 実 `BoundedExpectimaxPlanner` ＋ 2048登録で合成beliefが実際にreadyになる（`warm_worker_launch` 無しの製品起動経路。負荷下でも通るよう期限は十分長く取る）。合成beliefは `state_available` 盤面を含み実画面型を使わない。
- comparison: plannerのepisodeは `planner_readiness` を先に呼び、`PlannerNotReady` では `failure_stage == "planner_readiness"` かつ `actions == 0`、`ReferenceGame2048Episode` の `activate_preflight` / `observe` が呼ばれない。非planner providerではゲートを呼ばない。ゲートの待ちは `ready_deadline` を消費しない（既存の期限テストが維持される）。
- CI受入: 同一HEADで `Windows planning comparison (seed 479/480/481)` が成功することをPRのCIで示す。

## 完了条件
上記テストと `finish_task.bat` が通り、設計書とレビュー記録を同じPRに含め、PRのCIでseed 479-481が成功する。予算・判定値・Stop/stale検査は不変。

## 範囲外・残件
- worker事前起動プール／常駐worker化（起動を予算外へ出す製品設計）。必要なら別Issueで性能・安全設計を行う。
- decision loop中に連続して `compute_budget` となった場合の自動回復（今回は成功扱いにも緩和もしない。ゲートが起動直後の飢餓を待つ）。
- failure headerの保存frame取得経路の拡張（#812の副次症状の診断強化）。
