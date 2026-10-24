# Contributor向けサブシステム責務とレビュー先

このページは、Kadoka AI Game Playerへコード・テスト・文書を提供するContributor（貢献者）が、変更する場所、守る境界、相談するレビュー先を選ぶための地図です。対象は2026-10-15に確認したmainのコードと、同日に確認したGitHub Issueです。担当候補は参加先を案内するための情報であり、Contributionやmergeの権限を制限しません。

## このページで使う用語

- Area（領域）は、共通の目的と変更理由を持つサブシステム群です。
- Area Maintainer（領域保守担当）は、Areaの設計と長期保守を引き受ける人です。Reviewer（レビュアー）は、Pull Request（PR、変更提案）の差分と検証結果を確認する人です。
- 契約境界は、別の層やツールが依存する入力、出力、形式、状態、互換性条件です。契約境界を変える変更は、定義側と利用側のAreaを両方確認します。
- CI（継続的インテグレーション）は、Pull Requestやmainの変更に対して自動テスト・静的検査を実行する仕組みです。

## 担当とレビューの現在地

GitHub上のリポジトリ所有者は`@tomiya7688`です。取得できた直近100コミットでは、作成者として確認できたアカウントは`@tomiya7688`だけでした。リポジトリには`.github/CODEOWNERS`がなく、Area Maintainerや領域別レビュアーを指名する記録もありません。この履歴だけでは、別のContributorが特定領域をレビューできるか判断できません。

全領域のArea Maintainerと領域別レビュアー候補は未指名です。現時点で変更先や設計判断に迷うContributorは、統合責任を持つリポジトリ所有者`@tomiya7688`へ相談してください。各領域の記載は専門担当者が任命済みであることを意味しません。

この地図はレビュー先を探す手掛かりです。必須レビュー数、承認条件、merge条件は定めません。PRのレビュー・merge条件はIssue #79で定義し、必須CIとmain保護はIssue #92で管理します。

## 現行コードと計画中の拡張

高水準コードは`src/ai_game_player/`、C/C++ Native Runtimeは`native/`、自動テストは`tests/`と`native/tests/`にあります。CI workflowは`.github/workflows/`にあります。

Capability ManifestとComposition Resolverは`src/ai_game_player/runtime/`に実装され、関連するIssue #219とIssue #220は完了しています。Native Runtime C ABI v1の公開ヘッダーは`native/include/kadoka/runtime_api.h`です。

現行リポジトリに`plugins/`ディレクトリはありません。MOD / Processing Hookの公開契約と実行機構は親Issue #58と子Issue #150–#153で追跡中です。Contributorは、これらのIssueで実装されるまでPluginの実装パスが存在するものとして扱わないでください。

## Area別の責務とレビュー先

各Areaの「現行コード」で`src/ai_game_player/`を先頭に付けず記したPython module名とサブディレクトリ名は、すべて`src/ai_game_player/`配下です。`native/`、`doc/`、`tests/`、`tools/`、`.github/`はリポジトリ直下です。

### Core / Architecture

- 責務: アプリケーション、Session、runtime composition、共有契約の境界を保ちます。
- 現行コード: `src/ai_game_player/application_workflow.py`、`game_session.py`、`runtime/`、`doc/architecture/`。
- 公開境界: `RuntimeBackend`、`RuntimeCapability`、`RuntimeDescriptor`、`CapabilityManifest`、`CompositionResolver`がruntime capabilityの宣言と選択を担います。`doc/architecture/runtime_layers.md`が高水準層とNative層の依存規則を説明します。
- 関連Issue: #32、#42、#56、#148。
- 作業に必要な知識: 変更するPython moduleと対応テストの読み方。
- あると役立つスキル: PythonのProtocol / 型、依存注入、ライフサイクル設計、API互換性。
- レビュー感度: 高。共有契約、Session lifecycle、層間依存を変える場合は関連AreaもPRに記載してください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Native Runtime / Windows Capture / Input

- 責務: PythonとC++のABI、Windows画面取得、Win32入力境界、Native resourceの所有権を保ちます。
- 現行コード: `native/include/kadoka/runtime_api.h`、`native/src/`、`native/tests/`、`src/ai_game_player/windows_abi.py`、`windows_input.py`、`screen_capture.py`、`capture_geometry.py`、`capture_region.py`。
- 公開境界: Nativeの外部入口はC ABI（C言語からも呼び出せる共有ライブラリの関数契約）v1です。`kadoka_runtime_*`、`kadoka_wgc_*`、`kadoka_safety_validate_action`関数と、ABI version・status code・frame lease（フレーム資源を借りる期間）の条件を`doc/architecture/native_abi_v1.md`で定義します。C++ STL型はABIを越えません。
- 関連Issue: #56、#60、#94、#148、#149。
- 作業に必要な知識: 変更対象がWindows専用の場合はWindows APIと対応するfake / integration testの実行方法。
- あると役立つスキル: C++20、Win32、C ABI、Python ctypes、Direct3D、handle / bufferの所有権管理。
- レビュー感度: 高。ABI、HWND（Windowsがウィンドウを識別する値）、画素形式、OS handleの寿命、実入力方式の変更は互換性や他ウィンドウへの影響を確認してください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Safety / Fail-safe

