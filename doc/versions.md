# Versions

## Unreleased

- Issue #122: 現行実装と未実装の対策を区別したThreat Modelを追加する。ゲーム画面由来の間接Prompt Injection、UI偽装・誤対象、Provider / Remote data、Plugin、Artifact、path / archive、supply-chain、Resource exhaustion、反復入力を信頼境界ごとに記録し、各脅威のprevent / detect / contain / recover、残余リスク、関連test・CI・担当領域、更新条件を示す。Provider endpointはloopback既定だが画面から変更できること、Remote consent・Plugin隔離・外部Artifact intake・main required-check設定が未確認または未実装であることを明記する。
- Issue #77: Contributor向けSubsystem Ownership Mapを追加し、9つのAreaについて現行コード、契約境界、関連Issue、作業スキル、レビュー感度、未指名の担当候補を整理する。READMEから地図へ移動できるようにし、未指名の領域別レビュアーを推測でCODEOWNERSへ割り当てない方針を記録する。現行リポジトリに存在しない`plugins/`の案内をruntime architectureから除き、MOD / Processing Hookの実装Issueを示す。
- Issue #247: `CampaignRunner`に600秒PR/1800秒Releaseのprofile、対象ゲームごとに差し替える`CampaignEpisode`契約、deadlineまでのlive-input閉ループ、任意の`CampaignState` milestone callback、game-over後のepisode再起動、回数制限付きstall recoveryとstep上限を追加。callbackはaction後の状態を判定し、到達・判定失敗をstop reasonへ分ける。状態遷移をJSONLへ追記し、stop reason/failure category、milestone有効・到達状態、episode/restart/recovery数、last/peak score・score観測数・max tile、action-to-observation latency、Gitとpinned-game metadataをJSON reportへ保存する。2048のUP/DOWN/LEFT/RIGHT候補は対応するWindows仮想キーコードでNative SafetyGuardへ渡す。2048 episodeは画面状態を取得した後にFailSafeRuntimeの観測完了時刻を更新する。Windows CIで記録したaction-to-observationの最大値2,666msを超える猶予を設けるため、episodeの観測期限は5秒とする。5秒間観測時刻が更新されなければwatchdogが追加の実入力を拒否する。2048 CLIは専用browser HWNDへのwindow-message inputを使い、CIでPR profileを実行する。偽episodeの単体テストでduration、milestone、live-input条件、game-over restart、bounded recovery、hard failure分類、既存artifact保護を検証し、矢印キー変換、観測時刻の更新、FailSafe停止理由のartifact記録も単体テストする。
- Issue #245: MIT版2048のソースを固定commitから取得するsource lockへGit tree SHAと全34ファイルのGit blob SHAを記録し、cacheに欠落・追加・変更されたファイルがあれば再利用を拒否する。検証済みsourceはloopback限定HTTP serverと専用Edge/Chrome profileでWindows上に起動し、launcherは起動したPIDと2048のwindow titleで対象を識別してdesktop内に最大1180×850で配置する。Windowsのforeground制限で前面化できない場合も起動を失敗させず、その状態をJSON出力へ記録する。終了時は起動したbrowser process tree・一時profile・HTTP serverを閉じ、検証済みsource cacheは再利用のため保持する。
- Issue #246: pinned 2048画面のCSS色と4x4配置からboardを復元し、同じcapture時点のOCR Observationから現在scoreとgame-over状態を得るScreen Adapterを追加する。状態にはboard矩形・cell/board confidence・再現可能なstate signatureを記録し、board confidenceが0.65以上かつgame-overでない場合だけUP/DOWN/LEFT/RIGHTのkeyboard候補を返す。CSS・grid・OCRデータから作る合成pixel fixtureでboard/score/terminal/action経路を検証し、Windows launcher integration testでは起動windowのcaptureからboardと4候補を検査する。launcherは小さいWindows desktopで盤面全体をcaptureするためChromium device scale factorを0.75にする。
- Issue #138: Play Workspaceの先頭へ「ゲーム / ウィンドウ」選択欄を配置する。ウィンドウ一覧の更新後も存在する選択対象は維持し、消えた選択対象は自動で別ウィンドウへ切り替えず再選択を求める。候補がない場合と一覧取得が失敗した場合は、利用者へ次の操作を表示する。
- Issue #337: GUIの画面取得後、Observation内のOCR・画像候補と任意のAutomation候補をCandidateMergerで統合し、安全評価欄へ入力元と評価結果を表示する。Automation候補JSONは空配列のまま使える。画面previewは実行中Sessionの候補sourceを書き換えず、step開始時に評価した同じObservationと候補だけをSession sourceへ登録する。confidence `0.0` の画像候補は拒否する。
- Issue #333: PythonとNative Runtimeのbright-region検出が明るさ・画素密度から実行信頼度を作らないようにする。両経路の明領域をUI要素として未確認のconfidence `0.0` で返し、画像候補だけの判断を候補評価器が拒否する。閉ループE2Eはsample gameのactive control情報と画面検出矩形を照合して実入力を検証する。
- Issue #370: 同じタイトルのウィンドウをタイトルとHWNDで個別表示する処理に回帰テストを追加する。選んだウィンドウのHWNDが画面取得と実入力用Session runtimeの両方へ渡ることを確認する。
- Issue #371: 連続実行が開始時に固定したdry-run／実入力modeを画面へ明示し、stepまたはloopの実行中は実入力許可checkboxを無効にする。Session modeとcheckbox表示が待機区間に食い違う状態を防ぐ。
- Issue #369: window選択・入力・captureが使うuser32/gdi32関数にWindows ABIの引数・戻り値型を宣言し、HWND・HDC・HBITMAP・pointer値をx64で切り詰めず受け渡す。EnumWindows callback、CreateDIBSectionの構造体/pixel pointer、GDI handle戻り値、SendMessageTimeoutWの戻り値をABIに合わせ、64-bit相当HWNDを通すfake関数とWindows native DLLのprototype設定を検証する。
- Issue #367: Windows入力APIが失敗した場合のエラーを具体化。SetCursorPosが失敗したクリックは押下を送らず、PostMessageWのキー・マウス送信失敗は対象操作と次の確認手順を日本語で表示する。API失敗時に成功結果を返さないことを実行境界のテストで確認し、偽Win32 APIの回帰テストでクリック抑止、Session停止の案内、押下状態の保持と解放再試行を検証する。
- Issue #60: capture modeをdesktop全体・選択window全体・主画面上の固定pixel ROI・window内の正規化ROIから選択できるようにする。captureごとにwindow-relative領域を解決し、ROI外のpixelを画像/OCR解析へ渡さない。ROI内候補を選択windowまたはscreen座標へ変換し、逆変換とnormalized coordinates、mode・source・confidenceをObservation metadataへ保持する。capture regionを`data/config.json`へ保存し、旧configにはwindow全体を適用する。
- Issue #39: `UnknownnessMonitor`へ認識・prototype距離・知識検索miss・評価器不一致・遷移曖昧さの正規化済み根拠を入力し、perceptual/semantic/action/transition/knowledgeの5scopeを別々に重み付き融合する。観測ごとの`instant_unknown`と、連続2回の閾値超過で開始し連続2回の回復で解除する`persistent_unknown`を分け、現在および保持中の根拠・確信度・出所を`unknownness/v1`で出力する。検出器入力を実行pipelineへ接続する処理とunknown時の行動選択は含まない。
- Issue #43: Decision Provider出力を形式・型・許可候補・判断時の画面状態・理由と候補の矛盾で決定論的に検証し、4段階の状態と根拠・出所をDecision traceおよびAction Safety監査へ記録する。TRUSTは実施検査に通過した状態であり、正しさの保証ではない。REJECTはfail closed、非TRUSTはlive input前にverificationを要求する。レビュー対応で候補語境界・任意の後続修飾語・一文字候補・二重否定・クリック／押下の能動形と受動形、前置詞・冠詞・UI部品名を含む明示的な否定を検証し、英語・日本語とも候補一覧にある複合名の別候補を区別する。未実行判断だけを行動履歴から除外し、直前に実行した操作のOutcome結果はその操作のtraceと後続Decision Contextに保持する。終端結果でも変化・安定Evidenceを重み付けして `state_changed` を独立判定し、競合が強い場合は状態変化として扱わない。旧Ollama APIは省略された理由を空文字列へ補い、文字列以外の理由を拒否する。空候補の通信前拒否、schema失敗時のprovider provenance、Evaluator許可候補による独立groundingも追加検証し、Action SafetyとDecision Reliabilityの宣言コメントを責務・入力の意味・判断条件・結果まで説明する形へ改める。
- Issue #259: 選択HWND専用のWindows Graphics Capture sessionをNative Runtime C ABI v1へ追加。寸法・packed BGRA・session内frame ID・monotonic時刻・lease中だけ有効なDirect3D texture handleを返し、caller-owned bufferへのcopyとtimeout付きframe取得を提供する。Pythonの`WindowsScreenCapture`は選択windowでsessionを再利用し、WGC非対応・取得失敗時は理由を保持してGDI fallbackを試す。Windows resize integration testとPython fake ABI・fallback・ObservationService lifecycle testを追加。

