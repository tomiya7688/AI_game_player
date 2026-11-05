Spec-Status: READY（今回のスライス。Harness・判定・report・fast gate・Windows統合の測定まで。実`SessionResourceRegistry`との結線は#786の実装後、実機30分runの結線は#250側の残件）
Depends-On: none（今回のスライス）。#786は`ResourceLifecycleSnapshot` v1の**形だけ**に依存し、実装完了は待たない。実Registry結線は#786の実装後、実機runは#250
Decision-Gaps: none（閾値・判定規則・層構成は本書で決定。根拠は「未決事項と決定」）
Parent: #785

# Issue #787 Long-session Resource Stability Gate

長時間のSessionで資源使用量が**単調増加し続けない**ことを、peakだけでなくwarmup後の傾き・Session終了後の回収・複数Session反復のbaselineで判定するHarness / Gateの設計。#786のRegistryが「所有と解放」を保証する側、本Issueは「実際に増え続けていない」ことを外から測って落とす側。測れない資源を成功扱いしない。

## 現状調査

### 既存の関連実装（再利用し、置き換えない）

| 既存 | 内容 | 本Issueでの扱い |
|---|---|---|
| `tools/performance_check.py`（`performance-ci.yml`） | CPU hot pathの時間予算（`config/performance_budgets.json`、Windowsはmultiplier 1.5）。資源量は測らない | 触らない。時間予算とは別のGateにする。**multiplierのような緩和引数は作らない** |
| `tools/run_noisy_provider_benchmark.py`（#811） | 独立した複数round、「roundごとのnearest-rank p95の中央値」で一時揺らぎに耐える | 「独立した複数区間の中央値で判定」「p95はnearest-rank」の考え方と用語を再利用 |
| `tools/planning_resource_monitor.py` | 50msのprocess tree RSS標本（`psutil`のimportは関数内、`pyproject.toml`の依存に無い）。開発者比較用で「OS hard budgetの証拠ではない」と明記 | 流用しない。`psutil`を依存に加えず、標準ライブラリ（`ctypes`・`/proc`）で採る（理由は未決事項3） |
| `tools/check_gpu_vram_budget.py` | `nvidia-smi --query-gpu=memory.used`の**device合計**peakだけ。`parse_memory_used_mib`でN/Aを`None`扱い | 解析関数の考え方（N/A→測定不能）を再利用。process単位のVRAMは`--query-compute-apps`で別に採る |
| `tests/test_windows_capture_integration.py` `test_repeated_capture_releases_gdi_resources` | `GetGuiResources(GR_GDIOBJECTS)`をwarmup後のsteady stateと**等値比較** | GDI/USER採取の呼出し（`ctypes`の型定義）を共通probeへ移す。この試験は変更しない（#334の正本） |
| `tools/run_2048_campaign.py` | `--profile pr`（600秒）/`release`（1800秒）、`--duration-seconds`で上書き。1 processで複数episode（起動・終了の反復） | 外側から観測する対象。campaign本体は**改変しない** |
| `language-ci.yml` `windows-e2e` | `windows-latest`、`timeout-minutes: 20`、campaignを実行しartifactを保存 | Gateでcampaign commandを包む（実装時。今回は設計のみ） |
| `python-security-ci.yml` | `schedule`（週次cron）と`workflow_dispatch`の前例 | deep validationの置き場の前例 |
| `fail_safe_runtime.py` `_process_alive` / `tools/run_fail_safe_ci.py` | 子processの生存確認、watchdog注入 | 孤児process検出（終了後の子孫生存）の考え方を再利用 |
| #786 `ResourceLifecycleSnapshot` v1（設計書、PR #823） | `active_count` / `releasing_count` / `active_by_kind{count,known_bytes,unknown_count}` / `counters`（`release_failed`等）/ `max_cleanup_latency_ms`、`schema_version=1` | metric入力の正本。#786が「#787は値とschema_versionを固定するだけ」と定めている接続点 |

### 不足していること

1. 資源**量**を時系列で採って、傾き・終了後回収・反復baselineを判定する仕組みが無い（既存は単発のpeakまたは単一等値比較）。
2. machine-readableな共通metric schemaが無く、#250の実機runが保存する形が決まっていない。
3. 「測れない」と「0」を区別する表現が無い（`check_gpu_vram_budget.py`は未測定を`None`で返すが、集計は無い）。
4. 意図的に保持する資源（model warm cache等）と実リークを区別する宣言が無い。
5. Stop/error後に資源が残る経路を、CIで継続して落とす負例が無い。

## 今回のスライスと範囲外

**今回**: (1)純粋な判定core、(2)OS probeと資源gauge、(3)fakeな資源所有者によるfast gateと負例canary、(4)process外からcampaignを観測するintegration層（Windows、`windows-e2e`の既存campaign runを包む）、(5)report schema・sample schema・offline評価（#250の入力）、(6)CI組込み方針。

**範囲外**（残件へ）: 実`SessionResourceRegistry`との結線テスト（#786実装後）、#250の実機30分harness本体とGPU実測（#250）、Experience Artifact Storeのdisk GC（#18、`storage.*`はmetric語彙に**入れない**）、Nativeの確保/copy counter自体の実装（無ければ`unsupported`）、既存のGDI/ Handle解放の修正（#334）、branch protection / rulesetの変更（owner作業）。

