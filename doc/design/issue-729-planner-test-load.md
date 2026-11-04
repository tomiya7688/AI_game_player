# Issue #729 高負荷時に結果を期待するテストを決定的にする

Spec-Status: READY
Depends-On: #718（PR #720、toy domainの分離と結果期待テストの予算分離）
Decision-Gaps: なし
Parent: なし

## 成果物

- `tests/planning_warm_worker.py`（新規・テスト専用）: planning workerのinterpreter起動と製品moduleのimportを`plan()`の前に済ませ、plannerの`subprocess.Popen`呼出し時に実processを渡す`warm_worker_launch()`。
- `tests/test_expectimax_planner.py`: worker結果を期待するテストだけwarm起動を使う。予算・TTL・fixture期限を実測に基づき設定。製品の監視loopの競合に対する回帰テストを追加。
- `src/ai_game_player/bounded_planner.py`: 監視loopの1周回で`finished.json`の有無を`ready.json`より先に観測する（下記「手順」3）。
- `tests/test_knowledge_promotion.py` / `tests/test_runtime_boundary_report.py`: 同じ原則（結果を期待するテストは負荷で落ちない、予算・停止を検証する上限は弱めない）で修正。
- `tests/test_planning_worker_diagnostic.py`: 変更なし（`ExpectimaxPlannerTests.run_plan()`を使うため、warm起動の既定で解消）。
- 文書: `doc/versions.md`（Unreleased）、`doc/architecture/expectimax_planning.md`、`doc/ワークフロー/ワークフロー.md`、本設計書。

## 入出力

- `warm_worker_launch() -> ContextManager[None]`
  - 入: なし。環境は現processの`os.environ`に`PYTHONPATH=sys.path`を加えたもの。
  - 動作: 子`python -m planning_warm_worker`を`stdin/stdout=PIPE`、製品と同じ`creationflags`/`start_new_session`で起動し、import完了行`planning-warm-worker-ready\n`を最大120秒待つ（予算の外）。with内で`ai_game_player.bounded_planner.subprocess.Popen`を置き換える。
  - Popen置換: 先頭3要素が`[sys.executable, "-m", "ai_game_player.planning_worker"]`のときだけ、残りの引数をJSON 1行でstdinへ書いて閉じ、warm processを返す。それ以外のcommandは実Popenへ委ねる。2回目のworker起動は`AssertionError`（単一使用）。
  - 子側: `planning_worker`と、`_execute`が資源制限の後にimportする製品module（belief_contract, belief_validation, expectimax_search, planning_contract, planning_outcome, planning_result, planning_file_reader, planning_wire, world_state_hypothesis）を先にimportし、`planning_worker.main(argv)`の終了codeで終わる。登録factory moduleは先読みしない。
  - 失敗: 起動行が来ない・不一致は`AssertionError`。終了時に未終了なら`kill`し`wait(10)`、pipeを閉じる。
- `ExpectimaxPlannerTests.run_plan(..., warm_worker=True)`: `False`で従来の通常起動。`self.last_plan_elapsed_s`に`plan()`だけの所要秒を保存。
- 定数（テスト）: `WORKER_RESULT_BUDGET_MS=10_000`、`REFERENCE_2048_BUDGET_MS=20_000`、`TEST_BELIEF_TTL_NS=120_000_000_000`、fixture `BeliefStorePolicy.max_update_ns=10_000_000_000`、slow_deep予算3500ms、`CHILD_TIMEOUT_S=120`（runtime report）。
- 失敗の扱い: warm processの起動失敗・起動行の不一致・単一使用違反は`AssertionError`でテスト失敗にする（成功扱いにしない）。`kill`後の`wait(10)`が期限切れなら`TimeoutExpired`でテスト失敗にする。
- 製品の型・schema・reason code・予算上限は変更しない。

## 手順

