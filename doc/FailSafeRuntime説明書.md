# Fail-safe Runtime / OS Emergency Stop

## 目的

Issue #40 のFail-safe Runtimeは、Actionの意味や通常の入力妥当性ではなく、**上位AIや制御processそのものが壊れた場合にも入力権限を失効させる**ための実行基盤である。

安全層は次の順序で分離する。

```text
Recognition / Decision
  -> Action Safety (#22: 意味的リスク)
  -> SafetyGuard (#33: Action直前の決定論的hard guard)
  -> FailSafeRuntime (#40: lease / freshness / epoch / watchdog)
  -> Executor
  -> OS / Game
```

#40の基本式は次の通り。

```text
No fresh lease
or No fresh observation
or No valid target
or No healthy runtime
=> No input
```

LLM、GPU、Experience DB、semantic evaluatorは停止判断に必要ない。

## 状態

`FailSafeState` は4状態を持つ。

- `SAFE_IDLE`: 起動直後および正常終了後。入力不可。
- `ACTIVE`: 明示re-arm後かつleaseが有効な間だけ入力資格を持つ。
- `EMERGENCY_STOP`: ユーザーまたはSafety層による緊急停止。入力不可。
- `RECOVERY_REQUIRED`: crash、lease失効、capture/target/storage異常等を検出した状態。明示re-armまで入力不可。

起動時に `ACTIVE` を復元しない。前回journalが `ACTIVE` / `EMERGENCY_STOP` / `RECOVERY_REQUIRED` だった場合、新processは `RECOVERY_REQUIRED` から開始する。

## 明示re-arm

通常の `RunController` は起動時にrunning状態でも、`rearm_token=0` である。これは「処理ループを実行できる」ことと「OS入力権限を与えた」ことを分離するためである。

`RunController.start()` / `DecisionPipeline.rearm_safety()` の明示操作だけがtokenを増加させ、そのtokenを見たPipelineがFailSafeRuntimeをre-armする。

re-arm時には以下を新規発行する。

- control `epoch`
- `session_id`
- target PID / HWND binding
- short control lease

re-arm直後でもcapture freshnessは未成立であり、最初の新しいObservationを取得するまでOS入力は拒否される。

## Lease / heartbeat

既定のcontrol leaseは0.75秒である。親process内のdependency-minimal heartbeat threadがleaseを短周期で更新する。

外部watchdogも同じlease fileを読む。外部watchdogが `RECOVERY_REQUIRED` を書いた場合、親heartbeatはdisk stateを読み戻し、ACTIVEで上書きせず自身もRecoveryへ移る。

heartbeatは「processが生きている」信号であって、capture freshnessの代わりにはならない。

## Observation freshness

Observationは既定2秒で失効する。Pipelineが実際にsourceからObservationを取得した時だけ `record_observation()` を更新する。

したがってAI processとheartbeatだけが動き続けても、captureが停止した場合は入力できない。

## Command contract

各OS入力直前に `FailSafeCommand` を生成し、次を検証する。

1. runtimeがACTIVEか
2. control leaseがfreshか
3. Observationがfreshか
4. epochが一致するか
5. command ageが上限内か
6. commandが最新Observationへbindされているか
7. sequenceが単調増加しているか
8. target PID / HWNDがre-arm時と同じか
9. session_idが一致するか
10. in-flight queueが上限内か

同じ候補Actionをゲーム上の必要性から繰り返すことは許可する。replay防止はstableな`action_id`の永久denyではなく、`epoch + sequence + issued_at + observation_at` で行う。

## Target validation

#33 SafetyGuardはAction直前にHWND/PID/foreground/client areaを検証する。#40はさらにre-arm時のPID/HWNDをcontrol epochへbindし、command時に同じtargetであることを要求する。

外部watchdogはWindowsでHWNDが同じPIDへ所属しているかも独立確認する。target processまたはwindowが失われれば `RECOVERY_REQUIRED` へ移る。

## Input Ledger / hold TTL