## 成果物

| ファイル | 内容 |
|---|---|
| `src/ai_game_player/runtime/resource_stability.py`（新規、純粋） | `MetricId`語彙、`ResourceSample`、`StabilityProfile`（検証付き読込）、統計（nearest-rank、区間中央値、Theil-Sen傾き）、`evaluate_run()`、`build_report()`。**I/Oなし**（時計・probeは引数） |
| `src/ai_game_player/runtime/resource_probes.py`（新規） | `ProcessGauges`（RSS/private/handle/GDI/USER、自process・任意pid、Windows=`ctypes`、Linux=`/proc`）、`ProcessTreeGauges`、`TempTreeGauge`、`NvidiaSmiGauge`、`TracemallocGauge`、`RegistrySnapshotGauges`（`ResourceLifecycleSnapshot` v1の辞書を受ける） |
| `config/resource_stability_profile.json`（新規） | 層（fast/integration/real_hardware）× 実行profile（fast/pr/deep）ごとの必須metric・許容値・warmup・区間数。上限（hard limit）はコード側に持つ |
| `config/resource_stability_report.schema.json` / `config/resource_stability_sample.schema.json`（新規） | report v1 / samples行 v1のJSON Schema |
| `tools/run_resource_stability.py`（新規） | CLI。`run` / `evaluate` / `list-scenarios` |
| `tools/resource_stability_scenarios.py`（新規） | fake資源世界（lease・handle・GDI・temp・bytes・queue）と、pass期待のscenarioおよびcanary（fail期待）scenario |
| `tests/test_resource_stability.py` / `tests/test_resource_probes.py` / `tests/test_resource_stability_cli.py`（新規） | 下記「テスト」 |
| `doc/architecture/resource_stability_gate.md`、`doc/E2E受入試験手順.md`の節、`doc/versions.md` | 実装PRで更新 |
| `.github/workflows/resource-stability-ci.yml`（新規）、`language-ci.yml` `windows-e2e`の1 step | CI組込み（「CIへの組み込み」） |

`runtime/resource_lifecycle.py`（#786）には**import依存しない**。Gateは`snapshot()`が返す辞書（または同形のfake）だけを読む。

## 入出力

### metric語彙（固定。語彙外のIDはprofile読込で拒否）

| ID | 単位・種別 | Windows | Linux | 判定 |
|---|---|---|---|---|
| `mem.resident_bytes` | bytes | `GetProcessMemoryInfo`の`WorkingSetSize` | `/proc/<pid>/status`の`VmRSS` | 判定 |
| `mem.private_bytes` | bytes | 同`PROCESS_MEMORY_COUNTERS_EX.PrivateUsage` | `/proc/<pid>/smaps_rollup`の`Private_Clean+Private_Dirty`（無ければ`unsupported: platform`） | 判定 |
| `mem.os_peak_bytes` | bytes | `PeakWorkingSetSize` | `VmHWM` | 診断（標本間のspike確認用。傾き判定に使わない） |
| `py.heap_traced_bytes` | bytes | `tracemalloc.get_traced_memory()[0]`（`--python-heap`指定時のみ有効化） | 同 | 診断（Issue: 唯一の判定根拠にしない） |
| `native.alloc_count` / `native.copy_count` | count | Native Runtimeが公開した場合のみ | 同 | 既存計測が無いため既定は`unsupported: no_counter`。公開後にprofileで`required`へ |
| `gpu.process_vram_bytes` | bytes | `nvidia-smi --query-compute-apps=pid,used_memory`（対象pid合計。WDDMで`[N/A]`なら`unsupported: probe_error`） | 同 | real_hardwareで判定 |
| `gpu.device_vram_bytes` | bytes | `--query-gpu=memory.used`の合計 | 同 | 診断。scenarioが`exclusive_gpu: true`を宣言した時だけ判定（他processの影響を受けるため） |
| `os.handle_count` | count | `GetProcessHandleCount` | `/proc/<pid>/fd`の件数 | 判定 |
| `os.gdi_objects` / `os.user_objects` | count | `GetGuiResources(GR_GDIOBJECTS=0 / GR_USEROBJECTS=1)` | `unsupported: platform` | Windowsで判定 |
| `registry.active_leases` / `registry.releasing_leases` / `registry.failed_leases` | count | snapshotの`active_count` / `releasing_count` / `counters.release_failed` | 同 | 判定（許容0固定） |
| `registry.kind.<kind>.count` / `.bytes` | count / bytes | snapshotの`active_by_kind[kind]`。shared memory・ResourceRef・model cacheはここ | 同 | 判定 |
| `registry.max_cleanup_latency_ms` | ms | snapshotの`max_cleanup_latency_ms` | 同 | 上限判定（cleanup latency。傾きは見ない） |
| `temp.file_count` / `temp.bytes` | count / bytes | 宣言したtemp root（通常はrun専用の`TEMP`/`TMP`上書きdirectory）をsymlinkを辿らず走査 | 同 | 判定。走査は上限（既定20000件・200ms）、超過は`unsupported: scan_truncated` |
| `queue.length.<name>` / `task.pending_count` | count | scenarioが渡すgauge callable | 同 | gaugeが無ければ`unsupported: no_counter` |