- 責務: 候補操作の安全評価、実入力許可、対象Windowの検証、停止、入力解放、異常時のfail-closed動作を保ちます。
- 現行コード: `src/ai_game_player/action_safety.py`、`safety_guard.py`、`action_executor.py`、`execution_mode.py`、`fail_safe_runtime.py`、`run_control.py`、`loop_guard.py`、`windows_input.py`。
- 公開境界: `ActionSafetyEvaluator`が候補とEvidenceを評価し、`SafetyGuard`がTarget / session状態を確認します。`FailSafeRuntime`はlease、heartbeat、入力ledger、watchdogの状態を管理します。最終的なWindows入力を変更する場合はNative / Windows Areaも含めてください。
- 関連Issue: #33、#40、#43、#94、#459。
- 作業に必要な知識: dry-runと実入力の違い、該当する安全条件、異常経路のテスト。
- あると役立つスキル: fail-safe設計、Windows入力、状態機械、プロセス間通信、脅威分析。
- レビュー感度: 高。安全判定、停止条件、実入力、全入力解放、watchdog（異常を監視して停止を促す別プロセス）の変更は正常経路だけでなく拒否・停止・回復経路を検証してください。fail-closedとは、安全状態を確認できないときに実入力を許可しない動作です。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Perception / OCR / CV

- 責務: 画面をObservationへ変換し、テキスト・UI要素・領域・操作候補と不確実性を保持します。
- 現行コード: `src/ai_game_player/observation_service.py`、`screen_capture.py`、`frame_analyzer.py`、`frame_preprocessor.py`、`ocr_*.py`、`region_detector.py`、`ui_recognition.py`、`bright_region_detector.py`、`candidate_merger.py`。
- 公開境界: `ScreenObservation`、`DetectedElement`、`ActionCandidate`は`models.py`で定義します。画像認識の結果を実行可能な候補として採用する条件は、PerceptionとSafetyの両方に関わります。
- 関連Issue: #60、#94、#333、#337。
- 作業に必要な知識: 対象画像形式、候補の座標系、既存のsynthetic image fixture（再現用に固定した合成画像テストデータ）。
- あると役立つスキル: OCR、OpenCV / CV、画像座標変換、confidence（認識の確信度）設計、fixture設計。
- レビュー感度: 中。候補の信頼度・座標・実行可否を変える場合は高感度としてSafetyもレビュー先に加えてください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Reasoning / LLM / Model Routing

- 責務: 候補と画面の文脈から操作判断を作り、Providerの応答を検証に渡します。
- 現行コード: `src/ai_game_player/provider.py`、`pipeline.py`、`engine.py`、`decision_context.py`、`decision_verifier.py`、`runtime/manifest.py`、`runtime/composition.py`。
- 公開境界: 現行Providerは`RuleProvider`と`OllamaProvider`です。Providerは`ActionCandidate`を含む判断入力を受け取り、`ActionDecision`を返します。LLMに未検証の任意座標やOS入力を直接渡す境界ではありません。Remote Provider API v1は親Issue #174と実装Issue #239で追跡中です。
- 関連Issue: #42、#174、#219、#220、#239。
- 作業に必要な知識: Decision入力と許可候補の契約、Provider出力の検証テスト。
- あると役立つスキル: Python、Ollama / HTTP API（HTTP通信でProviderと交換するAPI契約）、JSON Schema、モデルrouting、画面文字列によるprompt injection（LLMへの指示混入）を含む入力境界の検討。
- レビュー感度: 高。Providerの出力が操作候補やDecisionの受け入れ条件を変える場合はSafetyもレビュー先に加えてください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Evaluation / Outcome / Experience / Learning

- 責務: 画面差分や終端表示などのEvidence（結果を判断する根拠）から操作結果を評価し、Episode / Step / Eventを保存・検索し、学習用データのprovenance（由来）を保ちます。
- 現行コード: `src/ai_game_player/evaluator.py`、`outcome*.py`、`decision_verifier.py`、`experience.py`、`experience_reader.py`、`learning.py`、`learning_dataset.py`、`artifact_store.py`、`campaign_*.py`。
- 公開境界: `OutcomeAssessment`と`DeterministicOutcomeFusion`が状態変化の評価を担います。`ExperienceEpisode`、`ExperienceStep`、`ExperienceEvent`はversioned Experience schema v1（版番号付きJSONデータ形式）を構成し、学習能力は`LearningCapabilityManifest`で宣言します。
- 関連Issue: #43、#166–#182、#247。
- 作業に必要な知識: 変更対象のschema、既存データの読み書き条件、対応fixture。
- あると役立つスキル: 統計評価、data provenance、schema migration、content-addressed storage、benchmark設計。
- レビュー感度: 高。永続schema、label、provenance、sensitive artifactの扱いを変える場合は互換性と既存データへの影響を記載してください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### UI / Packaging / Distribution

