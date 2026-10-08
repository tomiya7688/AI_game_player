# Versions

## Unreleased

- Issue #332: Ollamaの画面評価・候補判断をUI thread外で実行し、推論中の停止を受理する。実行中のキー保持・待機もRunControllerの停止状態とstep開始世代を監視して中断し、保持キーを解除する。停止後・再開後に返った旧推論結果は判断履歴と実入力へ進めず、停止理由をログへ記録する。mouse方式の連続実行では、入力workerがSetCursorPosの開始・完了とクリック先をUIへ通知し、自動カーソル移動を手動移動による停止と誤認しない。未通知の手動カーソル移動による停止は維持する。

- PR #345: 重複した画面評価workerは起動順IDで新旧を判定し、最新より古い結果を観測履歴・評価表示へ反映しない。古い評価結果が現在有効な連続実行stepに属する場合は、結果待ちでループが止まったままになることを避けるため連続実行を停止する。

- PR #345: 判断履歴と判断traceを一時ファイルへ先に準備し、RunControllerが実行世代を検証してから両ファイルとエンジンの前回判断状態を公開する。Stopが一時ファイル準備中に届いた場合、workerは未公開データを破棄する。

- Issue #336: 実入力開始時に対象HWND/PIDの選択・有効性を検証し、HWND再利用による別プロセスへの入力を実行直前に拒否する。連続実行は対象未選択で開始せず、各ステップ失敗時に停止する。

- Issue #137: Tkinter UIを上部Command/Status + 左Navigator / 中央Workspace mount points / 右Inspector / 下部Bottom Panelへ分離。Play/Vision/Reasoning/Evaluation/History/Mods間を切り替え、Inspector/Bottom Panelをresize・開閉可能。Workspaceと表示状態を`data/shell_state.json`へ独立保存し、実行制御は既存Applicationから停止commandとして接続。

- Issue #313: ai-context-reducerのTask Routing / Remote Delta Firstを開発入口へ適用。Issue一覧は本文なしのmetadataだけを全ページ取得し、P0-P5・親Issue除外・明示指定で対象1件の本文のみ取得。文字数上限・省略表示・原典ポインタ・失敗時の古いpack再利用禁止と回帰テストを追加。AST索引/Test Impactは別Issueのまま。

- Issue #232: Native Runtime C ABI v1の薄いPython loader/binding、ABI versionとstruct検証、init/shutdown所有権、batch/status変換、明示的availability、Linux/Windows実共有ライブラリ結合テストを追加。capture/inputやbuffer実装・GUI自動切替は含まない。

- Issue #148: C ABI v1へsize/version付き初期化options、opaque runtime handleのinit/shutdown lifecycle、安定status code、未実装のcoarse batch入口をfail closedする契約とC/C++ contract testを追加。Issue #86の段階導入方針に沿い、mypyは依存moduleの型推論を維持しつつ、指定root 6ファイルのstrict検査へ範囲を明示。

- Issue #182: Learning Capabilityのversioned schemaに沿うExperience Dataset Builder、5種のtyped sample、provenance/label・sensitive artifact制御、dedup、再現可能split、JSON export/importとcontent digestを追加。

- Issue #168: JSONL Experience Readerの任意Step/検索index、snapshot/delta再構築、schema v0移行、破損tail回復、checkpoint検証とArtifact参照検査を追加。

- Issue #167: SHA-256 content-addressed Artifact Store、atomic publication、artifact type metadata schema、dedup/integrity/orphan検査とlocal benchmarkを追加。

- Issue #166: Episode/Step/Event canonical Experience schema、source/model provenance、sensitive artifact referencesと論理Reader APIを追加。

- Issue #181: Model learning capability、Dataset/Trainer compatibility、artifact provenanceおよびuntrainable状態を表すversioned contractとJSON Schemasを追加。

- Issue #220: Capability Manifestを使うComposition Resolver、preferred/default選択、dependency/health検査、fallbackとdegraded reason、provider lifecycle cleanupを追加。

- Issue #219: Optional feature capability manifestのJSON Schema、runtime検証、required/optional dependency・lifecycle・config namespace・fallback/degraded mode宣言と検証を追加。

- Issue #94: HWNDごとの選択・隠し内容取得を追加。可視で覆われていない対象だけ画面領域から取得し、隠れた／覆われた対象はタイムアウト付きWM_PRINTへ切り替える。WM_PRINT失敗時は明示的に失敗し、別ウィンドウ画素へ誤フォールバックしないことを検証。

- Windows Capture integrationで最小化・非表示のテスト用HWNDでもフレーム寸法とBGRA契約が維持されることを確認し、内容取得は保証しない制約を明記する。

- Windows CaptureのWin32/GDIハンドル取得失敗を明示し、途中まで確保したDC/Bitmapを解放する失敗経路をfake APIで検証する。

- Windows Captureを連続実行してもプロセスのGDIオブジェクト数が増加しないことをCIで検証する。

- ScreenFrameにプロセス内で比較可能なmonotonic取得時刻を追加し、Windows Capture連続取得で非減少を検証する。

- Windows Capture CIでテスト用非表示HWNDのresize、破棄後の失効検知、新しいHWNDでの再取得を検証する。

- Windows Captureは無効または失効したHWNDをフレーム化せず明示的に失敗することをCIで確認する。

- Windows CIで実デスクトップを読み取り専用でキャプチャし、BGRAフレーム契約とCapturedObservationSourceへの接続を検証する。