- 各metricは`source`を持つ: `process`（OS API）/ `external`（process外から観測）/ `registry_snapshot` / `synthetic`（fast gateのfake世界）。reportの各metricへ必ず書き、**syntheticの値を実測として扱わない**。
- 範囲外の`storage.*`（Experience Store、#18）は語彙に無い。混ぜようとしたprofileは`profile_invalid`。
- process tree観測（integration層）は、観測対象rootの子孫をWindowsは`CreateToolhelp32Snapshot`、Linuxは`/proc/*/stat`のppidで辿り、bytes・handle・GDI・USERを木全体の合計で記録する。GDI/USERの取得に`PROCESS_QUERY_INFORMATION`が無い子は、その標本の当該metricを`None`（欠測）にする。

### unsupportedの表現（固定語彙）

`status`: `measured` / `unsupported`。`unsupported.reason`: `platform` / `no_counter` / `tool_missing` / `access_denied` / `probe_error` / `registry_not_connected` / `snapshot_version` / `scan_truncated`。**0・空・成功への読み替えを禁止**。標本の値は`int`または`None`で、`None`を0にしない。

### ResourceSample（samples.jsonl 1行、`resource-stability-sample/v1`）

```text
{schema, run_id, session_index, phase, step, t_monotonic_ns, values: {metric_id: int|null}}
phase: warmup | steady | closing | post_close | pre_open
```

`values`の`null`は欠測。`pre_open`はSession開始直前、`post_close`は`close_session()`後に`settle`（`gc.collect()`×2相当をscenarioが行い、最大`settle_ms`、既定500ms、2連続標本が許容内になるまで）した後の標本。

### StabilityProfile（`config/resource_stability_profile.json`）

```text
{schema: "resource-stability-profile/v1",
 layers: { fast|integration|real_hardware: {
   run_profiles: { fast|pr|deep: { warmup_steps, warmup_seconds, steady_min_steps, segments (4..8),
                                   min_samples_per_segment (>=8), sample_every_steps | interval_seconds,
                                   sessions (>=4) , settle_ms } },
   metrics: { <metric_id>: { required_on: ["windows"|"linux"|"any"], class: "bytes"|"count"|"latency",
                             tolerance: {rel?, abs}, recovery: {rel?, abs}, ceiling?: number } } } },
 enforcement: { fast: "enforce", integration: "observe"|"enforce", real_hardware: "enforce" } }
```

- 許容値の**上限（hard limit）はコードに持つ**: bytesの`rel`≤0.05・`abs`≤64MiB、`os.handle_count`≤64、GDI/USER≤8、`registry.*`は0固定、`temp.file_count`≤16。profileがこれを超えると読込時に`profile_invalid`（fail-closed）。上限を上げるには設計書とコードを変える（レビュー対象）。
- **緩和用のCLI引数・環境変数は作らない**（`--budget-multiplier`相当を持たない）。ホストの違い（Windows runnerの揺らぎ）は`required_on`とhost別の同一hard limit内の値で表す。
- 初期値（実装PRで各OSのCI実測3回以上を`doc/E2E受入試験手順.md`へ記録して確定。**実測で不安定でも許容値を上げる前にscenarioを直す**）:

| class | tolerance | recovery（post_close − pre_open、およびbaseline drift） |
|---|---|---|
| bytes（mem.*, gpu.*） | `max(8MiB, 2% × S2中央値)` | `max(16MiB, 3% × pre_open)` |
| `temp.bytes` / `registry.kind.*.bytes` | `max(1MiB, 2%)` | `max(1MiB, 2%)` |
| `os.handle_count` | 16 | 16 |
| `os.gdi_objects` / `os.user_objects` | 0（既存試験が等値のため） | 0 |
| `temp.file_count` | 0 | 0 |
| `registry.*`（active/releasing/failed、kind count） | 0 | 0 |
| `queue.*` / `task.pending_count` | 2 | 2 |
| `registry.max_cleanup_latency_ms` | 上限`2000`（#786の`close_session`既定budgetと同じ。傾きは見ない） | — |

### report（`resource-stability-report/v1`、`build/resource-stability/report.json`）

Issueの証拠項目と1対1:

```text
{schema, generated_at_utc,
 runtime: {python, implementation, os, machine},         # runtime/version
 code: {commit (GITHUB_SHA または git rev-parse、無ければ "unknown" とreason), dirty?},
 host: {profile_id, cpu_count, gpu: {name, driver, memory_total_mib}|null},
 run: {layer, run_profile, scenario, seed, enforcement, gate_effective: bool,
       duration_seconds, steps, sessions},
 warmup: {boundary_step, boundary_seconds, declared_fill_steps},
 sampling: {mode: steps|seconds, interval, samples_total, samples_steady},
 metrics: { <id>: {status, unsupported_reason?, source, class, unit, required: bool, judged: bool,
                   peak, p50, p95, end, end_minus_start, tail_growth, tail_slope_per_1000_steps,
                   segment_medians[], declared_ceiling?, tolerance, verdict: pass|fail|observed|unsupported,
                   reasons[]} },
 sessions: [{index, pre_open, post_close, recovery_delta_by_metric, cleanup_latency_ms}],
 baseline_drift: {<id>: {post_close_series[], delta_last_minus_second, verdict}},
 coverage: {judged_metrics[], observed_only_metrics[], unsupported_required[], unsupported_optional[]},
 verdict: pass|fail,
 reasons: [{code, metric?, session?, observed, limit}] }
```

