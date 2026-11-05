Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: #178

# Issue #811 Noisy Provider性能予算の判定をrunnerの一時揺らぎに強くする

## 成果物
- `tools/run_noisy_provider_benchmark.py`: 独立した複数round（既定5、奇数かつ3以上）で従来と同じ測定を繰り返し、各指標を「roundごとのnearest-rank p95の中央値」で予算と比較する。
- `tests/test_noisy_provider_benchmark.py`（新規）: 決定論的な疑似時計と軽量engineで、一時揺らぎを許容し本当の退行（処理量2倍など）を検出することを示す回帰テスト。
- `doc/E2E受入試験手順.md` の「Noisy Language Provider 常設CI」節へ判定方法と根拠、`doc/versions.md` へ変更点を記録する。
- 予算値 `config/noisy_provider_performance_budgets.json` とWindows multiplier（1.5）は変更しない。

## 入出力
- CLI: 既存引数に `--rounds N`（既定5）を追加。`N < 3` または偶数は `ValueError` → 終了コード2（既存の引数検証と同じ）。中央値を実際に観測したroundの値にするため奇数に限る。
- `--steps`（既定128、100未満は拒否）と `--warmup-steps`（既定8）は1 roundあたりの値として従来の意味を保つ。
- 終了コード: 全指標合格かつout-of-set fixture拒否で0、予算超過で1、入力/実行エラーで2（従来どおり）。
- report: `schema` を `noisy-provider-benchmark/v3` に上げる。追加・変更項目:
  - `rounds`、`total_measured_steps`、`judgement_method`（"median of per-round nearest-rank p95 across independent rounds"）
  - `measurements.<name>.p95_ms`: 判定値（round p95の中央値）。`round_p95_ms`（全roundの値）、`worst_round_p95_ms`、`pooled_p95_ms`（全sample結合のp95、参考値）、`p50_ms`（全sample結合の中央値）、`p95_budget_ms`、`passed`
  - `selection_count`・`outcome_assessment_count`等は全roundの合計。
  - 消費者は CI artifact のupload（`performance-ci.yml`）だけで、fieldを読むコードは無い（`rg noisy-provider-benchmark` で確認）。

## 手順
1. 各roundは、新しい一時directory・SQLite `EventJournal`・`GamePlayerEngine`・同じseedの `NoisyLanguageProvider` で、warm-up→測定step→直接outcome評価を従来どおり実行する。roundは同一作業量の複製であり、順に実行するため数秒規模のrunner負荷集中は一部のroundにしか入らない。
2. roundごとに4指標（provider選択・provider outcome・Engine総時間・non-provider Core）のnearest-rank p95を計算する。
3. 指標ごとにround p95の中央値（奇数roundなので観測された中央のround p95そのもの）を、丸める前の値のまま予算（budget×multiplier）と `<=` で比較する。表示用の丸めは判定後に行う。5 roundなら3 round以上が予算超過した場合に不合格となる。
   - 検出範囲: 過半数のroundで測定stepの5%を超えて現れる遅延（全体の処理量増加、間欠的でも5%超の尾部退行）。5%以下の頻度の尾部退行は従来の単一p95と同様に検出対象外で、p99/最大値の予算は今回追加しない（予算の新設は性能要件の追加でありIssue範囲外）。
4. 安全不変条件（危険候補を選ばない、Coreがnoisy actionをREJECTしない、provider時間がstep時間を超えない）はround内の全stepで従来どおり検査し、1件でも違反すればRuntimeError→`main`が捕捉して終了コード2（中央値で緩和しない、fail-closed）。reportは全round完了後にだけ書くため、異常時に合格reportは出ない。想定外の例外は捕捉せずPythonの非0終了になる（0にはならない）。out-of-set fixtureは1回実行し従来どおり必須。
5. 測定engineの生成は `engine_factory`（`TimedNoisyLanguageProvider`を受け取り、`step(observation, candidates, purpose=...)` と `last_reliability_result` を持つengineを返すcontext manager）として注入可能にし、既定は従来の一時directory＋SQLite journal＋`GamePlayerEngine`。時間は常にbenchmark moduleの `time.perf_counter_ns` で測り、`measure_engine_steps`（step全体）と `TimedNoisyLanguageProvider`（provider呼出し）の測定経路はテストでも共有する。テストは軽量engineと、moduleの `time` を差し替える疑似時計だけを模擬する。軽量engineはstep内でproviderを呼び、作業量に応じて疑似時計を進める。