- Safety CIでビルドしたNative shared libraryをPython ctypesから実際にロードし、許可/拒否判定までWindows/Linux双方で結合検証する。

- Native Safety Validatorのconfigured library loading、ctypes argtypes/restype、Action/Result structure転送と戻り値変換をfake native ABIで検証する。

- global Windows keybd_event/mouse_eventのrelease失敗時もheld状態とTTL ledgerを保持し、release-all再試行で回復できることをfake APIで検証する。

- window-message key/mouse-up失敗時にheld状態とTTL ledgerを維持し、release-allで対象windowへUPを再送して回復する。window-message modeからglobal OS inputへ漏れないよう修正。

- Windows window-message APIがdownを拒否した場合に、key/buttonのheld状態とinput ledgerを残さず、OS実入力も発生しないことをfake User32で検証する。

- Windows Safetyのrelease-allをfake User32とin-memory input ledgerで検証し、held key/buttonの解放APIとledger消去を確認する。

- WindowsTargetProbeのHWND/PID、可視性/前景状態、window/client geometryをfake User32で検証する安全なintegration回帰を追加。

- Windows Safety CIにUser32をfake化したwindow-to-screen/client座標変換回帰テストを追加し、OSへ実入力せず座標契約を検証する。

- strict mypy対象をDecisionPipeline/CandidateMergerまで拡張し、入力候補の共変な読み取り型を明示した。

- strict mypy対象にWindowsInputExecutorを追加し、ActionExecutorからWindows実入力までの実行境界を検査対象にした。

- strict mypy対象をaction_executor.pyまで拡張し、注入executor契約と現在のtarget戻り値を明示した。

- strict mypy対象を共有モデルからSafetyGuard境界へ段階拡張し、TargetProbe契約とNative ctypes動的境界を明示した。

- Python unit testsのbranch coverageをCI/ローカルで計測し、HTML/XML artifactと70% baseline floorを追加。バグ修正の再現テスト記載ルールをPR templateとworkflowへ反映した。

- Ubuntu Native CIにASan/UBSanとGCC静的解析経路を追加し、C++テストのassertをReleaseでも有効化してAPI null境界を検証する。

- Ruffの基本lint・共有モデルのstrict mypy・Python構文検証をPR CIおよびfinish_task.batへ追加し、開発依存として固定した。

- Fail-safe Runtime / OS Emergency Stopを追加し、startup SAFE_IDLE、explicit re-arm、short lease/heartbeat、Observation freshness、epoch/sequence/session/target binding、bounded command age/queue、held-input TTL、crash-consistent journal、LLM/GPU非依存のout-of-process watchdogでcrash/OOM/通信断/capture/target/storage異常をfail closedする。
- Before Observation + Action + After ObservationをStateDelta / ScreenDiff / Temporal / Terminal textの独立Evidenceとして検出し、conflict / confidence / provenance / abstentionを保持するdeterministic Outcome Fusionを追加する。低信頼時だけSemantic Providerへfallbackし、OutcomeEventを評価プリミティブへ接続する。
- UPD Commander Base Designを段階導入し、新規Quality ApplicationでCommander / Messenger / Processingの責務境界を適用する。外部UPD checkerはcommit SHA固定でCI実行し、通常ゲームruntimeへchecker依存や追加処理を持ち込まない。
- FrameAnalyzer / CandidateMerger / DecisionContextBuilder / SafetyGuardのCPU hot pathをbudget監視するperformance-ciとローカルperformance checkerを追加する。
- Python構文・mutable default・finally return・重複定義/辞書key・到達不能except・literal identity比較を高確度で検査するbug-ciとローカルbug checkerを追加する。
- Windows上でNative RuntimeとPythonアプリを1つのPyInstaller bundleとして組み立て、生成された`Kadoka.exe --smoke-test`が実際に起動完了することをCIで継続検証する。正式ビルド入口として`tools/build_windows_bundle.ps1`を追加する。
- 方針で採用・候補化した実装言語向けCIを追加し、Python 3.10/3.14、C++20 Native RuntimeのLinux/Windowsビルドを継続検証する。Lua / .NET / JavaScript・TypeScriptは対応ソースまたはmanifest追加時に自動で検査jobを有効化する。
- 高水準言語とC++ Native Runtimeを分離するためのruntime capability contract、registry、C ABIスケルトン、architecture/profile配置を追加する。
- 一般ユーザー向けはself-containedで最小操作、上級ユーザー向けはversioned provider/policy/profile等で拡張可能とする設計方針をAGENTS.mdへ反映する。
- 明るい連結領域から、OCRに依存しない低信頼度の画像由来操作候補を生成・統合する。

- 操作候補に任意の矩形情報を保持し、CandidateMergerがIoUで重複候補を統合できるようにする。

- 図表生成・整合性確認・テストを一括実行するfinish_task.batと、CI用の生成物チェックを追加する。

- Codex向けの最小コンテキスト入口としてAGENTS.mdを追加し、Issue中心の参照ルールを定義する。

- 実入力許可時にGUIの実行ボタン・連続実行ボタン・状態表示を実入力用へ切り替える。
- window_message方式の実入力前に対象ウィンドウ選択を検証する。
- 対象ウィンドウのキャプチャに失敗した場合、連続実行を停止する。
- RuleProviderの状態評価をローカルのOutcomeEvaluatorへ統一し、Rule経路でネットワークアクセスしないようにする。