- 欠測・unsupportedは`coverage`へ必ず列挙する。`reasons.code`の語彙: `tail_growth` / `ceiling_exceeded` / `not_reclaimed` / `baseline_drift` / `baseline_staircase` / `metric_unsupported` / `metric_unreliable` / `no_judged_metrics` / `insufficient_samples` / `insufficient_sessions` / `orphan_process` / `canary_not_detected` / `scenario_error` / `profile_invalid`。
- reportは数値・ID・版情報のみ。environment変数、絶対path、user名、frame・画像・OCR文字、command引数の値を含めない（pathはrepoからの相対、commandは固定ラベルとexit code）。書込みは`tools/`側で`build/`配下、`src/`のcoreはファイルを書かない（Privacy data inventory #224のcoverage testの走査対象を増やさない）。
- samples.jsonlは`build/resource-stability/samples.jsonl`。上限`max_samples`（既定20000）。超えそうな長時間runは**事前に間隔を広げて**決め（`sampling.interval`に記録）、後から黙って間引かない。

### CLI（`tools/run_resource_stability.py`）

```text
run --layer fast|integration --run-profile fast|pr|deep [--scenario NAME ...] [--seed N]
    [--mode enforce|observe] [--output PATH] [--samples PATH] [--python-heap]
    [-- COMMAND ...]            # integration層: 観測対象command（例 campaign）
evaluate --samples PATH --layer real_hardware --run-profile deep --host-profile ID --output PATH
list-scenarios
```

- 終了コード: 0=pass（またはobserveでfail）、1=fail（verdictがfail、canaryが検出されない、観測対象commandが非0）、2=引数・profile・入力の不正。`observe`でも`verdict`と`reasons`はreportに残し、`gate_effective=false`、GitHub Actionsでは`::warning`で失敗理由を出す。
- `COMMAND`の終了コードは`scenario_error`の根拠として記録し、Gateの終了コードに潰さず別fieldに残す。runnerは観測対象を自分で起動した場合だけ、timeout時にその木を終了する（他processは触らない）。

## 手順

### 責務境界

```text
scenario / 観測対象 ──(gauge callable / pid)──▶ resource_probes ──▶ ResourceSample列(JSONL)
                                                          │
                                         resource_stability.evaluate_run(profile, samples, declarations)
                                                          │
                                      report.json（verdict, reasons）──▶ CLI 終了コード / CI artifact
```

判定coreは時計・OS・fileに触れない純関数（同じsamplesなら同じreport）。probeは値を返すだけで判定しない。

### 判定アルゴリズム（metricごと・session ごと）

1. **妥当性**: profileとsamplesを検証。`insufficient_sessions`（`sessions`がprofile未満）、`insufficient_samples`（steadyが`segments × min_samples_per_segment`未満）はfail（少ないdataで合格させない）。
2. **warmup分離**: steadyの開始は`max(warmup_steps, warmup_seconds相当, scenario宣言のdeclared_fill_steps)`。warmup中の増加は傾き判定に使わないが、peakとceilingの判定には含める。bounded cacheの充填完了時刻は宣言（`declared_fill_steps`）で渡し、warmupを超えて充填が続く宣言は`profile_invalid`にしない代わりに、steady区間で増えれば下記規則で落ちる。
3. **区間分割**: steady標本を`segments`（既定4）個の連続区間S1..SKに分け、区間ごとに中央値を保存（`segment_medians`）。
4. **統計**: `peak`（全phase）、`p50`、`p95`（nearest-rank、#811と同じ）、`end`（SKの中央値）、`end_minus_start`（SK中央値 − S1中央値）、`tail_growth`（SK中央値 − S⌈K/2⌉中央値）、`tail_slope`（後半区間の標本を最大100個の中央値binにまとめたTheil-Sen傾き、単位は1000step当たり。時間基準runは1分当たりを併記）。
5. **成長判定**: `tol`はmetric classの許容。`drift = tail_slope × 後半のstep幅`とし、**`tail_growth > tol` かつ `drift > tol` ならfail（`tail_growth`）。どちらか片方だけでも`2×tol`を超えたらfail**。二つの独立な推定量（区間中央値の差とTheil-Sen）が一致したときに落とすことで、単発spikeによる誤failを避け、片方でも明確に大きければ見逃さない。
6. **上限**: `ceiling`（profile、またはscenarioが宣言したbounded cacheの宣言上限）に対し`peak ≤ ceiling`。超過は`ceiling_exceeded`。ceiling未宣言のmetricには上限判定をしない（成長判定のみ）。
7. **Session回収**（sessionごと、`closing`→`post_close`）: `post_close − pre_open ≤ recovery`。`declared_retained`（process寿命で意図的に保持する資源。例: model warm cacheの`registry.kind.model_cache.bytes`）は宣言量までを許し、宣言を超えた分と、**宣言していない保持**は`not_reclaimed`。Registry系は`active_leases == 0`かつ`failed_leases`が増えていないことを厳密に要求。
8. **Session反復baseline**: 先頭Session（prime、lazy import・初回cache）の`post_close`は診断値`first_session_growth`として記録するだけで判定しない。2番目以降の`post_close`系列P2..PMについて、`PM − P2 > recovery`なら`baseline_drift`。さらに差分が全て正で、2回以上連続し、合計が`recovery/2`を超える階段状の増加も`baseline_staircase`でfail（`sessions ≥ 4`）。
9. **cleanup latency**: Session close要求から`registry.active_leases==0`の確認（またはsettle完了）までをmsで記録。`registry.max_cleanup_latency_ms`は上限判定、runner側計測値は報告のみ。
10. **欠測**: `required`のmetricが`unsupported`なら`metric_unsupported`でfail。steady標本の`None`が5%を超えるrequired metricは`metric_unreliable`でfail。optionalは`unsupported_optional`へ列挙して判定から外す。**判定したmetricが0件なら`no_judged_metrics`でfail**（全部unsupportedで通る経路を作らない）。
11. **孤児**（integration層）: 観測対象command終了後、最後の標本で見えていた子孫processが生存していれば`orphan_process`。
12. `verdict = pass`は、上記のfail理由が0件で、かつrequired metricが全て`measured`のときだけ。