- 責務: 利用者が対象Windowと実行modeを選び、状態を確認してアプリを起動できる入口を保ちます。
- 現行コード: `src/ai_game_player/app.py`、`application_workflow.py`、`ui/`、`__main__.py`、`profiles/`、`tools/build_windows_bundle.ps1`。
- 公開境界: `Application`が画面操作と表示を担当し、`ApplicationWorkflowService`と`GameSessionController`へ処理を委譲します。通常利用の起動・配布条件はREADMEとWindows bundle scriptで確認してください。
- 関連Issue: #57、#138、#149、#245。
- 作業に必要な知識: 利用者の操作順と、変更するUI / bundleに対応するGUIまたは起動テスト。
- あると役立つスキル: Tkinter、Windows packaging、PyInstaller、installer / runtime dependency、ユーザビリティ試験。
- レビュー感度: 中。実入力許可・対象Window・停止表示・Native libraryの配布を変える場合はSafetyまたはNativeもレビュー先に加えてください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Testing / E2E / CI

- 責務: unit test、Native test、Windows integration、E2E、performance gateが変更の影響を正しく検出する状態を保ちます。
- 現行コード: `tests/`、`native/tests/`、`tools/`、`.github/workflows/`、`finish_task.bat`、`finish_pr.bat`。
- 公開境界: workflow名とrequired check、各`tools/run_*.py`のCLI引数・終了code、および別jobが読むJSON reportはCIとの接続面です。report schemaを変更する場合は利用側も確認してください。
- 関連Issue: #85–#94。高速PRとscheduled deep CIは#91、Required Checksとmain保護は#92で追跡中です。
- 作業に必要な知識: テスト対象の実行環境、fixture、CI上のcheck名と完了条件。
- あると役立つスキル: pytest / unittest、GitHub Actions、CMake、E2E再現性、性能統計。
- レビュー感度: 高。required check、skip条件、保護ルール、実入力テストの隔離を変える場合は、その変更がmerge gateへ与える影響を明記してください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

### Documentation / Contributor Experience

- 責務: 利用者とContributorが、目的、前提、操作手順、現行制約、次の作業を文書だけから判断できる状態を保ちます。
- 現行コード: `README.md`、`AI_CONTEXT.md`、`AGENTS.md`、`key_info.md`、`doc/`、`.github/`のIssue / PR template。
- 公開境界: 利用者向け手順はREADMEと機能説明書、技術契約は`doc/architecture/`、Codexの作業入口は`AI_CONTEXT.md`と`AGENTS.md`が担います。Issueは要求・優先度・議論の正本です。
- 関連Issue: #19、#75–#84、特に#76、#77、#79、#83。
- 作業に必要な知識: 文書の想定読者、対象実装・test・Issueを根拠として照合する方法。
- あると役立つスキル: 日本語技術文書、OSS onboarding、Markdown、Issue / PR運用。
- レビュー感度: 中。安全条件、外部契約、利用者の実入力手順に関わる説明変更は対応する実装とtestも照合してください。
- 担当候補: Area Maintainer / 領域別レビュアーは未指名です。初回相談先は`@tomiya7688`です。

## 複数Areaにまたがる変更のレビュー先

PRの本文には、変更したAreaをすべて列挙してください。次の例のように、共有契約を利用する側と定義する側を両方レビュー先へ含めます。

- `ActionCandidate`の座標やconfidenceを変える場合は、PerceptionとReasoningを挙げます。候補の実行可否も変える場合はSafetyを加えます。
- Windows captureのframe / HWND契約を変える場合は、Native RuntimeとPerceptionを挙げます。入力leaseや停止動作にも触れる場合はSafetyを加えます。
- Experience schemaを変える場合はEvaluation / Learningを挙げます。runtime境界も変える場合はCore / Architectureを加えます。
- Windows bundleやCI必須checkを変える場合はUI / PackagingまたはTesting / CIを挙げ、runtime libraryの配布も変える場合はNative Runtimeを加えます。

このArea一覧はレビュー担当者をまだ指名していないため、実際の必須レビュー条件にはなりません。PR reviewの承認条件はIssue #79で合意し、GitHubのrequired checkによるmerge制御はIssue #92で定義してください。

## CODEOWNERS導入の判断

2026-10-15時点では`.github/CODEOWNERS`を追加しません。GitHub上でArea Maintainerや領域別レビュアーが指名されておらず、確認できた直近100コミットにも`@tomiya7688`以外の作成者がいません。全パスを同じアカウントへ割り当てても、領域別レビュー経路を作れず、Contributorが独立した担当者を選ぶ助けになりません。

Issue #79でレビュー条件を決め、Areaごとにレビューを引き受けるContributorまたはTeamが指名され、GitHub上で該当PRをreviewできる権限を持った時点でCODEOWNERSの追加を再検討します。CODEOWNERSはreviewer discoveryに使い、必須承認やrequired checkによるmerge制御はIssue #92のrulesetで別に扱います。
