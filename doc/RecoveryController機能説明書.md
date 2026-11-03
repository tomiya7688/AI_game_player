# Recovery Controllerの機能説明書

## この説明書の対象と目的

この説明書は、Issue #38の`SelfCheckMonitor`が返す`SelfCheckEvent`を受けて、再観測・再判断・停止要求を有限回で段階的に進める`RecoveryController`（Issue #205）の契約を説明します。対象読者は、Session loopへSelf-Checkを接続する実装者とレビュー担当者です。

`RecoveryController`は「不自然なので考え直す」経路だけを担当します。異常の検出は`SelfCheckMonitor`、入力可否の判断は`SafetyGuard`・Verifier、停止の実行は`RunController`・`GameSessionController`の責務で、`RecoveryController`はそれらを置き換えません。

## 構成

| 型 | 場所 | 責務 |
| --- | --- | --- |
| `RecoveryPolicy` | `src/ai_game_player/recovery_models.py` | 回数・時間・cooldown・連続失敗の上限。既定値は`max_reobserve=2`、`max_redecide=1`、`deadline_seconds=3.0`、`cooldown_steps=2`、`max_scene_failures=2` |
| `RecoveryPlan` | 同上 | 次の行動（`REOBSERVE`/`REDECIDE`/`ABSTAIN`/`REQUEST_STOP`）、型付き理由`RecoveryReason`、残予算、期限、行動世代、Scene、Snapshot、attempt番号、停止理由文。`recovery.plan/v1`へ変換できる |
| `RecoveryAttemptResult` | 同上 | 呼出側が実行したattemptの結果（`RESOLVED`/`UNRESOLVED`/`FAILED`）と、完了時点のSnapshot。`recovery.attempt/v1`へ変換できる |
| `RecoveryController` | `src/ai_game_player/recovery_controller.py` | Session単位の純粋な状態機械。時刻は引数で注入し、実入力・モデル推論・GUI callbackを呼ばない |
| `RecoverySessionAdapter` | `src/ai_game_player/recovery_session_adapter.py` | planとattempt結果をEvent Journal（`recovery.plan`/`recovery.attempt`）へ記録し、`REQUEST_STOP`だけを停止要求関数（例: `GameSessionController.stop`）へ渡す |

## 呼出し方

1. Self-Checkイベントを受けたら`on_event(event, snapshot_id=..., action_generation=RunController.rearm_token, now_monotonic=..., stop_requested=not RunController.is_running)`を呼びます。
2. `REOBSERVE`なら新しいFrameを取得し、`REDECIDE`なら`plan.snapshot_id`のFrameで既存のDecisionPipeline/Verifier/Safety経路により判断し直します。実入力は必ずその経路を通します。
3. 結果を`RecoveryAttemptResult`にして`ack(result, now_monotonic=..., stop_requested=...)`へ返し、返された次のplanに従います。
4. `SelfCheckMonitor.reset()`でstep数を0へ戻した場合は、`RecoveryController.reset()`も呼びます。再armは`RunController.start()`で世代が増えた後のイベントでだけ起き、Recovery側の停止要求の保持もそこで解除されます。
5. Controllerはlockで保護しますが、`RecoverySessionAdapter`はSession loopの1 threadから呼ぶ前提です（plan決定とJournal記録・停止要求の順序をAdapter側で直列化していません）。
6. 結果が返らない場合に備えて、loopの各tickで`poll(now_monotonic=...)`を呼びます。期限を過ぎた結果待ちattemptはepisode失敗として打ち切られます。

## 決定規則

判定は次の優先順で行い、同じ入力列からは常に同じplan列を返します。