### 実行層

| 層 | 実行内容 | 資源の源 | 判定対象 | 実行時間・場所 |
|---|---|---|---|---|
| fast | 決定論的fake世界を数千〜数万stepで反復（既定2万step × 5 session）。`clock_ns`はstep駆動の疑似時計 | `synthetic`（lease・handle・GDI・USER・temp・queue・bytesのfake counter）。加えて`real_probe_smoke`は実`os.handle_count`・`temp.file_count`（実file）を3 session × 1500 stepで判定 | registry系・fake counter・実handle/temp。**実RSS/privateはfastでは`observed`のみ**（runner負荷で揺れるため。integration層で判定） | 各OS 60秒以内（実装で実測を記録）。PRで常時 |
| integration | `windows-e2e`の既存campaign commandを`run -- COMMAND`で包み、process treeを`interval_seconds`（既定1.0、0.25..10）で標本化 | `external` | mem.resident/private、handle、GDI、USER、temp、孤児 | PRは既存`--profile pr`の600秒をそのまま観測（追加時間0）。deepは`--profile release`の1800秒、weekly cronとworkflow_dispatch |
| real_hardware | #250のharnessが同じ`ResourceSample`をJSONLへ保存し、`evaluate`で判定。または`resource_stability`をlibraryとして呼ぶ | `process`/`external`/`registry_snapshot` | integrationの集合 + `gpu.process_vram_bytes` | #250側（本Issueはschemaと`evaluate`の提供まで） |

- fake世界は`FakeResourceWorld`: 各stepでleaseの取得・解放、handle/GDI/USERの確保・解放、temp fileの作成・削除、boundedなcacheを行い、scenarioが「N stepごとに1つ解放しない」等の欠陥を注入する。乱数は`random.Random(seed)`のみ（時計・環境に依存しない）。
- **canary**: fast gateは、pass期待scenario（`clean` / `bounded_cache_plateau` / `pinned_model_cache` / `stop_mid_step_clean` / `error_mid_step_clean` / `real_probe_smoke`）に加えて、**fail期待のcanary scenario**（`leak_lease_per_step` / `leak_handle` / `leak_gdi` / `leak_temp_file` / `stop_leaves_resource` / `error_leaves_resource` / `baseline_staircase`）を毎回実行し、期待した`reasons.code`でfailすることを確認する。canaryがpassしたら`canary_not_detected`でCIを落とす。判定が静かに緩むことをCIで検出する。

### CIへの組み込み方（設計のみ。今回のPRはworkflowもrulesetも変更しない）

1. 新規`.github/workflows/resource-stability-ci.yml`（`permissions: contents: read`、`persist-credentials: false`、既存workflowと同じ書式）。
   - job `Resource stability fast gate (${{ matrix.os }})`: `ubuntu-latest`と`windows-latest`。`PYTHONPATH=src python tools/run_resource_stability.py run --layer fast --run-profile fast`。標準ライブラリのみでpip installしないため、trust floor（#514）の「pip install前」規則に抵触しない。artifactは`resource-stability-fast-${{ matrix.os }}`（report.json・samples.jsonl、`if-no-files-found: error`）。
   - job `Resource stability deep (windows-latest)`: `schedule`（週次、既存security CIと重ならない時刻）と`workflow_dispatch`のみ。`--layer fast --run-profile deep`（10倍step）と、campaign `--profile release`の観測。`timeout-minutes: 60`。
