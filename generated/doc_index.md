<!-- 自動生成: tools/generate_doc_index.py。手で編集しない。 -->
# 文書目次

文書を探す前にここを読みます。`doc/`配下の全文書と、主要なルート文書（README、AGENTS、AI_CONTEXT、key_info）の題名と先頭の1文を載せます。内容の正本は各文書です。

| 文書 | 題名 | 先頭の要約 | 親Issue | 確認日 |
|---|---|---|---|---|
| [`README.md`](../README.md) | AI Game Player | 未知のGUIゲームを、画面観測・候補評価・AI判断・安全な操作・結果評価のループでプレイする実験用エンジンです。 | - | - |
| [`AGENTS.md`](../AGENTS.md) | AI Game Player: Agent Guide | Do not begin by reading the entire repository, every document, all Issues, generated diagrams, or r… | - | - |
| [`AI_CONTEXT.md`](../AI_CONTEXT.md) | AI Context | Codex / AI が最初に読む小さい索引。 | - | - |
| [`key_info.md`](../key_info.md) | AI Game Player 現在の機能 | 確認基準：2026-11-01、統合feature 77a8995e。 | - | - |
| [`doc/ActionSafetyEvaluator機能説明書.md`](../doc/ActionSafetyEvaluator機能説明書.md) | Action Safety Evaluator 機能説明書 | ActionSafetyEvaluator は、Decisionが選んだ ActionCandidate を実行前に独立評価し、その候補を実行候補として信用してよいかを SAFE / SUSPICI… | - | - |
| [`doc/Automation機能説明書.md`](../doc/Automation機能説明書.md) | Automation候補検出 | ConfiguredRegionDetector はゲーム固有の automation.json に定義された名前付き矩形を ActionCandidate へ変換する。 | - | - |
| [`doc/Challenger更新機能説明書.md`](../doc/Challenger更新機能説明書.md) | Challenger更新（Challenger Update Pipeline）機能説明書 | Issue 170（親 36）の今回のスライスを説明します。 | - | - |
| [`doc/DecisionContext機能説明書.md`](../doc/DecisionContext機能説明書.md) | Decision Context機能説明書 | Recognition / Evaluation / Decision を分離し、小型Decision LLMへraw認識結果ではなく、候補単位に整理した DecisionContext を渡します。 | - | - |
| [`doc/E2E受入試験手順.md`](../doc/E2E受入試験手順.md) | E2E受入試験手順 | Issue 30 の第一段階受入として、人間が候補JSONを編集せずに Screen - State - Decide - Act - Observe - Evaluate - next Decis… | - | - |
| [`doc/Experience Reader機能説明書.md`](../doc/Experience%20Reader機能説明書.md) | Experience Reader | ai_game_player.experience_reader.JsonlExperienceReader は、Canonical Episode を1行1件のJSON Lines形式で読み込みま… | - | - |
| [`doc/FailSafeRuntime説明書.md`](../doc/FailSafeRuntime説明書.md) | Fail-safe Runtime / OS Emergency Stop | Issue 40 のFail-safe Runtimeは、Actionの意味や通常の入力妥当性ではなく、上位AIや制御processそのものが壊れた場合にも入力権限を失効させるための実行基盤である。 | - | - |
| [`doc/NativeRuntime接続機能説明書.md`](../doc/NativeRuntime接続機能説明書.md) | Native Runtime Python接続 | ai_game_player.runtime.nativeはC ABI v1に対する薄いctypes接続層です。 | - | - |
| [`doc/OCR候補検出機能説明書.md`](../doc/OCR候補検出機能説明書.md) | OCR候補検出 | OcrTextCandidateDetector はOCRエンジンの出力（文字列と矩形）を、認識共通中間表現 DetectedElement へ変換する。 | - | - |
| [`doc/OutcomeDetectionFusion機能説明書.md`](../doc/OutcomeDetectionFusion機能説明書.md) | Outcome Detection / Fusion 機能説明書 | Issue 34 のOutcome Detectionは、Before Observation + Action + After Observation から「何が起きたか」を構造化する責務を持つ。 | - | - |
| [`doc/Pythonコードの整形.md`](../doc/Pythonコードの整形.md) | 変更したPythonファイルを整形する | この手順はPythonコードを変更する開発者向けです。 | - | - |
| [`doc/Python証明書信頼棚卸し.md`](../doc/Python証明書信頼棚卸し.md) | Python証明書信頼の棚卸し（Issue 513 / 親Issue 482） | この文書は、AI Game Playerの開発・CI・配布で使うPython環境が、実際にどのpip・certifi・uv・sigstoreと、どの証明書信頼源（CA bundleまたはOSの信頼ス… | - | - |
| [`doc/RecoveryController機能説明書.md`](../doc/RecoveryController機能説明書.md) | Recovery Controllerの機能説明書 | この説明書は、Issue 38のSelfCheckMonitorが返すSelfCheckEventを受けて、再観測・再判断・停止要求を有限回で段階的に進めるRecoveryController（Is… | - | - |
| [`doc/SafetyGuard説明書.md`](../doc/SafetyGuard説明書.md) | SafetyGuard / Emergency Stop 説明書 | Issue 33 の最終防衛線として、LLM・Recognition・Action Safety Evaluatorの判断に依存しない決定論的な入力Guardを提供する。 | - | - |
| [`doc/UI埋め込み機能説明書.md`](../doc/UI埋め込み機能説明書.md) | UI埋め込み機能 | ui_embedding.py はUI crop/regionを検索用特徴へ変換し、同一ゲーム再認識とゲーム横断prototype検索へ利用する基盤を提供する。 | - | - |
| [`doc/UI認識メモリ機能説明書.md`](../doc/UI認識メモリ機能説明書.md) | UI Detection / Recognition Memory | Issue 10 の初期実装は、未知ゲーム向けの汎用detectorと、プレイ済みゲーム向けの軽量Recognition Memoryを分離する。 | - | - |
| [`doc/UPDCommander導入方針.md`](../doc/UPDCommander導入方針.md) | UPD Commander 導入方針と品質Checker | upd-commander-base-design の UI / Process / Data、Commander / Messenger / Processing の責務分離を AI Game P… | - | - |
| [`doc/UnknownnessMonitor機能説明書.md`](../doc/UnknownnessMonitor機能説明書.md) | Unknownness Monitorの機能説明書 | この説明書は、画面認識・知識検索・候補評価などの検出器を追加する実装者が、検出器の不確かさをUnknownnessMonitorへ渡し、scope別の瞬間判定と持続判定を確認するための説明です。 | - | - |
| [`doc/Windows安全統合ゲート.md`](../doc/Windows安全統合ゲート.md) | Windows安全統合ゲート（Issue 94） | GitHub ActionsのWindows runnerで、Windows固有のCapture・Window targeting・Input・Safety・bundleの回帰を検出する検査の一覧で… | - | - |
| [`doc/Windows配布bundle説明書.md`](../doc/Windows配布bundle説明書.md) | Windows配布bundle説明書（Issue 149、515） | この文書は、tools/build_windows_bundle.ps1が作るWindows用配布物（以下、bundle）の配置と、bundleの起動・同梱CAを確かめる検査を説明します。 | - | - |
| [`doc/architecture/README.md`](../doc/architecture/README.md) | 動作と配置の見取り図：詳しく読む入口 | このフォルダーには、開発者向けの実装配置資料があります。 | - | - |
| [`doc/architecture/artifact_store.md`](../doc/architecture/artifact_store.md) | Content-addressed Artifact Store | ArtifactStore defines the backend-neutral API; FileSystemArtifactStore is the local immutable imple… | - | - |
| [`doc/architecture/autonomous_player_goal_authority.md`](../doc/architecture/autonomous_player_goal_authority.md) | 自律Playerと権限分離：Goalは賢者ミスク本人が決める（845） | 過去の設計は「利用者がGUIで目標を与え、システムがそれを最優先で実行する」前提を含んでいました。 | - | - |
| [`doc/architecture/belief_snapshot.md`](../doc/architecture/belief_snapshot.md) | 型付き BeliefSnapshot と有界候補 Store | Issue 478 の子 563 が実装する Versioned Bounded BeliefSnapshot Contract + Candidate Dedup/Residual Mass の契… | - | - |
| [`doc/architecture/belief_update.md`](../doc/architecture/belief_update.md) | 状態仮説を予測し、実観測の尤度で更新する | この資料は汎用ゲームプレイヤーの認識・計画を接続する開発者向けです。 | - | - |
| [`doc/architecture/campaign_runner.md`](../doc/architecture/campaign_runner.md) | Campaign Runnerの実行契約 | 2048 campaignを調査する開発者は、report.jsonのfailure_detailに付くobservation_timingを確認してください。 | - | - |
| [`doc/architecture/canonical_experience_schema.md`](../doc/architecture/canonical_experience_schema.md) | Canonical Experience Schema | ExperienceEpisode, ExperienceStep, and ExperienceEvent in src/ai_game_player/experience.py define t… | - | - |
| [`doc/architecture/capability_manifest.md`](../doc/architecture/capability_manifest.md) | Optional Capability Manifest | Optional features declare their capability IDs, required and optional dependency IDs, lifecycle, co… | - | - |
| [`doc/architecture/execution_receipts.md`](../doc/architecture/execution_receipts.md) | 実行受領記録と操作後観測 | Issue 628は、保存した判断だけを直前の実操作として扱っていた帰属の誤りを修正する。 | - | - |
| [`doc/architecture/expectimax_planning.md`](../doc/architecture/expectimax_planning.md) | Belief-aware bounded Expectimax | Issue 565（親 480 / 478）は、typed immutable Beliefから複数worldとchanceを評価する汎用Coreと、停止可能な制限workerを提供します。 | - | - |
| [`doc/architecture/flow_for_readers.md`](../doc/architecture/flow_for_readers.md) | 一回の判断と実行は、何をどの順番で行う？ | 例えば、前回「開始」を押したという記録がある場合、次に取得した画面でメニューが消えたか、別画面へ進んだかなどを調べます。 | - | - |
| [`doc/architecture/hook_runtime.md`](../doc/architecture/hook_runtime.md) | Processing Hook Runtime：stage境界のread/modify/inject | Issue 151（親 58）で実装する、Pipelineのstage境界へStageEnvelope v1を渡してHookを実行する仕組みです。 | - | - |
| [`doc/architecture/inference_provider_api.md`](../doc/architecture/inference_provider_api.md) | Inference Provider API v1 | Kadoka Core must not depend directly on one model server or vendor API. | - | - |
| [`doc/architecture/knowledge_builder.md`](../doc/architecture/knowledge_builder.md) | Experienceから操作効果を数値Knowledgeへ増分集計する | この文書は、モデルの再学習を行わない環境でもプレイ経験を次の判断へ使うための 開発者向け説明です。 | - | - |
| [`doc/architecture/knowledge_feature_adapter.md`](../doc/architecture/knowledge_feature_adapter.md) | Knowledgeを一回の判断へ固定してsoft evidenceとして使う | この文書は、ゲーム経験から得たKnowledgeを判断へ接続する開発者向けです。 | - | - |
| [`doc/architecture/knowledge_promotion.md`](../doc/architecture/knowledge_promotion.md) | 知識の更新候補を評価し、本番版の参照先を切り替える | この文書はKnowledgeを学習・公開する処理を実装する開発者向けです。 | - | - |
| [`doc/architecture/knowledge_snapshot.md`](../doc/architecture/knowledge_snapshot.md) | version付きKnowledge Snapshotと安全な読取 | この文書はKnowledge Builder・Decision Adapter・Promotion Gateの実装者向けです。 | - | - |
| [`doc/architecture/learning_capability_contract.md`](../doc/architecture/learning_capability_contract.md) | Learning Capability Contract | LearningCapabilityManifest in src/ai_game_player/learning.py is the versioned declaration for a mod… | - | - |
| [`doc/architecture/light_adaptation.md`](../doc/architecture/light_adaptation.md) | Inference + Light Adaptation runner (422) | RuntimeComposition.create_session_runtime は実 DecisionPipeline に SessionLightAdaptation を接続する。 | - | - |
| [`doc/architecture/native_abi_v1.md`](../doc/architecture/native_abi_v1.md) | Native Runtime C ABI v1 | native/include/kadoka/runtime_api.h is the public, C-compatible boundary between the high-level app… | - | - |
| [`doc/architecture/native_runtime_boundary.md`](../doc/architecture/native_runtime_boundary.md) | Native Runtimeの配置宣言と境界コストを検査する | この文書は、AI Game PlayerのPythonとC++の責務を変更する開発者向けです。 | - | - |
| [`doc/architecture/native_security_profile.md`](../doc/architecture/native_security_profile.md) | Native C++ Security Profile（Issue 115 第1段） | この文書は、Native Runtime（native/）のC/C++ソースへ適用するSecurity検査の構成、判定基準、実行方法、未対応範囲を記録します。 | - | - |
| [`doc/architecture/planner_runtime.md`](../doc/architecture/planner_runtime.md) | Planner → Decision と receding-horizon 実行 | Issue 567（親 480）。 | - | - |
| [`doc/architecture/planning_risk_information.md`](../doc/architecture/planning_risk_information.md) | 観測価値と下振れリスクを使うPlanning Policy | Issue 566は、探索が返す予測分布から、操作提案・追加観測・保留・追加判断要求を選ぶCoreです。 | - | - |
| [`doc/architecture/plugin_capability_manifest.md`](../doc/architecture/plugin_capability_manifest.md) | Plugin Capability Manifest v1：Plugin権限の宣言と判定 | Issue 152（親 58）で定義する、PluginがどのHook接続点で何をするかを宣言する版付き契約と、その宣言だけを根拠にHookを許可する判定です。 | - | - |
| [`doc/architecture/privacy_data_inventory.md`](../doc/architecture/privacy_data_inventory.md) | Privacy Data Inventory（data class・1.0既定policy） | Privacy条件が文章だけだと、Provider・Recorder・Learningで保存や送信の判断がずれます。 | - | - |
| [`doc/architecture/qt_rust_migration.md`](../doc/architecture/qt_rust_migration.md) | Qtの画面とRustの常駐Coreへ移る設計 | Issue 661 の決定により、恒久GUIは Qt 6 Widgetsを使うC++のネイティブ画面、常駐する実行・状態・操作許可の中心は Rust Core とします。 | - | - |
| [`doc/architecture/reference_2048_recognition.md`](../doc/architecture/reference_2048_recognition.md) | 固定版2048の発光・得点認識回帰（Issue 659） | この修正は任意の参照2048 Adapterの画像認識に限定する。 | - | - |
| [`doc/architecture/reference_browser_post_failure_diagnostics.md`](../doc/architecture/reference_browser_post_failure_diagnostics.md) | 参照ブラウザーの失敗後だけに行う描画診断（明示検証のみ） | Windows の initial observation が全空盤面のまま期限切れになる場合、 画素だけでは未実行の初期 RAF、DOM 構築、CSS 表示、GDI の合成面取得を 分離できない。 | - | - |
| [`doc/architecture/reference_http_accept_queue.md`](../doc/architecture/reference_http_accept_queue.md) | 参照ゲームの私有HTTPサーバーと起動時burst | LoopbackStaticServer は所有する固定版2048だけを、episodeごとの 127.0.0.1 / ephemeral portから配信する。 | - | - |
| [`doc/architecture/repository_state.md`](../doc/architecture/repository_state.md) | Repository Map: Pythonソース構造の機械可読インデックス | この文書は、Codex支援ツールの利用者と実装者に向けて、Pythonソースの宣言構造を検索する generated/repo_map.json の生成方法と収録範囲を説明します。 | - | - |
| [`doc/architecture/resource_stability_gate.md`](../doc/architecture/resource_stability_gate.md) | Long-session Resource Stability Gate | 最終確認日: 2026-11-07 / 状態: 実装済み（今回のスライス） / 関連: 設計書 787・Runtime Resource Lifecycle (786)・Issue 250（実機30… | - | - |
| [`doc/architecture/retention_core.md`](../doc/architecture/retention_core.md) | Retention Core（budget / Protected / TTL / exact dedup） | Experienceの重いpayload（frame・video segment・model output等）はContent-addressed Artifact StoreへSHA-256参照で… | - | - |
| [`doc/architecture/runtime_evaluation.md`](../doc/architecture/runtime_evaluation.md) | Runtime Evaluation (491 / 元8) | 既存 PrimitiveEvaluator の6軸を、既定の実Session / GamePlayerEngine / Decision Context / Traceへ接続する。 | - | - |
| [`doc/architecture/runtime_layers.md`](../doc/architecture/runtime_layers.md) | Runtime Architecture | Kadoka is intentionally modular internally while remaining minimal for ordinary users. | - | - |
| [`doc/architecture/runtime_map_for_readers.md`](../doc/architecture/runtime_map_for_readers.md) | どの部品がどこで動く？ | これは役割をまとめた図で、すべての関数呼出しを一対一に描くものではありません。 | - | - |
| [`doc/architecture/runtime_modes.md`](../doc/architecture/runtime_modes.md) | Runtime Mode / Phase 契約 | Issue 420 の基盤である 421 の実装契約です。 | - | - |
| [`doc/architecture/runtime_resource_lifecycle.md`](../doc/architecture/runtime_resource_lifecycle.md) | Runtime Resource Lifecycle（Session Resource Registry / Cleanup Coordinator） | Session内で確保・借用される資源（共有Frame、GPU cache、capture session等）の所有者と解放経路を1か所に集め、release・task終了・世代切替・Stop・er… | - | - |
| [`doc/architecture/session_event_journal.md`](../doc/architecture/session_event_journal.md) | Session Event Journal | EventEnvelope in src/ai_game_player/event_journal.py is the versioned session-event contract. | - | - |
| [`doc/architecture/stage_envelope.md`](../doc/architecture/stage_envelope.md) | StageEnvelope v1：stage間の中間データ契約 | Issue 150（親 58）で定義する、Pipeline各stageの境界で中間データを外部拡張（Hook・Provider・Replay）へ渡すための版付き契約です。 | - | - |
| [`doc/architecture/startup_failure_evidence.md`](../doc/architecture/startup_failure_evidence.md) | 2048起動失敗の診断保存 | Issue 637は、自然なbrowser起動失敗の原因を調べるための証拠保存を追加する。 | - | - |
| [`doc/architecture/transition_simulation.md`](../doc/architecture/transition_simulation.md) | 仮想操作と確率遷移を実入力から分離する | この文書は汎用ゲームプレイヤーの探索機能を実装する開発者向けです。 | - | - |
| [`doc/architecture/windows_long_comparison.md`](../doc/architecture/windows_long_comparison.md) | Windows長枠3 Provider比較(Issue 795) | リポジトリ直下から実行し、先に PYTHONPATH=src;.(PowerShellでは $env:PYTHONPATH = "src;.")を設定します。 | - | - |
| [`doc/contributor/documentation_update_guide.md`](../doc/contributor/documentation_update_guide.md) | 日本語の設計図と説明書を更新するとき | この手順は、ゲームの実装が変わったときに「分かりやすい図は古い実装を説明している」という問題を防ぐためのものです。 | - | - |
| [`doc/contributor/subsystem_ownership_map.md`](../doc/contributor/subsystem_ownership_map.md) | Contributor向けサブシステム責務とレビュー先 | このページは、Kadoka AI Game Playerへコード・テスト・文書を提供するContributor（貢献者）が、変更する場所、守る境界、相談するレビュー先を選ぶための地図です。 | - | - |
| [`doc/design/issue-152-capability-manifest.md`](../doc/design/issue-152-capability-manifest.md) | Issue 152 設計：Plugin Capability Manifest / Enforcement Contract | 今回のスライスは「宣言の契約（schema・参照validator・適合ケース）」と「in-process Hook runtimeでのdeny-by-default判定」まで。 | #58（関連: #42 Optional Parent、#235 Isolation、#224 Data Inventory） | - |
| [`doc/design/issue-170-challenger-update.md`](../doc/design/issue-170-challenger-update.md) | Issue 170 設計：Challenger Update Pipeline（今回のスライス） | 今回のスライス: proposal schema、strategy選択（calibration / threshold / prototype の3 baseline）、Champion/Chall… | #36（兄弟: #171 Promotion / Rollback Gate、#173 Candidate Evaluation Runner。関連: #647、#712、#426、#427） | - |
| [`doc/design/issue-224-data-inventory.md`](../doc/design/issue-224-data-inventory.md) | Issue 224 設計: Privacy Data Inventory / Defaults | - | - | - |
| [`doc/design/issue-495-candidate-identity.md`](../doc/design/issue-495-candidate-identity.md) | 候補の同一性ガード（CandidateIdentityGuard）仕様（495） | tests/test_candidate_identity.pyと既存tests/test_candidate_merger.pyで次を検証します。 | - | - |
| [`doc/design/issue-729-planner-test-load.md`](../doc/design/issue-729-planner-test-load.md) | Issue 729 高負荷時に結果を期待するテストを決定的にする | - | なし | - |
| [`doc/design/issue-786-session-resource-registry.md`](../doc/design/issue-786-session-resource-registry.md) | Issue 786 Session Resource Lease Registry / Cleanup Coordinator | Session内で確保・借用されるruntime resourceの所有者・lease・解放経路を1か所に列挙し、release / task終了 / generation切替 / Stop / e… | #785 | - |
| [`doc/design/issue-787-long-session-resource-stability-gate.md`](../doc/design/issue-787-long-session-resource-stability-gate.md) | Issue 787 Long-session Resource Stability Gate | 長時間のSessionで資源使用量が単調増加し続けないことを、peakだけでなくwarmup後の傾き・Session終了後の回収・複数Session反復のbaselineで判定するHarness /… | #785 | - |
| [`doc/design/issue-788-runtime-retention-adapter.md`](../doc/design/issue-788-runtime-retention-adapter.md) | Issue 788 Runtime Retention Adapter：Session/Journal参照SnapshotとGC Triggerの本番接続 | artifact_sha256s と references の digest 集合は一致しなければ不正（ValueError）。 | #18 | - |
| [`doc/design/issue-789-artifact-admission.md`](../doc/design/issue-789-artifact-admission.md) | Issue 789 Artifact Admission Transaction | ArtifactAdmissionCoordinator(collector, , trace=None, records=None)。 | #18 | - |
| [`doc/design/issue-790-orphan-reconciliation.md`](../doc/design/issue-790-orphan-reconciliation.md) | Issue 790 Orphan Reconciliation（crash由来の未索引Artifactの安全な回収） | 利用者の操作はありません。 | #18 | - |
| [`doc/design/issue-791-2048-empty-board-diagnosis.md`](../doc/design/issue-791-2048-empty-board-diagnosis.md) | Issue 791 2048の空盤面・HTMLActuator欠落の原因カテゴリ診断 | 署名: Tomiya | #459 | - |
| [`doc/design/issue-794-observation-lease.md`](../doc/design/issue-794-observation-lease.md) | Issue 794 Observation Lease失効の時系列因果と回帰防止 | 証拠: run 41938316919 attempt 1（PR 730）と run 41941522328 attempt 1（PR 732）の--save-heartbeat-diagnosti… | #459 | - |
| [`doc/design/issue-795-windows-long-comparison.md`](../doc/design/issue-795-windows-long-comparison.md) | Issue 795 Windows長枠でCycling・Baseline・Belief Plannerを同条件比較する | 署名: Tomiya | #567（長枠比較の残件）。安定性の前提は #459/#794（merge済み）、Campaign基盤は #247 | - |
| [`doc/design/issue-806-provider-wire-data-classes.md`](../doc/design/issue-806-provider-wire-data-classes.md) | Issue 806 Provider wire schemaのdata class列挙へui_metadata・model_outputを追加し、送信payloadを全classへ対応付ける | - | #123（兄弟: #226 Remote Consent Enforcement、後続） | - |
| [`doc/design/issue-807-merge-gate-draft-retry.md`](../doc/design/issue-807-merge-gate-draft-retry.md) | Issue 807 merge gateのDraft事前検出・merge失敗本文・読取専用APIの有界再試行 | Pull request is a draft; mark it ready for review on GitHub before running the merge gate (this too… | none | - |
| [`doc/design/issue-808-preflight-deadline-margin.md`](../doc/design/issue-808-preflight-deadline-margin.md) | Issue 808 comparison preflightテストの実時計期限 | tests/test_comparison_preflight.py のsetUpの期限を time.monotonic() + 5 から + 30 へ変更する（テストのみ）。 | #729 | - |
| [`doc/design/issue-811-perf-budget-robustness.md`](../doc/design/issue-811-perf-budget-robustness.md) | Issue 811 Noisy Provider性能予算の判定をrunnerの一時揺らぎに強くする | 受入条件ごとの試験（すべて疑似時計で決定論的、ネットワーク・実I/O時間に依存しない）: | #178 | - |
| [`doc/design/issue-812-invalid-frame-planner-readiness.md`](../doc/design/issue-812-invalid-frame-planner-readiness.md) | Issue 812 Windows planning comparisonの初回入力0 — plannerのworker準備完了を待つ | 上記テストと finish_task.bat が通り、設計書とレビュー記録を同じPRに含め、PRのCIでseed 479-481が成功する。 | #729 | - |
| [`doc/design/issue-817-http-test-race.md`](../doc/design/issue-817-http-test-race.md) | Issue 817 reference game HTTPサーバーの並行10 scriptテストの競合 | tests/test_reference_game_http_server.py::test_concurrent_ten_scripts_preserve_bytes_counters_and_p… | #729 | - |
| [`doc/design/issue-864-macro-trigger.md`](../doc/design/issue-864-macro-trigger.md) | Issue 864 重い判断機の非同期化と、判断・操作の層分離（MacroTrigger） | 設計資料のみ。 | #19 | 2026-11-08 |
| [`doc/design/issue-866-decision-architecture.md`](../doc/design/issue-866-decision-architecture.md) | Issue 866 判断・操作の層構造と、モデル運用・学習・知識増築・データ圧縮の統合設計 | 設計資料のみ。 | #19 | 2026-11-08 |
| [`doc/design/issue-868-human-cognition-tasks.md`](../doc/design/issue-868-human-cognition-tasks.md) | Issue 868 人間のゲームプレイ中の認知をタスクとして洗い出し、タスク別のモデル・手法を割り当てる | 設計資料のみ。 | #866 | 2026-11-08 |
| [`doc/design/issue-895-reading-indexes.md`](../doc/design/issue-895-reading-indexes.md) | Issue 895 読む時のトークンを減らす目次・責務表・依存図の自動生成 | 設計書やソースを読む時に、探すためだけに読む量を減らす。 | #19 | 2026-11-09 |
| [`doc/guide/README.md`](../doc/guide/README.md) | AI Game Player：まず全体を知る | READMEへ戻る ｜ 目指す完成図と現在の実装図 ｜ 場所ごとの実装状況と根拠 ｜ 用語集 | - | - |
| [`doc/guide/implementation_status.md`](../doc/guide/implementation_status.md) | 場所ごとの実装状況と、完成までの残作業 | 「部品あり」はコードが存在すること、「接続あり」は標準の操作から呼ばれることです。 | - | - |
| [`doc/guide/judgment.md`](../doc/guide/judgment.md) | 次の操作をどう決める？ 前回の結果をどう確かめる？ | このページは、候補を評価すること、前回の操作結果を調べること、今回の操作を選ぶこと、回答を検証することを別々の仕事として説明します。 | - | - |
| [`doc/guide/orchestration.md`](../doc/guide/orchestration.md) | 処理を選ぶ調整役と、精度・時間の配分 | 実装したい調整役は、候補の処理時間を見積もり、追加したときに判断がどの程度改善しそうかを比較します。 | - | - |
| [`doc/guide/recognition.md`](../doc/guide/recognition.md) | 画面をどう読み取り、操作候補に変えるの？ | 古い画像は捨て、待ち時間の期限まで新しい画像を待ちます。 | - | - |
| [`doc/guide/records_learning.md`](../doc/guide/records_learning.md) | ゲームを動かした記録は、どう残し、何に使う？ | このアプリは、画面を見て判断したことや入力の結果を、後で確かめられるように記録する仕組みを持っています。 | - | - |
| [`doc/guide/safety.md`](../doc/guide/safety.md) | モデルが「押す」と答えたら、すぐゲームに入力される？ | このページは、操作の許可・拒否・確認運転・停止・記録を説明します。 | - | - |
| [`doc/guide/target_and_current.md`](../doc/guide/target_and_current.md) | 目指す完成形と、いま動く仕組み | 以下の二つの図は、同じ完成度を表しません。 | - | - |
| [`doc/guide/terms.md`](../doc/guide/terms.md) | 用語集：難しい言葉を日常の言葉に直す | 言葉の意味はページを開いたときにも説明します。 | - | - |
| [`doc/guide/writing_rules.md`](../doc/guide/writing_rules.md) | 詳しい資料の書き方と確認事項 | この様式は、長い説明を読みやすく分けるためのものです。 | - | - |
| [`doc/licensing_rationale.md`](../doc/licensing_rationale.md) | ライセンス方針と、その意図 | この文書は法的なライセンス本文ではなく、AI Game Player Projectがなぜ現在のライセンス構成を選んでいるかを説明するためのものです。 | - | - |
| [`doc/main保護方針.md`](../doc/main保護方針.md) | mainブランチ保護方針（Repository Ruleset） | この文書は、AI Game Playerのmainブランチへ、CIを通過していない変更が通常のマージ経路で入らないようにする設定と運用を説明します。 | - | - |
| [`doc/models/README.md`](../doc/models/README.md) | どこで、どのモデルや手法を使う？ | 実線は標準のつながり、点線は条件付きのつながりです。 | - | - |
| [`doc/models/evidence.md`](../doc/models/evidence.md) | 設定にあるモデルと、実際に動いたモデルを分けて確認する | 「設定欄にgemma3:4bとあるので、このゲームはgemma3で動いた」とは言えません。 | - | - |
| [`doc/release_1_0.md`](../doc/release_1_0.md) | 1.0.0 Release Scope and Acceptance | Issue 19 is the priority/source-of-truth. | - | - |
| [`doc/security/python_security_profile.md`](../doc/security/python_security_profile.md) | Python Security Profile | この文書は、Python層のセキュリティ静的解析（Issue 114）の判定規則、抑制の管理方法、CIでの実行方法を説明します。 | - | - |
| [`doc/security/threat_model.md`](../doc/security/threat_model.md) | Kadoka AI Game Player 脅威モデル | この文書は、Kadoka AI Game Playerの保守者とContributorが、実装・テスト・CI（コード変更を自動build / testする継続的インテグレーション）で先に扱うセキュリ… | - | - |
| [`doc/versions.md`](../doc/versions.md) | Versions | Application versionとSchema/API/ABI versionは独立して管理する。 | - | - |
| [`doc/キャプチャ観測Source機能説明書.md`](../doc/キャプチャ観測Source機能説明書.md) | キャプチャ観測Source | CapturedObservationSource はFrameCaptureとFrameAnalyzerを接続し、実画面のBGRAフレームを ScreenObservation へ変換する。 | - | - |
| [`doc/コア判断ループ機能説明書.md`](../doc/コア判断ループ機能説明書.md) | (題名なし) | - | - | - |
| [`doc/コマンド指向.md`](../doc/コマンド指向.md) | (題名なし) | - | - | - |
| [`doc/コーディングルール.md`](../doc/コーディングルール.md) | 共通コーディングルール | この文書は、AI Game Playerのコード、テスト、文書を変更するすべての作業者（人、Claude Code、Codex、ChatGPT、GitHub Copilot）が守る規則です。 | - | - |
| [`doc/コーディング規約.md`](../doc/コーディング規約.md) | コーディング規約 | AI Game Playerは複数の処理領域と言語を持ち、コードの生成・変更も継続します。 | - | - |
| [`doc/レビュー基準.md`](../doc/レビュー基準.md) | レビュー基準 | AI Game Playerは、画面認識・判断・入力・評価をつなぐアプリケーションです。 | - | - |
| [`doc/ワークフロー/ワークフロー.md`](../doc/ワークフロー/ワークフロー.md) | 開発者と評価者が使うGitHub Issue・Pull Request手順 | この文書は、AI Game Playerの開発者がGitHub Issueから変更を作り、Pull Request（PR）で確認を受ける手順を説明します。 | - | - |
| [`doc/候補統合機能説明書.md`](../doc/候補統合機能説明書.md) | 操作候補統合 | この説明書は、画面取得後にGUIがAutomation・OCR・画像候補を統合し、候補の入力元と安全評価を表示する仕組みを実装者へ説明する。 | - | - |
| [`doc/判断パイプライン機能説明書.md`](../doc/判断パイプライン機能説明書.md) | 判断パイプライン | DecisionPipeline は観測Sourceから読み込んだ画面とAutomation候補へOCR候補を加え、CandidateMerger、GamePlayerEngine の順に接続する。 | - | - |
| [`doc/学習データセット生成機能説明書.md`](../doc/学習データセット生成機能説明書.md) | Experience Training Dataset Builder | ExperienceDatasetBuilder は、Learning Capabilityが宣言するDataset schema/versionに従い、保存済みExperienceのEpisode… | - | - |
| [`doc/学習診断機能説明書.md`](../doc/学習診断機能説明書.md) | 学習診断（Training Value / Error Diagnosis）機能説明書 | Issue 169（親 36）の実装範囲を説明します。 | - | - |
| [`doc/提案メモ.md`](../doc/提案メモ.md) | (題名なし) | - | - | - |
| [`doc/操作実行機能説明書.md`](../doc/操作実行機能説明書.md) | 操作実行 | ActionExecutor は安全評価済み候補の実行境界を提供する。 | - | - |
| [`doc/根本目標.md`](../doc/根本目標.md) | 根本目標（最初に読む1ページ） | 作業を始める前に、次の3点に答えられることを確認します。 | - | - |
| [`doc/独立テストゲーム集.md`](../doc/独立テストゲーム集.md) | 独立テストゲーム集の境界と検証 | 汎用プレイヤーを多様な画面・入力方式で検証するため、634で既存ゲーム追加commit a1b7214ca97b4d188f389c0f0efbe876ff67b151の独立成果物を統合します。 | - | - |
| [`doc/画像解析機能説明書.md`](../doc/画像解析機能説明書.md) | 画像解析 | FrameAnalyzer はBGRAフレームを ScreenObservation へ変換する。 | - | - |
| [`doc/画面キャプチャ機能説明書.md`](../doc/画面キャプチャ機能説明書.md) | 画面キャプチャ | WindowsScreenCapture は選択したウィンドウをWindows Graphics Capture（WGC）で取得し、WGCを開始できない場合は既存のWin32/GDI経路へ切り替える。 | - | - |
| [`doc/知識ベース機能説明書.md`](../doc/知識ベース機能説明書.md) | 知識ベース | KnowledgeStore はゲーム単位の knowledge.json を管理する。 | - | - |
| [`doc/観測入力機能説明書.md`](../doc/観測入力機能説明書.md) | 観測入力 | ObservationSource はゲーム画面の取得方法を抽象化する。 | - | - |
| [`doc/設定永続化機能説明書.md`](../doc/設定永続化機能説明書.md) | 設定と履歴の永続化 | ConfigStore はProvider、モデル、Endpoint、人格、目的を config.json に保存する。 | - | - |
| [`doc/評価指標機能説明書.md`](../doc/評価指標機能説明書.md) | 評価指標 | MetricsCalculator は実行履歴から総数、dry-run数、実行成功数、失敗数、操作別回数を集計する。 | - | - |
| [`doc/開発予定.md`](../doc/開発予定.md) | この文書について | AI_GamePlayerにて開発予定のものをまとめる AIは設計を軽く書いても良いが深い設計については各自機能説明書.mdを作成して記入すること 表はmdの表機能を利用し、図はmermaidを使用… | - | - |