1. **最優先の拒否**: 旧世代（`action_generation`が既知の世代より小さい）は`STALE_EPOCH`。既存Stop（`stop_requested=True`）は`STOP_LATCHED`のABSTAINとして記録し、停止理由を置き換えず、再arm（世代の増加）まで新しいRecoveryを開始しません。自身が停止要求済みなら、同じ停止理由で`REQUEST_STOP`を返し続けます。Target喪失は`TARGET_LOST`でepisodeを破棄します。
2. **進行無し**: 受け付けた最大step数より古いイベントと、同じ（Scene, category, step数）のイベントは`DUPLICATE_EVENT`、解消・失敗直後の`cooldown_steps`未満は`COOLDOWN`、結果待ち中の新イベントは`PLAN_PENDING`。いずれもABSTAINで、予算を消費しません。
3. **Scene変更**: 別Sceneのイベントで、前Sceneのepisode・連続失敗数・cooldownを破棄します。
4. **CRITICAL**: `SelfCheckSeverity.CRITICAL`は直ちに`REQUEST_STOP`（`CRITICAL_SELF_CHECK`）。イベントのseverityから新しい入力候補は作りません。
5. **WARNING**: episodeを開始して`REOBSERVE`。再観測で新しいSnapshotを得た場合だけ「再観測済み」とし、そのSnapshotで`REDECIDE`します。新しいFrameが来なければ再観測予算だけを消費し、同じFrameで再判断を繰り返しません。`RESOLVED`は、episode開始時ともplanの基準Snapshotとも異なるSnapshotで報告された場合だけ解消として扱います。`ack`は、結果待ちplanと行動・attempt番号・行動世代がすべて一致する結果だけを受け付け、結果の世代で再armしません。
6. **打切り**: 1 episodeのattemptは最大`max_reobserve + max_redecide`回。予算切れは`BUDGET_EXHAUSTED`、`now_monotonic >= deadline`は`DEADLINE_EXCEEDED`でepisode失敗です。同一Sceneの連続失敗が`max_scene_failures`に達すると`REQUEST_STOP`（`SCENE_FAILURES_EXCEEDED`、停止理由文に直前の失敗理由を含む）、それ未満はABSTAINで通常経路へ戻します。

時刻が非有限または逆行した場合、世代が負の場合、Snapshotが空の場合は`ValueError`です。

## 記録と失敗時の扱い

`RecoverySessionAdapter`は、各planを`recovery.plan`（status=action、payload=`recovery.plan/v1`）、各attempt結果を`recovery.attempt`（payload=`recovery.attempt/v1`）としてJournalへ追記し、同じepisodeを`correlation_id`（`recovery:<世代>:<Scene>:<event_ref>`）で結び付けます。Journalへの記録に失敗した場合は、記録の無いRecoveryを続けないため`JOURNAL_FAILED`の`REQUEST_STOP`へ昇格し、停止要求を必ず渡します。既存Stopが要求済みのときは停止要求関数を呼びません。

## 現在の接続範囲と残り

- 今回のPRでは、`SelfCheckMonitor`自体がまだSession loopへ接続されていないため、`GameSessionController`の連続loopへ自動接続していません。`RecoverySessionAdapter`の停止要求先に`GameSessionController.stop`を渡した場合に、Sessionが`STOPPED`になり`ensure_running()`が以後の実行を拒否することをテストで確認しています。
- 接続点は、loop observerの結果から`SelfCheckSample`を作って`SelfCheckMonitor.observe()`を呼ぶ箇所（`GameSessionController._apply_loop_observation`付近）です。`REOBSERVE`/`REDECIDE`を次stepの観測・判断へ反映する処理と、`ManagedSessionRuntime`のJournalを渡す処理は後続作業です。
- 性能の参考値（開発PC、20,000イベント、`on_event`のみ）: p50 15.4µs、p95 42.0µs、最大427µs。tracemallocのheapは約0.8MB（測定用の時間配列を含む）。保持する重複判定履歴は最大64件です。

## 試験

`tests/test_recovery_controller.py`は、2step反復→再観測→新Frame→再判断→解消、無効な再観測2回→ABSTAIN→同Scene再失敗でSTOP、CRITICAL→STOPと再armまでの保持、既存Stopの非置換、別Sceneでのreset、同一Frameでの再判断禁止、状態未更新での解消扱い禁止、重複・結果待ち、期限境界（2.999秒は継続・3.0秒で打切り）、結果が返らないworkerのpoll打切りと遅延ackの拒否、合法候補なしの失敗、旧世代・Target喪失・想定外ack、入力検証、予算0、seed 20本×500操作で「attempt数が上限以下」「Stop中に行動要求0」を確認する乱択試験、実`EventJournal`への記録、Journal失敗時の停止要求、`GameSessionController`への停止要求を検証します。