2. `language-ci.yml` `windows-e2e`: 「Run the 600-second pinned-game campaign」stepのcommandを`python tools/run_resource_stability.py run --layer integration --run-profile pr --mode observe -- python -u -X faulthandler tools/run_2048_campaign.py ...`へ変更（campaignの引数は不変）。最初の1リリース相当は`observe`（reportと`::warning`のみ、終了コードはcampaignの結果）で、**CIの実測3回以上をPRに記録し、許容値の根拠を確定してから**profileの`enforcement.integration`を`enforce`へ1行で切り替える（切替は別PR、完了条件で追跡）。canaryとfast層は最初から`enforce`なので、判定ロジック自体は最初から落とせる。
3. required status check（ブランチ保護/ruleset）への追加候補名は上記job名。**rulesetはowner作業のため変更しない**。実装PRでIssueへ候補名を提示し、追加はownerの判断（#92と同じ扱い）。

## テスト

全て決定論的でネットワーク不要。`tests/test_resource_stability.py`（core）、`tests/test_resource_probes.py`（probe）、`tests/test_resource_stability_cli.py`（CLI・schema・scenario）。

| # | 受入条件・必須負例 | 試験 |
|---|---|---|
| T1 | 1 lease/stepを解放しないfake ownerでfail（必須負例1） | `leak_lease_per_step` 2万step×5 sessionで`registry.active_leases`が`tail_growth`でfail、CLI終了コード1。fast gateが60秒以内（疑似時計でなく実時間も測り記録） |
| T2 | 欠陥なしはpass | `clean`が全required metric `measured`でpass、終了コード0 |
| T3 | 決定論 | 同seed・同profileで2回実行し、`generated_at_utc`を除いたreportが一致。異なるseedでも`clean`はpass |
| T4 | bounded cacheの上限内plateauはpass（必須負例） | 充填後に一定値でplateauするcache（`declared_ceiling`あり、`declared_fill_steps`がwarmup内）が`peak ≤ ceiling`でpass。同じ最終値へ到達する**実リーク**（steadyでも増え続ける）は`tail_growth`でfail |
| T5 | 小さな持続リークを検出 | 100stepに1つだけ増えるleakを`tail_growth`でfail。単発spike（1標本だけ巨大）は片方の推定量だけが反応するため`2×tol`未満ならpass、`2×tol`超ならfail（境界の両側を試験） |
| T6 | 意図的保持はdeclared pinned/baselineとして誤failしない（必須負例） | `pinned_model_cache`: model cacheをsession間で保持し、`declared_retained`内ならpass。**宣言量を超える保持**と**宣言なしの保持**は`not_reclaimed`でfail |
| T7 | Session closeで回収を検証 | 各sessionの`post_close − pre_open`が`recovery`内でpass。close後にlease 1件残る注入で`not_reclaimed` |
| T8 | Stop後に資源が残るケースでfail（必須負例） | `stop_mid_step_clean`はpass、`stop_leaves_resource`（step途中のstopで確保済みhandleを解放しない）は`not_reclaimed` |
| T9 | error後に資源が残るケースでfail（必須負例） | `error_mid_step_clean`はpass、`error_leaves_resource`（例外経路で解放しない）は`not_reclaimed` |
| T10 | 反復Sessionのbaseline drift | 5 sessionで`post_close`が1 sessionごとに階段状に増える`baseline_staircase`でfail。primeだけが大きく以降一定のscenarioはpass（`first_session_growth`は報告のみ） |
| T11 | 測定不能を成功扱いしない（required） | required metricをunsupportedにして`metric_unsupported`でfail、`unsupported_required`に列挙。`None`を0に変換していないことを、値0のmetricと`None`のmetricで結果が異なることで確認 |
| T12 | optionalのunsupportedは列挙して判定から外す | `unsupported_optional`に理由付きで載り、他のmetricの判定は有効 |
| T13 | 全部unsupportedでは通らない | 全metricがunsupportedなら`no_judged_metrics`でfail |
| T14 | GDI/handle leak fake counterでfail（必須負例） | `leak_handle`（`os.handle_count`）・`leak_gdi`（`os.gdi_objects`、許容0）がそれぞれ`tail_growth`でfail |
| T15 | temp file leakでfail（必須負例） | `leak_temp_file`: 実fileを一定stepごとに作って消さないと`temp.file_count`が`tail_growth`でfail。走査上限超過は`scan_truncated`のunsupportedでrequiredなら`metric_unsupported`。symlinkを辿らない |
| T16 | 実probe（platform別） | `skipUnless(sys.platform=="win32")`: 自processのRSS/private/handle/GDI/USERが正の`int`、`GetGuiResources`の戻り0を`unsupported`にしない。`skipUnless(linux)`: `VmRSS`・`fd`件数・smaps_rollup。他OSでは当該metricが`unsupported: platform`。nvidia-smi無しは`tool_missing`、`[N/A]`は`probe_error`（出力文字列を差し替えたfake実行で試験） |
| T17 | 欠測5%超 | steadyの`None`が5%超のrequired metricは`metric_unreliable`、5%以下は欠測を除いて判定 |
| T18 | 不十分なdata | steady標本が`segments×min_samples_per_segment`未満は`insufficient_samples`、sessionsがprofile未満は`insufficient_sessions` |
| T19 | profileのhard limit | 許容値がhard limit超（bytes rel 0.06、GDI 9、`registry.*`に0以外、語彙外ID、`storage.*`）は`profile_invalid`で終了コード2。CLIに緩和引数・環境変数が無いことを`--help`の項目一覧で確認 |
| T20 | machine-readable evidence | reportが`resource_stability_report.schema.json`に適合（`jsonschema`）。runtime/commit/host/scenario/seed/duration/steps/sessions/metric support/warmup/sample interval/peak・p50・p95・end・slope/declared ceilings/cleanup latency/reasonsが全て存在。`source`が全metricにある |
| T21 | 機微情報を含まない | reportとsamplesの文字列にenvironment値・絶対path・user名・command引数値が無い（`TEMP`/`USERNAME`を一意な値へ設定して検索） |
| T22 | #250へ同じschemaを渡せる | #250相当のsamples.jsonl（sample schemaで検証）を`evaluate --layer real_hardware`へ渡し、fast層と**同じ構造・同じ判定関数**でreportが出る。`gpu.process_vram_bytes`がrequiredで欠測なら`metric_unsupported` |
| T23 | #786 snapshot v1契約 | #786設計書の`ResourceLifecycleSnapshot` v1と同形の固定fixtureから`registry.*`が読める。`schema_version`が1以外・必須keyなしは`unsupported: snapshot_version`（0扱いしない） |
| T24 | canary | CLIの`run --layer fast`が全canaryで期待codeのfailを確認して終了コード0。判定を緩めた偽の`evaluate_run`を差し込むと`canary_not_detected`で終了コード1 |
| T25 | integration層（Windows、`skipUnless`） | 短い子process（メモリ・handleを一定に保つ数秒のscript）をtree観測してpass、handleを増やし続ける子はfail、終了後に子孫を残すscriptは`orphan_process`。`--mode observe`は終了コード0でreportはfail、`::warning`出力 |
| T26 | 既存互換 | `performance-ci`・`test_windows_capture_integration`・campaign関連の既存テストが変更なしで通る。`tests/test_data_inventory.py`のcoverage testが通る（`src/`に保存箇所を増やさない） |