`InputLedger` はheld key / mouse buttonを別processから読めるatomic JSONへ保存する。

OSへkey-down/mouse-downを送る**前**にledgerへ記録する。これにより、ledger記録後にcrashした場合は余分なkey-upが起こり得るが、OSへdownを送った後にledgerが無い状態は作らない。

各entryはTTLを持つ。親processが生きていてもTTL超過したheld inputを外部watchdogがreleaseする。Emergency Stop / Recovery / parent lossではTTLを待たず全releaseする。

## Out-of-process watchdog

re-arm時にAI processとは別processでwatchdogを起動する。

開発時:

```text
python -m ai_game_player --failsafe-watchdog <state-dir>
```

PyInstaller bundle時:

```text
Kadoka.exe --failsafe-watchdog <state-dir>
```

watchdog pathはGUI、LLM、GPU、OCR、semantic providerをimportせず、Python標準ライブラリとOS APIだけで動作する。

監視対象:

- owner PID
- target PID
- Windows target HWND/PID association
- lease expiry
- Observation expiry
- Input Ledger TTL

親AI processをkillしてcleanup callbackが一切走らなくても、watchdog自身がlease/owner lossを検出してheld inputをreleaseする。

## Crash-consistent state

`AtomicJsonStore` は次の順序でcritical stateを更新する。

1. 同directoryのtemporary fileへwrite
2. flush
3. file `fsync`
4. `os.replace` でatomic replace
5. 対応OSで可能ならdirectory `fsync`

対象:

- `journal.json`: critical runtime state / reason / epoch
- `lease.json`: current lease / freshness / target / sequence
- `input_ledger.json`: held input

storage writeが失敗した場合はfail-openせず `RECOVERY_REQUIRED` へ移り、入力解放を試みる。

## Local Windows / remote Windows共通contract

現在の実装はlocal Windows Executorへ直接接続するが、lease/command contract自体はcontrollerの言語や配置に依存しない。

Linux controller -> Windows input agent構成でも、Windows側agentが次を所有すれば同じcontractを使える。

- epoch/session
- short lease
- latest Observation timestamp
- target PID/HWND
- command sequence/age
- Input Ledger
- watchdog

remote通信断はheartbeat/lease lossとして扱い、自動再開しない。

## Failure injection CI

専用 `fail-safe-ci` でUbuntu/Windowsの両方を検証する。

### Runtime contract

- startup SAFE_IDLE
- explicit re-arm
- restart後RECOVERY_REQUIRED
- stale epoch/session/sequence/command age/Observation rejection
- queue bound
- target mismatch
- capture freshness loss
- storage write failure
- Input Ledger TTL

### Out-of-process failure injection

- owner process kill
- simulated OOM相当のparent突然死
- network/heartbeat loss
- capture freshness loss
- target process loss
- held input TTL expiry

実OOMをCI runnerへ発生させること自体はrunner破壊につながるため、OOMケースは「親processがcleanupなしで突然終了する」という安全上同じ境界をprocess killで注入する。

## #33との境界

`SafetyGuard`はAction内容、座標、system key、rate/repeat/session、target foreground等を同期的に検査する。

`FailSafeRuntime`はAI processの外側まで含むcontrol authorityの生存性を検査する。

どちらか一方がallowしても、他方がblockならOS入力しない。

## #56との境界

Issue #56 Hybrid / Native Runtimeでは、このcontractのうち高頻度・OS密着部分をC++ Native Runtimeへ移す余地がある。

#40ではまず言語に依存しないstate/lease/command契約とfailure semanticsを固定する。Native化しても次の性質は変更しない。

- startup fail-closed
- short lease
- explicit re-arm
- stale/replay rejection
- held input TTL
- out-of-process supervision

## 保証しないもの

電源断そのものの最中にkey-up callbackを実行できることは保証しない。OSが停止している間のcleanupは不可能である。

保証対象は、OSが稼働している限りAI/controller processがcrash/OOM/deadlock/通信断してもleaseを更新できないため入力権限が失効し、独立watchdogがrelease処理を行うことである。
