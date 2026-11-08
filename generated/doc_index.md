<!-- 自動生成: tools/generate_doc_index.py。手で編集しない。 -->
# 文書目次

文書を探す前にここを読みます。題名と先頭の1文だけを載せます。内容の正本は各文書です。

| 文書 | 題名 | 先頭の要約 | 親Issue | 確認日 |
|---|---|---|---|---|
| [`README.md`](../README.md) | AI Game Player | 未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。ローカル規則またはOllamaを使い、実入力は明示許可までdry-runで扱います。 | - | - |
| [`AGENTS.md`](../AGENTS.md) | AI Game Player: Agent Guide | 0. Read AICONTEXT.md first. It is the compact ai-context-reducer entrypoint and defines exploration  | - | - |
| [`AI_CONTEXT.md`](../AI_CONTEXT.md) | AI Context | Codex / AI が最初に読む小さい索引。詳細仕様はここへ複製しない。 | - | - |
| [`key_info.md`](../key_info.md) | AI Game Player 現在の機能 | 確認基準：2026-11-01、統合feature 77a8995e。部品・本番接続・実ゲーム検証を区別します。場所別の状態と根拠も参照してください。 | - | - |
| [`doc/ActionSafetyEvaluator機能説明書.md`](../doc/ActionSafetyEvaluator機能説明書.md) | Action Safety Evaluator 機能説明書 | ActionSafetyEvaluator は、Decisionが選んだ ActionCandidate を実行前に独立評価し、その候補を実行候補として信用してよいかを SAFE / SUSPICIO | - | - |
| [`doc/Automation機能説明書.md`](../doc/Automation機能説明書.md) | Automation候補検出 | ConfiguredRegionDetector はゲーム固有の automation.json に定義された名前付き矩形を ActionCandidate へ変換する。矩形の中心をクリック候補とし、 | - | - |
| [`doc/Challenger更新機能説明書.md`](../doc/Challenger更新機能説明書.md) | Challenger更新（Challenger Update Pipeline）機能説明書 | リポジトリに戻る ｜ 学習診断 ｜ 記録と学習の案内 ｜ 設計 | - | - |
| [`doc/DecisionContext機能説明書.md`](../doc/DecisionContext機能説明書.md) | Decision Context機能説明書 | Recognition / Evaluation / Decision を分離し、小型Decision LLMへraw認識結果ではなく、候補単位に整理した DecisionContext を渡します。 | - | - |
| [`doc/E2E受入試験手順.md`](../doc/E2E受入試験手順.md) | E2E受入試験手順 | Issue 30 の第一段階受入として、人間が候補JSONを編集せずに Screen - State - Decide - Act - Observe - Evaluate - next Decisi | - | - |
| [`doc/Experience Reader機能説明書.md`](../doc/Experience Reader機能説明書.md) | Experience Reader | aigameplayer.experiencereader.JsonlExperienceReader は、Canonical Episode を1行1件のJSON Lines形式で読み込みます。Ex | - | - |
| [`doc/FailSafeRuntime説明書.md`](../doc/FailSafeRuntime説明書.md) | Fail-safe Runtime / OS Emergency Stop | Issue 40 のFail-safe Runtimeは、Actionの意味や通常の入力妥当性ではなく、上位AIや制御processそのものが壊れた場合にも入力権限を失効させるための実行基盤である。 | - | - |
| [`doc/NativeRuntime接続機能説明書.md`](../doc/NativeRuntime接続機能説明書.md) | Native Runtime Python接続 | aigameplayer.runtime.nativeはC ABI v1に対する薄いctypes接続層です。importだけでは共有ライブラリをロードせず、GUIや既存Python実行経路を自動的に切 | - | - |
| [`doc/OCR候補検出機能説明書.md`](../doc/OCR候補検出機能説明書.md) | OCR候補検出 | OcrTextCandidateDetector はOCRエンジンの出力（文字列と矩形）を、認識共通中間表現 DetectedElement へ変換する。text、bbox、source、confid | - | - |
| [`doc/OutcomeDetectionFusion機能説明書.md`](../doc/OutcomeDetectionFusion機能説明書.md) | Outcome Detection / Fusion 機能説明書 | Issue 34 のOutcome Detectionは、Before Observation + Action + After Observation から「何が起きたか」を構造化する責務を持つ。成 | - | - |
| [`doc/Pythonコードの整形.md`](../doc/Pythonコードの整形.md) | 変更したPythonファイルを整形する | この手順はPythonコードを変更する開発者向けです。整形漏れを検出しつつ、別の作業者の変更や既存ファイル全体の一括書き換えを避けます。 | - | - |
| [`doc/Python証明書信頼棚卸し.md`](../doc/Python証明書信頼棚卸し.md) | Python証明書信頼の棚卸し（Issue 513 / 親Issue 482） | この文書は、AI Game Playerの開発・CI・配布で使うPython環境が、実際にどのpip・certifi・uv・sigstoreと、どの証明書信頼源（CA bundleまたはOSの信頼スト | - | - |
| [`doc/RecoveryController機能説明書.md`](../doc/RecoveryController機能説明書.md) | Recovery Controllerの機能説明書 | この説明書は、Issue 38のSelfCheckMonitorが返すSelfCheckEventを受けて、再観測・再判断・停止要求を有限回で段階的に進めるRecoveryController（Iss | - | - |
| [`doc/SafetyGuard説明書.md`](../doc/SafetyGuard説明書.md) | SafetyGuard / Emergency Stop 説明書 | Issue 33 の最終防衛線として、LLM・Recognition・Action Safety Evaluatorの判断に依存しない決定論的な入力Guardを提供する。 | - | - |
| [`doc/UI埋め込み機能説明書.md`](../doc/UI埋め込み機能説明書.md) | UI埋め込み機能 | uiembedding.py はUI crop/regionを検索用特徴へ変換し、同一ゲーム再認識とゲーム横断prototype検索へ利用する基盤を提供する。 | - | - |
| [`doc/UI認識メモリ機能説明書.md`](../doc/UI認識メモリ機能説明書.md) | UI Detection / Recognition Memory | Issue 10 の初期実装は、未知ゲーム向けの汎用detectorと、プレイ済みゲーム向けの軽量Recognition Memoryを分離する。 | - | - |
| [`doc/UPDCommander導入方針.md`](../doc/UPDCommander導入方針.md) | UPD Commander 導入方針と品質Checker | upd-commander-base-design の UI / Process / Data、Commander / Messenger / Processing の責務分離を AI Game Pl | - | - |
| [`doc/UnknownnessMonitor機能説明書.md`](../doc/UnknownnessMonitor機能説明書.md) | Unknownness Monitorの機能説明書 | この説明書は、画面認識・知識検索・候補評価などの検出器を追加する実装者が、検出器の不確かさをUnknownnessMonitorへ渡し、scope別の瞬間判定と持続判定を確認するための説明です。Unk | - | - |
| [`doc/Windows安全統合ゲート.md`](../doc/Windows安全統合ゲート.md) | Windows安全統合ゲート（Issue 94） | GitHub ActionsのWindows runnerで、Windows固有のCapture・Window targeting・Input・Safety・bundleの回帰を検出する検査の一覧です | - | - |
| [`doc/Windows配布bundle説明書.md`](../doc/Windows配布bundle説明書.md) | Windows配布bundle説明書（Issue 149、515） | この文書は、tools/buildwindowsbundle.ps1が作るWindows用配布物（以下、bundle）の配置と、bundleの起動・同梱CAを確かめる検査を説明します。検査で確認できる | - | - |
| [`doc/architecture/README.md`](../doc/architecture/README.md) | 動作と配置の見取り図：詳しく読む入口 | リポジトリのREADMEへ戻る | - | - |
| [`doc/architecture/artifact_store.md`](../doc/architecture/artifact_store.md) | Content-addressed Artifact Store | ArtifactStore defines the backend-neutral API; FileSystemArtifactStore is the local immutable implem | - | - |
| [`doc/architecture/autonomous_player_goal_authority.md`](../doc/architecture/autonomous_player_goal_authority.md) | 自律Playerと権限分離：Goalは賢者ミスク本人が決める（845） | 過去の設計は「利用者がGUIで目標を与え、システムがそれを最優先で実行する」前提を含んでいました。これは誤りです。本プロジェクトの目的は、利用者の作業を肩代わりするマクロではなく、賢者ミスク自身がゲー | - | - |
| [`doc/architecture/belief_snapshot.md`](../doc/architecture/belief_snapshot.md) | 型付き BeliefSnapshot と有界候補 Store | Issue 478 の子 563 が実装する Versioned Bounded BeliefSnapshot Contract + Candidate Dedup/Residual Mass の契約 | - | - |
| [`doc/architecture/belief_update.md`](../doc/architecture/belief_update.md) | 状態仮説を予測し、実観測の尤度で更新する | この資料は汎用ゲームプレイヤーの認識・計画を接続する開発者向けです。 | - | - |
| [`doc/architecture/campaign_runner.md`](../doc/architecture/campaign_runner.md) | Campaign Runnerの実行契約 | 2048 campaignを調査する開発者は、report.jsonのfailuredetailに付くobservationtimingを確認してください。ReferenceGame2048Episo | - | - |
| [`doc/architecture/canonical_experience_schema.md`](../doc/architecture/canonical_experience_schema.md) | Canonical Experience Schema | ExperienceEpisode, ExperienceStep, and ExperienceEvent in src/aigameplayer/experience.py define the  | - | - |
| [`doc/architecture/capability_manifest.md`](../doc/architecture/capability_manifest.md) | Optional Capability Manifest | Optional features declare their capability IDs, required and optional dependency IDs, lifecycle, con | - | - |
| [`doc/architecture/execution_receipts.md`](../doc/architecture/execution_receipts.md) | 実行受領記録と操作後観測 | Issue 628は、保存した判断だけを直前の実操作として扱っていた帰属の誤りを修正する。操作結果は、実行境界の受領記録と、明示的な操作後観測の組で保存する。ゲーム固有の候補生成や探索戦略をこの境界に | - | - |
| [`doc/architecture/expectimax_planning.md`](../doc/architecture/expectimax_planning.md) | Belief-aware bounded Expectimax | 移行区分： このページのCoreはUI非依存のPython数値参照実装です。661の恒久GUIはQt6 Widgets/C++、常駐CoreはRustの予定で、未実装です。数学・品質・予算・版付きJS | - | - |
| [`doc/architecture/flow_for_readers.md`](../doc/architecture/flow_for_readers.md) | 一回の判断と実行は、何をどの順番で行う？ | 見取り図の索引へ戻る ｜ 部品と実行場所を見る | - | - |
| [`doc/architecture/hook_runtime.md`](../doc/architecture/hook_runtime.md) | Processing Hook Runtime：stage境界のread/modify/inject | 動作と配置の見取り図へ戻る | - | - |
| [`doc/architecture/inference_provider_api.md`](../doc/architecture/inference_provider_api.md) | Inference Provider API v1 | Kadoka Core must not depend directly on one model server or vendor API. | - | - |
| [`doc/architecture/knowledge_builder.md`](../doc/architecture/knowledge_builder.md) | Experienceから操作効果を数値Knowledgeへ増分集計する | この文書は、モデルの再学習を行わない環境でもプレイ経験を次の判断へ使うための | - | - |
| [`doc/architecture/knowledge_feature_adapter.md`](../doc/architecture/knowledge_feature_adapter.md) | Knowledgeを一回の判断へ固定してsoft evidenceとして使う | この文書は、ゲーム経験から得たKnowledgeを判断へ接続する開発者向けです。 | - | - |
| [`doc/architecture/knowledge_promotion.md`](../doc/architecture/knowledge_promotion.md) | 知識の更新候補を評価し、本番版の参照先を切り替える | この文書はKnowledgeを学習・公開する処理を実装する開発者向けです。 | - | - |
| [`doc/architecture/knowledge_snapshot.md`](../doc/architecture/knowledge_snapshot.md) | version付きKnowledge Snapshotと安全な読取 | この文書はKnowledge Builder・Decision Adapter・Promotion Gateの実装者向けです。 | - | - |
| [`doc/architecture/learning_capability_contract.md`](../doc/architecture/learning_capability_contract.md) | Learning Capability Contract | LearningCapabilityManifest in src/aigameplayer/learning.py is the versioned declaration for a model  | - | - |
| [`doc/architecture/light_adaptation.md`](../doc/architecture/light_adaptation.md) | Inference + Light Adaptation runner (422) | RuntimeComposition.createsessionruntime は実 DecisionPipeline に | - | - |
| [`doc/architecture/native_abi_v1.md`](../doc/architecture/native_abi_v1.md) | Native Runtime C ABI v1 | native/include/kadoka/runtimeapi.h is the public, C-compatible boundary between the high-level appli | - | - |
| [`doc/architecture/native_runtime_boundary.md`](../doc/architecture/native_runtime_boundary.md) | Native Runtimeの配置宣言と境界コストを検査する | この文書は、AI Game PlayerのPythonとC++の責務を変更する開発者向けです。配置の理由をmanifestへ記録し、契約宣言の不一致を検出し、言語境界のコストを測る手順を説明します。P | - | - |
| [`doc/architecture/native_security_profile.md`](../doc/architecture/native_security_profile.md) | Native C++ Security Profile（Issue 115 第1段） | 動作と配置の見取り図へ戻る | - | - |
| [`doc/architecture/planner_runtime.md`](../doc/architecture/planner_runtime.md) | Planner → Decision と receding-horizon 実行 | Issue 567（親 480）。本実装・比較は旧Python互換Runtimeの継続検証です。P0 661の製品方針はQt6 Widgets native C++ GUI / Rust reside | - | - |
| [`doc/architecture/planning_risk_information.md`](../doc/architecture/planning_risk_information.md) | 観測価値と下振れリスクを使うPlanning Policy | 移行区分： このページのCoreはUI非依存のPython数値参照実装です。661のQt6 Widgets/C++ GUI・Rust常駐Coreは未実装で、現在は移行互換側の数学APIです。risk/ | - | - |
| [`doc/architecture/plugin_capability_manifest.md`](../doc/architecture/plugin_capability_manifest.md) | Plugin Capability Manifest v1：Plugin権限の宣言と判定 | 動作と配置の見取り図へ戻る | - | - |
| [`doc/architecture/privacy_data_inventory.md`](../doc/architecture/privacy_data_inventory.md) | Privacy Data Inventory（data class・1.0既定policy） | Privacy条件が文章だけだと、Provider・Recorder・Learningで保存や送信の判断がずれます。Issue 224（親 123）は、Applicationが扱うdata class | - | - |
| [`doc/architecture/qt_rust_migration.md`](../doc/architecture/qt_rust_migration.md) | Qtの画面とRustの常駐Coreへ移る設計 | 全体案内 ｜ 完成形と現在 ｜ 配置の入口 | - | - |
| [`doc/architecture/reference_2048_recognition.md`](../doc/architecture/reference_2048_recognition.md) | 固定版2048の発光・得点認識回帰（Issue 659） | この修正は任意の参照2048 Adapterの画像認識に限定する。Minecraft・カードゲーム・FPS等の汎用Planner、Belief、Verifier、Safetyに盤面固有の規則を加えない | - | - |
| [`doc/architecture/reference_browser_post_failure_diagnostics.md`](../doc/architecture/reference_browser_post_failure_diagnostics.md) | 参照ブラウザーの失敗後だけに行う描画診断（明示検証のみ） | Windows の initial observation が全空盤面のまま期限切れになる場合、 | - | - |
| [`doc/architecture/reference_http_accept_queue.md`](../doc/architecture/reference_http_accept_queue.md) | 参照ゲームの私有HTTPサーバーと起動時burst | LoopbackStaticServer は所有する固定版2048だけを、episodeごとの | - | - |
| [`doc/architecture/repository_state.md`](../doc/architecture/repository_state.md) | Repository Map: Pythonソース構造の機械可読インデックス | この文書は、Codex支援ツールの利用者と実装者に向けて、Pythonソースの宣言構造を検索する generated/repomap.json の生成方法と収録範囲を説明します。Repository  | - | - |
| [`doc/architecture/resource_stability_gate.md`](../doc/architecture/resource_stability_gate.md) | Long-session Resource Stability Gate | 最終確認日: 2026-11-07 / 状態: 実装済み（今回のスライス） / 関連: 設計書 787・Runtime Resource Lifecycle (786)・Issue 250（実機30分 | - | - |
| [`doc/architecture/retention_core.md`](../doc/architecture/retention_core.md) | Retention Core（budget / Protected / TTL / exact dedup） | Experienceの重いpayload（frame・video segment・model output等）はContent-addressed Artifact StoreへSHA-256参照で保 | - | - |
| [`doc/architecture/runtime_evaluation.md`](../doc/architecture/runtime_evaluation.md) | Runtime Evaluation (491 / 元8) | 既存 PrimitiveEvaluator の6軸を、既定の実Session / GamePlayerEngine / Decision Context / | - | - |
| [`doc/architecture/runtime_layers.md`](../doc/architecture/runtime_layers.md) | Runtime Architecture | Kadoka is intentionally modular internally while remaining minimal for ordinary users. | - | - |
| [`doc/architecture/runtime_map_for_readers.md`](../doc/architecture/runtime_map_for_readers.md) | どの部品がどこで動く？ | 見取り図の索引へ戻る ｜ 一回の流れを見る | - | - |
| [`doc/architecture/runtime_modes.md`](../doc/architecture/runtime_modes.md) | Runtime Mode / Phase 契約 | Issue 420 の基盤である 421 の実装契約です。Mode は利用者が選ぶ実行方針、Phase は現在の処理段階です。Mode の代わりに個別の学習 bool を持たず、宣言から Subsys | - | - |
| [`doc/architecture/runtime_resource_lifecycle.md`](../doc/architecture/runtime_resource_lifecycle.md) | Runtime Resource Lifecycle（Session Resource Registry / Cleanup Coordinator） | Session内で確保・借用される資源（共有Frame、GPU cache、capture session等）の所有者と解放経路を1か所に集め、release・task終了・世代切替・Stop・err | - | - |
| [`doc/architecture/session_event_journal.md`](../doc/architecture/session_event_journal.md) | Session Event Journal | EventEnvelope in src/aigameplayer/eventjournal.py is the versioned session-event contract. Each reco | - | - |
| [`doc/architecture/stage_envelope.md`](../doc/architecture/stage_envelope.md) | StageEnvelope v1：stage間の中間データ契約 | 動作と配置の見取り図へ戻る | - | - |
| [`doc/architecture/startup_failure_evidence.md`](../doc/architecture/startup_failure_evidence.md) | 2048起動失敗の診断保存 | Issue 637は、自然なbrowser起動失敗の原因を調べるための証拠保存を追加する。親 459の原因解消とは別の成果物である。既存 tools/run2048campaign.py --save | - | - |
| [`doc/architecture/transition_simulation.md`](../doc/architecture/transition_simulation.md) | 仮想操作と確率遷移を実入力から分離する | この文書は汎用ゲームプレイヤーの探索機能を実装する開発者向けです。 | - | - |
| [`doc/architecture/windows_long_comparison.md`](../doc/architecture/windows_long_comparison.md) | Windows長枠3 Provider比較(Issue 795) | リポジトリ直下から実行し、先に PYTHONPATH=src;.(PowerShellでは $env:PYTHONPATH = "src;.")を設定します。 | - | - |
| [`doc/contributor/documentation_update_guide.md`](../doc/contributor/documentation_update_guide.md) | 日本語の設計図と説明書を更新するとき | リポジトリのREADMEへ戻る ｜ 既存のRuntime境界の説明 | - | - |
| [`doc/contributor/subsystem_ownership_map.md`](../doc/contributor/subsystem_ownership_map.md) | Contributor向けサブシステム責務とレビュー先 | このページは、Kadoka AI Game Playerへコード・テスト・文書を提供するContributor（貢献者）が、変更する場所、守る境界、相談するレビュー先を選ぶための地図です。対象は202 | - | - |
| [`doc/design/issue-152-capability-manifest.md`](../doc/design/issue-152-capability-manifest.md) | Issue 152 設計：Plugin Capability Manifest / Enforcement Contract | 今回のスライスは「宣言の契約（schema・参照validator・適合ケース）」と「in-process Hook runtimeでのdeny-by-default判定」まで。隔離・配布・data  | #58（関連: #42 Optional Parent、#235 Isolation、#224 Data Inventory） | - |
| [`doc/design/issue-170-challenger-update.md`](../doc/design/issue-170-challenger-update.md) | Issue 170 設計：Challenger Update Pipeline（今回のスライス） | 今回のスライス: proposal schema、strategy選択（calibration / threshold / prototype の3 baseline）、Champion/Challe | #36（兄弟: #171 Promotion / Rollback Gate、#173 Candidate Evaluation Runner。関連: #647、#712、#426、#427） | - |
| [`doc/design/issue-224-data-inventory.md`](../doc/design/issue-224-data-inventory.md) | Issue 224 設計: Privacy Data Inventory / Defaults | - DG-1（owner確認、非ブロッキング）: class別の既定TTL値（rawframe/croppedimage/unclassified 1日、ocrtext/decisioncontext | - | - |
| [`doc/design/issue-495-candidate-identity.md`](../doc/design/issue-495-candidate-identity.md) | 候補の同一性ガード（CandidateIdentityGuard）仕様（495） | - 座標系・Frame・世代が両方既知で食い違う場合はAMBIGUOUS。入力元の世代が違う候補は統合せず拒否する。 | - | - |
| [`doc/design/issue-729-planner-test-load.md`](../doc/design/issue-729-planner-test-load.md) | Issue 729 高負荷時に結果を期待するテストを決定的にする | Spec-Status: READY | - | - |
| [`doc/design/issue-786-session-resource-registry.md`](../doc/design/issue-786-session-resource-registry.md) | Issue 786 Session Resource Lease Registry / Cleanup Coordinator | Session内で確保・借用されるruntime resourceの所有者・lease・解放経路を1か所に列挙し、release / task終了 / generation切替 / Stop / er | #785 | - |
| [`doc/design/issue-787-long-session-resource-stability-gate.md`](../doc/design/issue-787-long-session-resource-stability-gate.md) | Issue 787 Long-session Resource Stability Gate | 長時間のSessionで資源使用量が単調増加し続けないことを、peakだけでなくwarmup後の傾き・Session終了後の回収・複数Session反復のbaselineで判定するHarness /  | #785 | - |
| [`doc/design/issue-788-runtime-retention-adapter.md`](../doc/design/issue-788-runtime-retention-adapter.md) | Issue 788 Runtime Retention Adapter：Session/Journal参照SnapshotとGC Triggerの本番接続 | 1. live reference snapshot: Session が参照中の Artifact（Event Journal・Experience・実行中の保存/Replay）を毎回集め、coll | #18 | - |
| [`doc/design/issue-789-artifact-admission.md`](../doc/design/issue-789-artifact-admission.md) | Issue 789 Artifact Admission Transaction | 1. canadmit()→put()→register()が別々の操作で、2 writerが同時に判定を通るとhard limitを超える。 | #18 | - |
| [`doc/design/issue-790-orphan-reconciliation.md`](../doc/design/issue-790-orphan-reconciliation.md) | Issue 790 Orphan Reconciliation（crash由来の未索引Artifactの安全な回収） | 利用者の操作はありません。本Issueはライブラリ API（OrphanReconciler・reclaimbudget）だけを追加し、アプリの起動・終了・定期処理からの呼出し（runtime 配線） | #18 | - |
| [`doc/design/issue-791-2048-empty-board-diagnosis.md`](../doc/design/issue-791-2048-empty-board-diagnosis.md) | Issue 791 2048の空盤面・HTMLActuator欠落の原因カテゴリ診断 | 署名: Tomiya | #459 | - |
| [`doc/design/issue-794-observation-lease.md`](../doc/design/issue-794-observation-lease.md) | Issue 794 Observation Lease失効の時系列因果と回帰防止 | 証拠: run 41938316919 attempt 1（PR 730）と run 41941522328 attempt 1（PR 732）の--save-heartbeat-diagnostic | #459 | - |
| [`doc/design/issue-795-windows-long-comparison.md`](../doc/design/issue-795-windows-long-comparison.md) | Issue 795 Windows長枠でCycling・Baseline・Belief Plannerを同条件比較する | 署名: Tomiya | #567（長枠比較の残件）。安定性の前提は #459/#794（merge済み）、Campaign基盤は #247 | - |
| [`doc/design/issue-806-provider-wire-data-classes.md`](../doc/design/issue-806-provider-wire-data-classes.md) | Issue 806 Provider wire schemaのdata class列挙へuimetadata・modeloutputを追加し、送信payloadを全classへ対応付ける | 1. 版の判断: v1の互換拡張とする。互換性は2つに分けて扱う。(i) 既存のmanifest・requestは新schemaでも妥当（後方互換）。(ii) 新classを含むrequest/man | #123（兄弟: #226 Remote Consent Enforcement、後続） | - |
| [`doc/design/issue-807-merge-gate-draft-retry.md`](../doc/design/issue-807-merge-gate-draft-retry.md) | Issue 807 merge gateのDraft事前検出・merge失敗本文・読取専用APIの有界再試行 | - PR状態取得（gh pr view）でisDraftも取得し、DraftならCI待機前に「PR is a draft」と明示して終了値3で停止する。自動でreadyにはしない。 | none | - |
| [`doc/design/issue-808-preflight-deadline-margin.md`](../doc/design/issue-808-preflight-deadline-margin.md) | Issue 808 comparison preflightテストの実時計期限 | tests/testcomparisonpreflight.py のsetUpの期限を time.monotonic() + 5 から + 30 へ変更する（テストのみ）。 | #729 | - |
| [`doc/design/issue-811-perf-budget-robustness.md`](../doc/design/issue-811-perf-budget-robustness.md) | Issue 811 Noisy Provider性能予算の判定をrunnerの一時揺らぎに強くする | - rounds、totalmeasuredsteps、judgementmethod（"median of per-round nearest-rank p95 across independent | #178 | - |
| [`doc/design/issue-812-invalid-frame-planner-readiness.md`](../doc/design/issue-812-invalid-frame-planner-readiness.md) | Issue 812 Windows planning comparisonの初回入力0 — plannerのworker準備完了を待つ | - requiredconsecutive は1〜5、retryintervals は0.05〜2.0、deadlinemonotonic は有限float。範囲外は ValueError。 | #729 | - |
| [`doc/design/issue-817-http-test-race.md`](../doc/design/issue-817-http-test-race.md) | Issue 817 reference game HTTPサーバーの並行10 scriptテストの競合 | tests/testreferencegamehttpserver.py::testconcurrenttenscriptspreservebytescountersandportlifetime の | #729 | - |
| [`doc/design/issue-864-macro-trigger.md`](../doc/design/issue-864-macro-trigger.md) | Issue 864 重い判断機の非同期化と、判断・操作の層分離（MacroTrigger） | 設計資料のみ。コードは変更しない。実装は「実装Issueの分割案」の単位で別Issueにする。 | #19 | 2026-11-08 |
| [`doc/design/issue-866-decision-architecture.md`](../doc/design/issue-866-decision-architecture.md) | Issue 866 判断・操作の層構造と、モデル運用・学習・知識増築・データ圧縮の統合設計 | 設計資料のみ。コードは変更しない。既存の設計資料・Issueの責務境界と、空白の所有者を決める。実装は子Issueに分ける。 | #19 | 2026-11-08 |
| [`doc/design/issue-868-human-cognition-tasks.md`](../doc/design/issue-868-human-cognition-tasks.md) | Issue 868 人間のゲームプレイ中の認知をタスクとして洗い出し、タスク別のモデル・手法を割り当てる | 設計資料のみ。コードは変更しない。実装は「子Issue案」の単位で別Issueにする。 | #866 | 2026-11-08 |
| [`doc/guide/README.md`](../doc/guide/README.md) | AI Game Player：まず全体を知る | READMEへ戻る ｜ 目指す完成図と現在の実装図 ｜ 場所ごとの実装状況と根拠 ｜ 用語集 | - | - |
| [`doc/guide/implementation_status.md`](../doc/guide/implementation_status.md) | 場所ごとの実装状況と、完成までの残作業 | 完成図と現状図へ戻る ｜ 全体案内へ戻る | - | - |
| [`doc/guide/judgment.md`](../doc/guide/judgment.md) | 次の操作をどう決める？ 前回の結果をどう確かめる？ | リポジトリへ戻る ｜ 処理を選ぶ調整役と時間配分 ｜ DecisionContextの詳しい説明 | - | - |
| [`doc/guide/orchestration.md`](../doc/guide/orchestration.md) | 処理を選ぶ調整役と、精度・時間の配分 | 判断の流れへ戻る ｜ 親Issue 606 | - | - |
| [`doc/guide/recognition.md`](../doc/guide/recognition.md) | 画面をどう読み取り、操作候補に変えるの？ | リポジトリのREADMEへ戻る ｜ 画像解析の詳しい説明 ｜ 候補統合の詳しい説明 | - | - |
| [`doc/guide/records_learning.md`](../doc/guide/records_learning.md) | ゲームを動かした記録は、どう残し、何に使う？ | リポジトリに戻る ｜ Session記録の既存仕様 ｜ 学習用データの既存説明 | - | - |
| [`doc/guide/safety.md`](../doc/guide/safety.md) | モデルが「押す」と答えたら、すぐゲームに入力される？ | リポジトリに戻る ｜ Action Safetyの詳しい説明 ｜ FailSafe Runtimeの詳しい説明 | - | - |
| [`doc/guide/target_and_current.md`](../doc/guide/target_and_current.md) | 目指す完成形と、いま動く仕組み | 全体案内へ戻る ｜ 場所ごとの実装状況と根拠 | - | - |
| [`doc/guide/terms.md`](../doc/guide/terms.md) | 用語集：難しい言葉を日常の言葉に直す | 全体案内へ戻る ｜ 詳しい資料の書き方 | - | - |
| [`doc/guide/writing_rules.md`](../doc/guide/writing_rules.md) | 詳しい資料の書き方と確認事項 | 全体案内へ戻る ｜ 用語集 | - | - |
| [`doc/licensing_rationale.md`](../doc/licensing_rationale.md) | ライセンス方針と、その意図 | この文書は法的なライセンス本文ではなく、AI Game Player Projectがなぜ現在のライセンス構成を選んでいるかを説明するためのものです。 | - | - |
| [`doc/main保護方針.md`](../doc/main保護方針.md) | mainブランチ保護方針（Repository Ruleset） | この文書は、AI Game Playerのmainブランチへ、CIを通過していない変更が通常のマージ経路で入らないようにする設定と運用を説明します。読者は、PRをマージする作業者（人、Claude C | - | - |
| [`doc/models/README.md`](../doc/models/README.md) | どこで、どのモデルや手法を使う？ | リポジトリへ戻る ｜ 実際に動いたモデルを確認する方法 | - | - |
| [`doc/models/evidence.md`](../doc/models/evidence.md) | 設定にあるモデルと、実際に動いたモデルを分けて確認する | モデル配置の図と対応表へ戻る ｜ 親Issue 606 | - | - |
| [`doc/release_1_0.md`](../doc/release_1_0.md) | 1.0.0 Release Scope and Acceptance | Issue 19 is the priority/source-of-truth. This document records the repository | - | - |
| [`doc/security/python_security_profile.md`](../doc/security/python_security_profile.md) | Python Security Profile | この文書は、Python層のセキュリティ静的解析（Issue 114）の判定規則、抑制の管理方法、CIでの実行方法を説明します。一般的なlint・型検査（Issue 86のtests.ymlのRuff | - | - |
| [`doc/security/threat_model.md`](../doc/security/threat_model.md) | Kadoka AI Game Player 脅威モデル | この文書は、Kadoka AI Game Playerの保守者とContributorが、実装・テスト・CI（コード変更を自動build / testする継続的インテグレーション）で先に扱うセキュリテ | - | - |
| [`doc/versions.md`](../doc/versions.md) | Versions | Application versionとSchema/API/ABI versionは独立して管理する。 | - | - |
| [`doc/キャプチャ観測Source機能説明書.md`](../doc/キャプチャ観測Source機能説明書.md) | キャプチャ観測Source | CapturedObservationSource はFrameCaptureとFrameAnalyzerを接続し、実画面のBGRAフレームを ScreenObservation へ変換する。操作候補 | - | - |
| [`doc/コア判断ループ機能説明書.md`](../doc/コア判断ループ機能説明書.md) | (題名なし) | - | - | - |
| [`doc/コマンド指向.md`](../doc/コマンド指向.md) | (題名なし) | - | - | - |
| [`doc/コーディングルール.md`](../doc/コーディングルール.md) | 共通コーディングルール | この文書は、AI Game Playerのコード、テスト、文書を変更するすべての作業者（人、Claude Code、Codex、ChatGPT、GitHub Copilot）が守る規則です。変更前に読 | - | - |
| [`doc/コーディング規約.md`](../doc/コーディング規約.md) | コーディング規約 | AI Game Playerは複数の処理領域と言語を持ち、コードの生成・変更も継続します。名前、責務、コメント、エラー処理、説明資料の書き方が揃っていないと、変更の理解や安全性の確認に時間がかかります | - | - |
| [`doc/レビュー基準.md`](../doc/レビュー基準.md) | レビュー基準 | AI Game Playerは、画面認識・判断・入力・評価をつなぐアプリケーションです。コードや説明の小さな曖昧さが、誤操作、停止不能、評価誤り、保守負担につながることがあります。 | - | - |
| [`doc/ワークフロー/ワークフロー.md`](../doc/ワークフロー/ワークフロー.md) | 開発者と評価者が使うGitHub Issue・Pull Request手順 | この文書は、AI Game Playerの開発者がGitHub Issueから変更を作り、Pull Request（PR）で確認を受ける手順を説明します。GitHub Issueが要求・優先度・設計上 | - | - |
| [`doc/候補統合機能説明書.md`](../doc/候補統合機能説明書.md) | 操作候補統合 | この説明書は、画面取得後にGUIがAutomation・OCR・画像候補を統合し、候補の入力元と安全評価を表示する仕組みを実装者へ説明する。 | - | - |
| [`doc/判断パイプライン機能説明書.md`](../doc/判断パイプライン機能説明書.md) | 判断パイプライン | DecisionPipeline は観測Sourceから読み込んだ画面とAutomation候補へOCR候補を加え、CandidateMerger、GamePlayerEngine の順に接続する。r | - | - |
| [`doc/学習データセット生成機能説明書.md`](../doc/学習データセット生成機能説明書.md) | Experience Training Dataset Builder | ExperienceDatasetBuilder は、Learning Capabilityが宣言するDataset schema/versionに従い、保存済みExperienceのEpisode列 | - | - |
| [`doc/学習診断機能説明書.md`](../doc/学習診断機能説明書.md) | 学習診断（Training Value / Error Diagnosis）機能説明書 | リポジトリに戻る ｜ 学習用データの説明 ｜ 記録と学習の案内 | - | - |
| [`doc/提案メモ.md`](../doc/提案メモ.md) | (題名なし) | - | - | - |
| [`doc/操作実行機能説明書.md`](../doc/操作実行機能説明書.md) | 操作実行 | ActionExecutor は安全評価済み候補の実行境界を提供する。既定は dryrun=True で、選択結果だけを返してOS入力を行わない。実入力は別Executorとして明示実装し、誤って有効 | - | - |
| [`doc/根本目標.md`](../doc/根本目標.md) | 根本目標（最初に読む1ページ） | 賢者ミスクという、ゲームを自分で遊ぶ独立したPlayerです。 画面を観察し、何を目指すか（Goal）と何が良い結果かを自分で考え、評価と攻略計画を作り、安全に操作して、結果から学びます。 | - | - |
| [`doc/独立テストゲーム集.md`](../doc/独立テストゲーム集.md) | 独立テストゲーム集の境界と検証 | 汎用プレイヤーを多様な画面・入力方式で検証するため、634で既存ゲーム追加commit a1b7214ca97b4d188f389c0f0efbe876ff67b151の独立成果物を統合します。旧br | - | - |
| [`doc/画像解析機能説明書.md`](../doc/画像解析機能説明書.md) | 画像解析 | FrameAnalyzer はBGRAフレームを ScreenObservation へ変換する。平均RGB、平均輝度、完全一致用SHA-256シグネチャに加え、外部依存なしの64bit dHash（ | - | - |
| [`doc/画面キャプチャ機能説明書.md`](../doc/画面キャプチャ機能説明書.md) | 画面キャプチャ | WindowsScreenCapture は選択したウィンドウをWindows Graphics Capture（WGC）で取得し、WGCを開始できない場合は既存のWin32/GDI経路へ切り替える。 | - | - |
| [`doc/知識ベース機能説明書.md`](../doc/知識ベース機能説明書.md) | 知識ベース | KnowledgeStore はゲーム単位の knowledge.json を管理する。事実はカテゴリ・対象・記述・信頼度を持ち、キーワードとカテゴリで検索できる。書き込みは一時ファイル置換で行う。 | - | - |
| [`doc/観測入力機能説明書.md`](../doc/観測入力機能説明書.md) | 観測入力 | ObservationSource はゲーム画面の取得方法を抽象化する。現在は再現テスト用の JsonObservationSource を提供し、1つのJSONから画面観測と候補操作を読み込む。将来 | - | - |
| [`doc/設定永続化機能説明書.md`](../doc/設定永続化機能説明書.md) | 設定と履歴の永続化 | ConfigStore はProvider、モデル、Endpoint、人格、目的を config.json に保存する。未作成時は安全なローカル規則設定を返し、未知の設定キーは無視する。 | - | - |
| [`doc/評価指標機能説明書.md`](../doc/評価指標機能説明書.md) | 評価指標 | MetricsCalculator は実行履歴から総数、dry-run数、実行成功数、失敗数、操作別回数を集計する。GUI品質評価では、この集計を迷い時間・同一画面表示回数などの指標へ拡張する。 | - | - |
| [`doc/開発予定.md`](../doc/開発予定.md) | この文書について | AIGamePlayerにて開発予定のものをまとめる | - | - |