## Issueの受入条件との対応

| Issue完了条件 | 設計箇所 | 試験 |
|---|---|---|
| fast deterministic leak regression testがある | 実行層fast、fake世界、canary | T1, T2, T3, T24 |
| repeated-session baseline driftを検出できる | 判定8 | T10 |
| RAM/handle/tempと、利用可能ならVRAM/GDIを測れる | metric語彙、probe | T14, T15, T16, T25 |
| bounded cache plateauと実リークを区別できる | 判定2・5・6、`declared_fill_steps` | T4, T5, T6 |
| Stop/error/session close後の回収を検証する | 判定7 | T7, T8, T9 |
| machine-readable evidenceを保存する | report schema、samples.jsonl | T20, T21 |
| #250 real-hardware harnessへ同じmetric schemaを渡せる | `evaluate`、sample schema、real_hardware層 | T22 |
| 測定不能resourceを成功扱いしない | unsupported語彙、判定10 | T11, T12, T13, T17 |

Issueの「測定対象」との対応: process RSS/working set/private bytes=`mem.*`、Python heap=`py.heap_traced_bytes`（診断のみ）、Native counter=`native.*`（既存計測が無いため`no_counter`）、VRAM/loaded model cache=`gpu.*`と`registry.kind.model_cache.*`、GDI/USER/handle=`os.*`、shared memory/ResourceRef=`registry.kind.*.count`、temp/file-backed=`temp.*`、queue/pending=`queue.*`/`task.pending_count`、Registry active/failed=`registry.*`、Experience Store=`storage.*`は語彙に入れず#18へ分離。

Issueの「判定方法」1〜6との対応: 1(warmup分離)=判定2、2(複数区間)=判定3、3(peak/median/p95/end-start/slope)=判定4、4(宣言上限と実測)=判定6、5(close後のbaseline帯)=判定7、6(3回以上の反復)=判定8。「固定のRAM上限だけにしない」は、成長・回収・反復の3種の判定を主とし、ceilingは宣言したbounded資源にだけ使う点で満たす。

## 設計レビュー

（レビュー後に記録する）

## 代替案と不採用理由

- **`psutil`を依存に加える**: process treeとhandleを簡潔に採れるが、依存と信頼floor（#514）・constraintsの管理が増える。必要なAPI（`GetProcessMemoryInfo`・`GetProcessHandleCount`・`GetGuiResources`・`/proc`）は標準ライブラリだけで書け、すでに`tests`が同じ`ctypes`呼出しを使っている。
- **固定上限（RSSは常にX MB以下）を主判定にする**: 機種・モデルで変わる値を固定すると、緩和（上限を上げる）で通せてしまい、増え続ける挙動を見逃す。Issueも否定している。固定値はbounded資源の宣言ceilingにだけ使う。
- **全標本での線形回帰（最小二乗）**: spikeと外れ値に弱い。区間中央値とTheil-Senの二つを突き合わせる方が小さな持続リークとspikeを分けられる。
- **peakだけの比較**: warmup後に増え続けるleakは、peakがceiling内でも長時間で必ず超える。終了後回収と反復baselineが必要。
- **fast gateで実RSSを判定**: 共有runnerの負荷で数十MB動き、許容を上げる圧力が生じる。決定論的なfake counterで判定ロジックを固定し、実RSSはintegration層に限る。
- **campaign本体にsampling hookを入れる**: campaignの責務（#459系）を増やし衝突しやすい。外部observerならcampaignは不変で、同じ方法が#250にも使える。
- **integrationを最初からenforce**: 実測前の許容値でmain CIを赤くし得る。ただしfast層とcanaryで判定ロジックは最初から強制し、integrationの切替条件（実測3回以上）を完了条件へ置く。
- **Python GCやfinalizerによる回収を前提にする**: #786と同じく、正常解放の唯一の契約にしない。fake世界は明示的な解放経路だけで検証し、`gc.collect()`はsettle時の安定化にだけ使う（leakの隠蔽には使わない: close前のbaselineと比べる）。

