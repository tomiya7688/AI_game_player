# 実行受領記録と操作後観測

Issue #628は、保存した判断だけを直前の実操作として扱っていた帰属の誤りを修正する。操作結果は、実行境界の受領記録と、明示的な操作後観測の組で保存する。ゲーム固有の候補生成や探索戦略をこの境界に持ち込まない。

## 契約と接続

`ExecutionReceipt` は不変のdataclassで、`snapshot_id`、`action_id`、`action_generation`、入力前の `screen_id/state_signature`、`execution_status`、`input_dispatch_started`、`eligible_for_learning`、監査理由を持つ。元のplanned traceと参照が一致しなければ登録を拒否する。同じreceiptの再登録は冪等、矛盾した上書きは拒否する。

`action_generation` はStop/rearmの照合用で、操作ごとの連番ではない。Experience集計の重複排除に用いる `correlation_sequence` は、#422のprojectorがSession内で単調増加させる別のaction連番であり、両者の一致を要求しない。

| execution_status | 送信根拠 | pending/学習での扱い |
|---|---|---|
| `executed` | backendが正常完了し、入力境界到達を確認 | 有効世代だけpendingと実操作履歴へ採用 |
| `dispatch_unknown` | 入力境界後の失敗、またはcustom backendで送信有無が不明 | pendingに保持してunknown観測を監査。学習対象外 |
| `not_executed` | 入力境界前の失敗 | pendingを変更しない |
| `not_dispatched` | wait等が正常完了し、OS入力なし | pendingを変更しない |
| `dry_run` | 意図的に入力を行わない | pendingを変更しない |

`input_dispatch_started` のFalseは未到達、Trueは入力API呼出し境界へ到達、Noneは送信有無が不明を表す。TrueでもOSやゲームが入力を受理したこと、操作が有効だったことは保証しない。`WindowsInputExecutor` はexecute開始で信号をresetし、対象検証・座標変換・ledger登録等の前処理を終えてから、最初の入力API直前でTrueにする。API例外は保守的に送信不明とする。`has_held_inputs` はowned key/button集合の有無だけをI/Oなしで返し、処理中の有無は別途確認する。

`GamePlayerEngine.step()` はplanned traceだけを公開する。`DecisionPipeline.run_and_execute()` が実行結果からreceiptを作り、世代guard下で登録する。送信事実を確認した実操作だけが次のpendingになる。純粋な `run()`、dry-run、入力前失敗は過去の実操作を上書きしない。停止/rearmが入力完了と競合した場合は、旧世代traceに `eligible_for_learning=False` の監査事実を直列化して残し、新世代pendingへは採用しない。

操作後観測は `pipeline.complete_execution_observation(observation, expected_rearm_token=token)` で完了する。Engine側APIは `action_generation` と公開用guardを受け取る。同世代のpendingとbefore/after観測から検出し、guard下で元traceへ一度だけ保存する。`ExecutionObservation` はreceipt・`OutcomeEvent`・after画面参照を返す。検出中または公開済み結果の再取得中に世代が変わった場合も公開を拒否する。次の実操作が登録されるまで観測済みpendingは保持し、二重検出を防ぐ。

呼出経路は次のとおり。

- 実入力pipelineの新しい観測取得後、次判断の前。次Providerが失敗しても直前結果を失わない。
- `GameSessionController` のexecute後観測。終端・反復停止・runtime closeより前に完了する。
- E2E Runnerの実行後probe、2048 campaignの最新観測。次操作がない最終actionも保存する。

2048 campaignの初回観測は実操作前の初期化で、安全制御を開始して実行世代が進むため、操作後のcompletionへ渡さない。後続の観測は取得前の世代を保持し、取得中のStop/rearmを現在世代へ読み替えず拒否する。独立したepisodeの再起動でも同じ初期化境界を使う。

`DecisionTraceStore.recent_actions()` は完全なreceiptとtrace参照、正常送信、学習可否を検証し、必要なら世代も限定する。planned/legacy不明を実操作扱いにせず、別操作の `previous_outcome` を当該操作の結果へ転用しない。Sessionの既定Journalは追記を使う。互換用JSON保存は遅延appendを世代guard内で最新配列へ適用し、計画準備から公開までに追加された入力auditを消さない。互換JSONは履歴全体の読書きが必要なため、長期SessionはJournalを使用する。

## 効果学習との境界

receiptは送信事実を確定するもので、因果的な成功ラベルではない。画面が変わった、得点が自動増加した、Modelが成功と述べた、という情報だけではverified action-effectを作らない。得点表示のクリックも候補として禁止せず、明示的な効果証拠を検証できた場合にsuccess/no_effect/failureへ投影する。送信不明とunknownを正負の実績へ混ぜない。軽量知識への集計・適用・昇格は #530/#531/#532、継続runnerは #422 の契約で扱う。

配置は既存PythonのEngine/Journal/RunController呼出境界に合わせた。入力APIそのものや画像処理を増やさず、小さな受領metadataを操作一回/観測一回で渡す。新しいFFIや保存基盤を作らず、既存停止lock・Journal・atomic JSONを使う。Native入力実装へ移行する場合も同じ受領契約を保つ。

## 検証範囲

Core回帰はdecision-only、dry-run、過去の実操作を残した入力前失敗、送信不明、末尾action、次判断失敗、停止/rearm競合、実行auditと遅延trace公開の競合、世代混入、partial/別参照receipt拒否を検査する。最初のdecision-only/dry-run/pending上書きと遅延公開競合は修正前に失敗することを確認した。

Runtime回帰は実Sessionの終端観測、既存Pipeline/E2E/campaign seam、Windows入力APIのfakeによる前処理失敗/送信後失敗/信号resetを検査する。Linuxの仮想displayでGUIを含む全テストを実行する。Windows WGC・Win32実入力と配布物での実ゲーム受入れはこの環境ではUnverifiedであり、この修正だけで汎用ゲームの因果認識やプレイ精度が完成したとは扱わない。

2026-11-01の補助実プレイでは、固定版Tetris（`4e497d1c858914f0a1f0818698029d1c7dad090b`）とBreakout（`4961fece5e9bfb21450b71d1bb8cb3efc8d19338`）をローカルHTTPで隔離Chromiumへ表示し、共通FrameAnalyzer/OCR・無変更RuleProvider・ActionExecutorでクリックした。手動候補やゲーム固有policyは追加していない。Tetrisは1操作後に許可候補がなく停止し、最後のreceiptとunknown結果が一致した。Breakoutは3操作すべてに同世代receiptとunchanged結果が保存された。クリックの有効性やプレイ能力の向上を示す結果ではなく、実画面での帰属確認と現行認識/探索の限界を示す。検証時のtraceと報告は `/tmp/628-tetris-play/`、`/tmp/628-breakout-play/` に保存した（開発環境の一時成果物）。

main側で並行更新中の #619 flow/architecture/README、#622 judgment、#623 safety、#624 records_learning は、統合時にこのreceipt/dispatch契約と整合させる。旧資料の「未送信判断も参照し得る」「dispatch flagは入力API到達の証拠でない」という現状記載は本修正の統合後に更新する。