## テスト
受入条件ごとの試験（すべて疑似時計で決定論的、ネットワーク・実I/O時間に依存しない）:
- 一時揺らぎで落ちない:
  - 基準作業量（step 100ms、予算150ms）で1 roundだけ全stepが400msになっても合格。
  - #732相当（1 roundのp95が150.66ms）でも合格。
- 本当の退行を検出する:
  - 対照: 基準作業量のままなら合格。
  - 処理量2倍（engineの作業単位を1→2にし全stepが200ms）は不合格・終了コード1。
  - 全roundで10stepに1回だけ予算の2倍かかる尾部退行はp95で検出し不合格。
  - 5 round中3 roundが遅い（持続的な遅さ）は不合格。
- 入力検証: `--rounds` が0・1・2・4なら `ValueError`、CLIでは終了コード2。
- 安全不変条件: 軽量engineが危険候補を返すとRuntimeErrorになり、reportを書かない。
- 既存 `tests/test_noisy_provider.py` は変更なしで合格。
- 実測: CIの `performance-ci` 両OSと、ローカルで既定設定の実行（所要時間とround p95をPRへ記録）。

## 完了条件
- 予算値・multiplierを変えずに、単発の外れroundで不合格にならない判定になっている。
- 処理量2倍の退行が不合格になることを回帰テストが示し、`finish_task.bat` が全段合格。
- 判定方法・根拠・限界が `doc/E2E受入試験手順.md` に記録されている。

## 現状調査
- 現行は1回の測定（warm-up 8、測定128 step）のnearest-rank p95（128件中122番目）で判定する。7 step（5%超）の遅延が同じ時間帯に重なるとp95が予算を超える。ローカル（Windows）実測はp50 86ms・p95 188msで、尾部はSQLite journalへの書込み待ちが支配的と考えられ、ディスク/runner負荷の影響を受けやすい。
- 失敗例: PR #732 ubuntu 150.66ms/150ms、PR #724 Windows 430ms/225ms（再実行で成功）、PR #717 Windows（再実行で成功）。
- `tools/performance_check.py` はwarm-up後の複数batch測定で単発値を避けている（`doc/UPDCommander導入方針.md`）。同じ「複数回測定で単発値に依存しない」方針をNoisy Providerにも適用する。
- 再利用: `TimedNoisyLanguageProvider`、`measure_engine_steps`、`percentile_ms`、`run_out_of_set_fixture` はそのまま使う。

## 代替案と不採用理由
- 予算値・multiplierを緩める: Issueが不可としている。検出感度も下がる。
- 最良round（最小値）で判定: 5 round中1 roundだけ速ければ合格し、持続的な退行や間欠的な退行を見逃しやすい。中央値は過半数のroundの悪化を要求するので、持続的な退行を検出しつつ少数roundの外れを除ける。
- 同一runnerの基準計測との比で判定: 基準となる処理を別途定義する必要があり、runner依存の比の揺らぎも加わる。今回は不要な複雑さとして不採用（必要なら後続）。
- 1回のrun内でp95をp90等へ下げる: 予算の意味を変え、尾部退行の検出を弱めるため不採用。
- CIでの自動再実行: 失敗原因を隠し、所要時間も読みにくくなるため不採用。

## 範囲外・残件
- 既定5 roundで測定時間は約5倍（ローカルWindowsで約17秒→約85秒、CIはより短い見込み）。所要時間が問題になればstep数の調整を別Issueで扱う。
- runner全体が測定期間中ずっと遅い場合は原理的に退行と区別できないため、従来どおり失敗する（再実行で判断）。
- `tools/performance_check.py` 側の判定方法は今回変更しない。