## 未決事項と決定（技術的に決まるため自分で決めた）

1. **#786が未実装でも設計を進める**: Spec-Statusは本来BLOCKEDだが、必要なのは`ResourceLifecycleSnapshot` v1の形だけで、#786設計書が「#787は値とschema_versionを固定するだけ」と接続点を固定している。Gateは辞書を読むadapterとfixtureで先に作れる。実Registryとの結線は#786実装後の残件（Issueとして追跡）。
2. **integration層をobserveから始める**: 根拠は「代替案」の通り。最初から強制する条件は(a)判定ロジックの正しさ（fast層・canaryで強制）と(b)許容値の根拠（実測が必要）が別のため、(b)だけ後から確定する。切替は`enforcement.integration`の1行で、profileにだけ現れ、緩和方向の変更ではない。切替条件: 同一HEADを含む連続3回のCI実測がpassし、値をdocに記録。
3. **標準ライブラリのprobe**: `psutil`不採用（代替案）。Linuxのprivate bytesは`smaps_rollup`（kernel 4.14以降）、無ければ`unsupported: platform`で、`VmData`等で代用して成功にしない。
4. **VRAMはprocess単位を既定の判定、device合計は診断**: 共有GPUでは他processの影響が混ざる。`exclusive_gpu: true`を宣言した実機runだけdeviceを判定に昇格。WDDMでprocess単位が`N/A`なら`unsupported`のまま（deviceを黙って代用しない）。GitHub-hosted Windows runnerはGPUが無く`tool_missing`でoptional。
5. **区間数4・各8標本以上・Theil-Senは100 bin**: 区間は「前半を捨てて後半だけで増加を見る」最小の単位で、nearest-rankのp95が意味を持つ最小標本数（8）に合わせた。Theil-Senは100 binで最大4950組、20万標本でも一定コスト。
6. **grow判定の「両者一致、または片方が2×」**: 片方だけなら単発spike（中央値は反応しにくい）か極端に遅いdrift（Theil-Senは反応）で、`2×tol`は「明らかに許容を超える」境界として置いた。T5が両側を試験する。実測で調整する場合も境界の係数2はhard limitとして変えない（変更は設計の改訂）。
7. **cleanup latencyの定義**: runner側は`close`要求から`registry.active_leases==0`確認（またはsettle完了）までの実時間(ms)、Registry側は`max_cleanup_latency_ms`。前者は報告のみ、後者は上限2000ms（#786の`close_session`既定budgetと一致）で判定。
8. **samplesを間引かない**: 長時間runは事前に間隔を決め`sampling.interval`へ記録。上限`max_samples`を超える設定は`profile_invalid`。後から間引くと傾きが歪み、再現も難しい。
9. **canaryをCIで常時実行**: 単体テストだけだと、profile変更や判定の緩みはテストを書き換えない限り検出されない。CIで毎回「落ちるべきものが落ちる」ことを確認する。

## 範囲外・残件

- 実`SessionResourceRegistry`・`CleanupCoordinator`を使った結線テスト（#786実装後に別Issue）。
- #250の実機30分harness、`gpu.process_vram_bytes`の実測、実ゲームのhost profile。
- `native.alloc_count` / `native.copy_count`の公開（Native Runtime側。公開されたらprofileでrequired化）。
- Experience Artifact Storeのdisk GC指標（#18）。
- required status checkへの追加（owner作業、ruleset）。
- integration層`enforce`への切替（実測3回以上の後、別PR）。
- 実機以外のゲーム/ブラウザ個別の許容値調整は、scenario単位のprofile追加で扱う（hard limit内）。

## 完了条件

- 上表T1〜T26が通り、`finish_task.bat`が全段passする。
- fast gateが`ubuntu-latest`と`windows-latest`でそれぞれ60秒以内に終わることを実測して記録する。
- canary全件が期待どおりfailし、pass期待scenarioが全てpassする。
- `windows-e2e`のcampaignをintegration層で観測し、reportとwarningが出る（observe）。許容値の根拠となる実測3回以上を`doc/E2E受入試験手順.md`へ記録する。
- 文書（`doc/architecture/resource_stability_gate.md`、`doc/E2E受入試験手順.md`、`doc/versions.md`、`generated/repo_map.json`）を同じPRで更新し、Issue #787へ実装PRと残件（実Registry結線、#250結線、integrationのenforce切替、required checks候補名）をコメントする。