1. 結果を期待するテスト（定義: worker結果のoutcome/reason/値を検証し、予算超過・停止・遅延そのものを検証しないテスト）: `warm_worker_launch()`に入ってwarm processの起動行を受け取った後で`plan()`を呼ぶ。`started`/`cutoff`/request内の`compute_cutoff_at_ns`/workerの探索予算/親の受領期限は全て`plan()`内で従来どおり決まるため、warm起動の待ちはどれにも入らず、値の計算式も変わらない。予算に残るのは引渡し、資源制限の適用、factory import、request読取、探索、受領。対象は`run_plan()`の既定（warm）と、`plan()`を直接呼ぶ結果期待テスト3件（worker_python_exception、reference_play_board、optional_2048）。`run_plan()`の全呼出しと`.plan(`の直接呼出しを列挙して分類した。
2. cutoff・遅延・Stop・観測期限・偽Popenのテスト（`test_slow_model_and_evaluator_are_killed_without_parent_blocking`、`test_stop_and_rearm_reject_results_without_waiting_for_model`、`test_source_version_change_and_observation_expiry_discard_background_result`、`test_final_marker_does_not_leave_an_older_checkpoint_adopted`、`test_invalid_current_snapshot_getter_falls_back_without_launch`）: `warm_worker=False`で通常起動と既存の予算（250ms/1000ms等）・時間上限（1.5s、0.1s）のまま。
3. 製品の監視loop（`BoundedExpectimaxPlanner._run`）: 各周回でdeadline確認の後、`finished_seen = finished.json.exists()`を記録してから`ready.json`→`checkpoint.json`を読み、`finished_seen`のときだけfinished処理へ進む。workerは`ready.json`を`finished.json`より先にatomic publishするため、finishedを観測した周回では必ずreadyも読める。finished処理中に新たにreadyが公開されても、そのreadyは次周回で読む（finishedでloopを抜ける場合は読まない＝従来どおり保守的に`not_applied`）。fail-closedは不変: 不正なreadyは従来どおり`worker_protocol_failure`、readyが無いまま終わったworker（例: 資源制限の適用失敗で`unsupported_resource_limits`）は`limit_enforcement="not_applied"`。予算・cutoff・採否・Stopの判定は変えない。
4. `test_knowledge_promotion`の`max_python_bytes=1`ケース: このケースの目的はメモリ絶対上限の確認だけ。policyは遅延をメモリより先に判定するため、このケースだけ遅延上限（`max_latency_ms=60000`、比1000）を緩める。メモリの絶対上限1byteは維持。遅延上限を検証するテストは従来どこにも無かったため、固定p95値で比（4.9倍は合格、5.1倍は超過）と絶対上限（1000.5ms）を検証するテスト`test_latency_budget_rejects_fixed_latency_ratio_and_absolute_limit`を追加する（実測時間に依存しない）。
5. `test_runtime_boundary_report`のspawn子process待ち（event 10s、join 5s）: 子の公開を待つだけのhang guardなので120秒にする。子が公開すれば即座に戻る。

## テスト

| 受入条件 | 試験 |
|---|---|
| 結果を期待するテストが負荷下で決定的に通る | 論理CPU数の半分（4）のbusy loop下で`tests.test_expectimax_planner`、`tests.test_planning_worker_diagnostic`、`tests.test_knowledge_promotion`、`tests.test_runtime_boundary_report`を各回5分以内で複数回実行し全件成功。無負荷でも全件成功 |
| budget超過・停止の検証を弱めない | 手順2のテストが通常起動・既存の予算と時間上限のまま成功。`slow_deep`はcutoffが深さ1と深さ2の間（深さ2は開始から4s以上）に入ることを維持 |
| warm起動が予算の計算に入らない | メモリ上限テストは`plan()`だけの所要秒（`last_plan_elapsed_s`）が「予算+1s」未満であることを検証。`cutoff`等は`plan()`内で計算され、warm起動で値が変わらないことはコード上の位置で確認 |
| 遅延上限の判定を弱めない | `test_latency_budget_rejects_fixed_latency_ratio_and_absolute_limit` |
| 製品の受領取りこぼしの修正 | `test_fast_worker_exit_keeps_its_resource_limit_acknowledgement`: ready確認直後にready+finishedが書かれる状況を`Path.exists`の差込みで再現し、`limit_enforcement`が実方式であること。修正前のコードで失敗することを確認済み |
| 再現手順の記録 | 下記「現状調査」とPR本文に負荷条件・回数・結果を記録 |
| 全体 | `finish_task.bat`を無負荷と中程度負荷で実行 |

## 完了条件

- 上表の試験が全て成功し、PRのCIが成功する（マージはしない）。
- 予算超過・停止・遅延を検証するテストの予算と時間上限が変わっていない。
- 文書と実装が一致している。

## 現状調査

