# Versions

## Unreleased

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