- Issue #38: ゲーム固有presetに依存しないrolling SelfCheckMonitorを追加。scene/action/state cycle、連続no-progress、confidenceを考慮したexpected/actual outcomeの不一致、汎用Scene時間超過、confidenceとreliabilityに基づくevaluator disagreementをseverity・evidence付きeventとして報告する。判定保留Outcomeは停滞・不一致判定から除外し、継続中の不一致がWarningからCriticalへ上がった場合は再通知する。

- Issue #32: `Application`が保持していた互換用Tkinter loop callbackと重複loop状態を削除し、開始・停止・step・連続実行を`GameSessionController`へ一本化。実行時責務境界の説明と、Application commandの委譲を検証する回帰テストを追加。

- Issue #146: `LiveObservationService`がcaptureを直接実行する構造を、bounded latest queueを持つ`SharedFrameProducer`からObservation cadenceでsampleする構造へ変更。window変更時に未読の旧source frameを破棄し、freshness上限を超えたframeを拒否する。出力する`features.capture_metadata`にproducer-scoped frame ID、source世代、Event Journalと同じprocess-monotonic時計で表すcapture nanosecondsを含める。Application終了時にcapture producerを停止し、fake capture/analyzerによるstale・window変更・error・lifecycleのテストを追加。

- Issue #142: Pythonのmodule/import/dependency/class/function/method/signature/data-model/source-locationを標準ASTから抽出するRepository Map Generatorとversion付きJSON Schemaを追加。include/exclude設定、出力先の上書き防止、platform-independent順序、PEP 263 encoding検出、decorator literal秘匿、atomic output、stale checkを提供し、本文・実行時挙動を推測せず`generated/repo_map.json`を生成する。