- 製品: `plan()`は開始時刻からの壁時計で`cutoff`を決め、worker起動（`subprocess.Popen`）・`ready.json`・checkpoint受領の全てをその中で待つ。workerの探索は独自の時計（探索開始から`wall_time_ms`）で打ち切る。
- 実測（8論理CPU、Windows、他エージェントの作業も並走）:
  - workerの新規起動（spawn→ready）: 無負荷0.7〜2.0s、CPU数と同数のbusy loop下で2.6〜6.3s。探索（ready→checkpoint）は負荷下0.42s以下。→ 結果期待テストの予算2500msは起動だけで尽きる。
  - warm起動後の引渡し→ready: 負荷下で中央55ms・最大676ms。他の作業が重なった回は予算内処理の合計が最大2.9s（親のrequest公開からspawnまでで265msの停止を含む）。
  - 参照2048の深さ2探索そのもの: 1.6〜2.0s、負荷下2.8〜3.5s（1回は4.7s超で失敗）。
  - 修正前、8並列負荷で`test_expectimax_planner`は32件中18件失敗（`compute_budget`、`limit_enforcement="not_applied"`）。
  - fixtureのbelief store更新期限100msが負荷下で`update_timeout`になった。
- 値の根拠（実測の最悪値に対する余裕）: 結果期待予算10s＝予算内処理の最大2.9sの約3.4倍。参照2048の20s＝失敗時の4.7s超の約4倍。belief TTL 120s＝warm起動の最大観測（約41s、1回）の約3倍で、起動待ちの上限120sと同じ。fixture更新期限10s＝fixture準備だけの上限で、store自体の100ms期限は`test_belief_candidate_store`側が検証する。slow_deepの3.5s＝深さ2が開始から4s以上かかる性質より短い最大値に近い値。
  - 修正後、warm起動を入れても1回、メモリ上限テストで`limit_enforcement="not_applied"`。原因は手順3の競合（速いworkerがready確認とfinished確認の間に両方を書く）。
  - `test_knowledge_promotion`: 24並列負荷で10回中3回`latency_budget_exceeded`（メモリ判定の前に遅延比5倍で落ちる）。`test_runtime_boundary_report`: 同負荷で10回中10回「child did not publish」（spawn子の起動が10s超）。修正後は同条件で10回とも成功。
  - `test_planning_worker_diagnostic.test_timing_oom_cannot_change_real_selection_or_worker_cleanup`: 8並列負荷で2回とも失敗（run_plan経由、原因は同じ起動時間）。
- 中程度負荷（論理CPUの半分=4つのbusy loop、各回5分以内）での前後比較: 修正前は`test_expectimax_planner`が32件中18件失敗、他3moduleで2件失敗（OOM timingとruntime report）。修正後は`test_expectimax_planner`（33件）と他3module（34件）を各3回実行し全件成功。warm起動のため負荷下のmodule所要は217〜286秒に伸びた（無負荷は約62秒）。
- `finish_task.bat`: 追加負荷なしで成功（1741件、skip 16、678秒。他エージェントの作業が並走）。最初の5分間に4つのbusy loopを加えた実行でも成功（1741件、skip 16、623秒）。
- 再利用: 既存テストが使う`patch("ai_game_player.bounded_planner.subprocess.Popen", ...)`の差込み点、`tests/planning_toy_domains.py`、`planning_worker.main()`。

## 代替案と不採用理由

- 予算をさらに広げるだけ: 起動時間は負荷に比例して伸び（6.3s以上）、`slow_deep`のように深さ1受領とcutoffの両方を検証するテストは広げられない。起動を予算外に出した上で、残りの実測に余裕を持たせる方式にした。
- 製品に注入可能な時計を入れる: belief・観測の時刻と親のmonotonic originを共有する契約（`clock_id`）があり、親だけ別時計にすると鮮度判定と整合しない。workerは独自の時計で探索を打ち切るため効果も無い。
- 製品の計時をworker ready以後から始めるモード: `compute_cutoff_at_ns`はrequestに入れてworker起動前に固定し、結果と照合しているため、プロトコル変更になる。製品の予算意味論を変えない条件に反する。
- worker poolを製品に入れる: 製品機能の追加で範囲外。
- 監視loopの競合をテスト側の待ちで回避: 製品の誤報告（制限適用済みなのに`not_applied`）が残るため、loopの観測順を直した。

## 範囲外・残件

- `test_stop_and_rearm_reject_results_without_waiting_for_model`の`controller.stop()`呼出し0.1s上限は、他の作業が重なった無負荷扱いの実行で1回0.112sになった。停止の性質を検証する上限のため緩めず、範囲外として記録する。再発したら別Issueで扱う。
- worker起動そのものの高速化（製品のpool等）は範囲外。