- Issue #143: start_task.batが取得したIssue本文、Git statusの変更パス、Repository Mapを使い、関連module/symbolとtest候補を理由・ヒューリスティックconfidence付きで`.codex/next_issue.md`へ追加する。ファイル本文やdiff hunksは読まず、既存の文字数上限とIssue本文優先の切り詰め、必須`finish_task.bat`確認を維持する。

- Issue #258: 1つのcapture backendから得た`ScreenFrame`を複数のconsumerへ配るShared Frame Producerを追加。`frame_id`とsource世代を付け、BGRA寸法・monotonic timestampを検証する。最新frame置換と順序維持のbounded queue、drop metrics、source切替、停止とcapture失敗通知をfake backendで検証する。

- Issue #262: legacy History・DecisionTrace・Execution・Safety・RuntimeLogをSession Event Journalへ接続。旧JSON/JSONLを削除せず、recordをstable event ID付きで移行し、decision/execution/safetyの配列JSON全体を書き直さずにSession内Journalへ追記する。

- Issue #261: versioned Session Event EnvelopeとSQLite WAL Journalを追加。UTC/monotonic timestamp、session sequence、correlation IDs、compact payload/artifact referencesを定義し、complete record検証・未コミットtransactionの復旧・flush/checkpointを実装。SQLite本体とfsync付きJSONL参照の30分相当append benchmarkを追加。
- Issue #204: Native Runtimeへ粗粒度FAST_CV frame-preprocess C ABIを追加。BGRAからRGB/brightness統計、dHash、4近傍bright-regionをNativeでまとめて計算し、caller-owned region buffer・buffer-too-small retry・timing/frame-payload/output/copy metricsを定義。NativeRuntime bindingはproviderが報告したinput-copy metricsを受け入れ、capacity retryを含む各ABI呼び出しの値を合算する。FrameAnalyzer注入点と同じPython参照実装へのfallbackを追加。C/C++ ABI testとWindows DLLを使ったPython/native parity testで検証。
- Issue #147: GUIからProvider・Pipeline・候補評価器の生成をRuntimeCompositionへ分離。GameSessionControllerが保持するPipelineとProviderをSession中に共有し、終了時はPipelineを先に閉じてProviderを解放する。部分初期化・終了失敗をFakeで検証し、失敗したresourceだけを再試行できるようにする。

- Issue #145: start/stop/one-step/continuous-loopをTkinterからGUI-freeのGameSessionControllerへ移動。repeat/outcome stop policy、generation-guarded timer cancellation、error/terminal lifecycleとSession-scoped DecisionPipeline/Engineの再利用・一括shutdownを追加し、fake runtime/schedulerで検証。

- Issue #145 review follow-up: 実入力許可の解除で即時停止し、各実行stepでも再検証。loop observer例外をFAILEDへ遷移させ、終了に失敗したruntimeを保持して再開を拒否。状態通知失敗を隔離し、各経路を回帰テスト。
- Issue #331: 判断時に評価・許可された候補オブジェクトを実行まで引き継ぎ、操作IDから元候補を再検索しないようにする。操作IDが重複する候補はすべて拒否し、Providerが候補リストを同じIDの別候補へ置き換えても元の評価済み候補だけを実行する。Decision Contextには一括評価した重複拒否理由を記録し、既存の`GamePlayerEngine.step()`拡張も実行経路で呼び出す。

- Issue #332: Ollamaの画面評価・候補判断をUI thread外で実行し、推論中の停止を受理する。実行中のキー保持・待機もRunControllerの停止状態とstep開始世代を監視して中断し、保持キーを解除する。停止後・再開後に返った旧推論結果は判断履歴と実入力へ進めず、停止理由をログへ記録する。mouse方式の連続実行では、入力workerがSetCursorPosの開始・完了とクリック先をUIへ通知し、自動カーソル移動を手動移動による停止と誤認しない。未通知の手動カーソル移動による停止は維持する。

- PR #345: 重複した画面評価workerは起動順IDで新旧を判定し、最新より古い結果を観測履歴・評価表示へ反映しない。停止中に完了した手動画面評価は実行世代に関係なく評価表示へ反映する。古い評価結果が現在有効な連続実行stepに属する場合は連続実行を停止する。「再開」操作で実行世代が更新された場合も旧連続実行状態と予約callbackを解除し、旧workerの結果を破棄した後に連続実行を再開できる状態に戻す。

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
