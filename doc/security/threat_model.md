# Kadoka AI Game Player 脅威モデル

この文書は、Kadoka AI Game Playerの保守者とContributorが、実装・テスト・CI（コード変更を自動build / testする継続的インテグレーション）で先に扱うセキュリティ脅威を判断するために使います。確認基準は2026-10-15の`origin/main`（`e4d07554f129b90f4a3ecf23b8efe229792f0e91`）です。この文書は脆弱性診断や安全性の保証ではなく、確認できた実装と未実装の対策を分けた設計記録です。

「実装済み」は該当コードまたはCI設定を確認した範囲を示します。「部分対応」は一部の条件だけを検査し、脅威全体を防ぐ保証がない状態です。「未実装」は要求または関連Issueにはありますが、現行mainで仕組みを確認できていない状態です。攻撃の発生確率を測定したデータはないため、確率を数値化しません。影響度は、HighをOS入力・秘密情報・配布物の信頼性に及ぶ結果、MediumをローカルデータやSessionの持続的な破損、Lowを選択中のゲーム内に限られる誤操作として扱います。各脅威では防止（Prevent）、検出（Detect）、封じ込め（Contain）、復旧（Recover）を分けて記録します。

## 用語

- Observation: 選択したゲーム画面を取得し、画像・OCR・状態情報をまとめた観測データです。
- ActionCandidate: Observationから作る実行候補です。候補ID、操作種別、表示ラベル、認識信頼度、必要な座標を持ちます。候補が実行可能かは別の評価処理が判定します。
- DecisionContext: Providerへ渡す判断材料です。Observationの要約、候補と評価根拠、許可候補ID、目標、直近の操作結果や履歴を含みます。
- Provider: RuleProviderまたはOllamaProviderのように、DecisionContextからActionCandidateを選びActionDecisionを返す判断器です。OS入力を実行する部品ではありません。
- snapshot ID: 1回の判断が対象にしたObservationを識別するIDです。古い画面への回答を識別するために使います。
- Session: 利用者が選択したゲームWindowに対して、観測・判断・操作を繰り返す実行単位です。
- Replay: 過去のObservation / Action / Outcomeを再現・評価するための記録です。
- Artifact: Model、画像、Replay等を参照する保存物です。Artifactのhash（内容から計算したdigest）が一致しても、作成者や内容が安全だとは限りません。
- endpoint: Provider通信の接続先URLです。現在のOllama endpoint欄は既定のloopback以外も設定できます。
- CI: コード変更後にbuild / testを自動実行する継続的インテグレーションです。
- VLM: 画像と文章の両方を処理する視覚言語モデルです。
- ActionDecision: Providerが返す候補IDと理由です。返答の形式が正しくても、選択の意味的な安全性は保証されません。
- Native Runtime: Windows / C++境界を担当する共有ライブラリです。Python側のSafety検査に加えて、一部の入力invariantを再検査します。
- DoS: 処理を止めたり資源を使い切ったりして、アプリを利用できなくする攻撃です。
- manifest: Pluginなどの拡張が要求する能力を宣言するデータです。
- IPC: 異なるprocess間でデータやcommandを渡すプロセス間通信です。
- TLS: ネットワーク通信の盗聴・改ざんを防ぐ暗号化方式です。
- SAST: ソースコードを実行せずに調べる静的アプリケーションセキュリティ検査です。

## 信頼境界

次の図は、現行の判断・入力経路と、未実装の拡張経路を示します。画面内の文字列、Providerの返答、外部ファイルは信頼できる命令として扱いません。

```text
利用者のGUI操作 ───────────────> Run Control / Emergency Stop
                                       │
選択したHWND ─> Capture / OCR ─> Observation・ActionCandidate
                                       │
                                       v
                                DecisionContext
                                       │
                                       v
                      RuleProvider / OllamaProvider
                         │                    │
                         │                    └─> Ollama endpoint
                         │                         既定値は127.0.0.1、画面で変更可能
                         v
                  未検証のActionDecision
                         │
                         v
 DecisionVerifier ─> ActionSafetyEvaluator ─> SafetyGuard / FailSafeRuntime
                                                     │
                                                     v
                                         WindowsInputExecutor
                                                     │
                                                     v
                                      選択したHWND / PID

 ローカル設定・履歴・Artifact ─> 個別のJSON / Artifact読込境界
 外部Model・Plugin・Replay ────> 現行mainに共通の受入・隔離経路はない
 利用者Chat ──────────────────> 将来の独立命令経路（#436 / #437）
```

`HWND`はWindowsがウィンドウを識別する値、`PID`は対象プロセスの識別番号です。`DecisionVerifier`はProviderの返答を実行可能な証明とはみなしません。返答した候補IDが評価済み候補に含まれることや、画面参照が判断時点と一致することを検査します。`SafetyGuard`と`FailSafeRuntime`は、その検査とは別に入力対象・停止状態・入力頻度などの実行条件を検査します。

## 保護対象と信頼規則

| 保護対象 | 守る性質 | 主な入口 |
|---|---|---|
| OS入力権限と選択中ウィンドウ | 意図しないクリック・キー入力を防ぐ | ActionCandidate、対象HWND / PID、Windows入力API |
| 画面・OCR・候補・判断履歴 | 機密性と判断根拠の完全性を保つ | Capture、Observation、DecisionContext、ログ |
| Provider設定とモデル選択 | 接続先と判断結果を利用者が把握できる | 画面のendpoint欄、Ollama HTTP API |
| 設定・履歴・Replay・Artifact | 壊れたデータや偽装データを実行・学習へ混入させない | JSON / JSONL、Artifact Store、将来の外部Artifact取込 |
| 依存関係・CI・配布物 | 改ざんされたコードや依存物をリリースしない | GitHub Actions、Python / Native依存、配布工程 |
| 認証情報・署名材料 | 外部送信・ログ・配布物への漏えいを防ぐ | 将来のRemote Provider、CI secret、署名鍵 |

以下の規則をすべての境界に適用します。

- ゲーム画面、OCR、VLM（画像と文章を処理する視覚言語モデル）の出力はObservationです。画面に「以前の指示を無視する」などの文があっても、利用者命令へ昇格させません。
- Model / Providerの出力は未信頼です。現行ProviderにOS入力権限を渡さず、評価済み候補の選択結果だけをVerifierへ渡します。
- 利用者が明示したゲーム目標とゲーム画面内の文は別の入力です。Trusted User Chatの独立経路はIssue #436 / #437で追跡中であり、現行UIの自然文を一般的な安全命令チャネルとはみなしません。
- SHA-256によるArtifactの整合性確認は、作成者や内容の安全性を証明しません。外部Artifactは、受入・隔離・検査の実装が完了するまで信頼済みデータとして扱いません。
- Safety判定やVerifierが成功しても、ゲーム内の意味的な正しさや無害性は保証されません。実入力を使う利用者は、対象ウィンドウを確認して実入力を明示許可してください。

## 脅威一覧

### TM-01 — ゲーム画面を通じた間接Prompt Injection

- 資産・入口・影響・露出: 利用者の操作権限とゲーム状態を脅かします。画面・OCR由来の情報は`DecisionContext`に入り、Ollama利用時は判断用要求へ含まれます。露出は現行、影響はHighです。
- Prevent: 現行の`DecisionContext`はOCR要約を`state.visible_text`に、利用者のゲーム目標を`goal`に分けて格納します。Ollamaは両方を含むJSONを受け取りますが、画面文字列へ強制可能なtrust labelを付ける機構はありません。`OllamaProvider`は許可候補IDから選ぶよう要求し、`DecisionVerifier`は許可済み候補以外を拒否します。この候補制限は実行範囲を狭めますが、許可候補内の選択に対する画面文字列の影響は防ぎません。
- Detect: `DecisionVerifier`は候補、snapshot / screen / state参照、判断理由と選択候補の明示的な矛盾を検査します。これは注入検出器ではなく、注入が許可候補内の選択を誘導した場合は検出できません。
- Contain: `ActionSafetyEvaluator`が意味的リスクを評価し、`SafetyGuard`が危険キー、対象、座標、反復、停止状態を独立して検査します。Safety Guardはゲーム内の全ての誤操作を防ぐものではありません。
- Recover: 利用者はStop / Emergency Stopを押して追加入力を止めてください。停止処理は保持中の入力を解放し、再開には明示操作を要求します。自然文の利用者命令をゲーム文字列と別経路へ分離する作業は#436 / #437です。
- 残余リスク・確認先: 許可済み候補が有害な選択肢なら、モデルがその候補を選ぶことは残ります。確認先はReasoning / Safety領域です。回帰確認は`tests/test_provider_decision.py`、`tests/test_decision_verifier.py`、`tests/test_action_safety.py`、`tests/test_safety_guard.py`です。

### TM-02 — UI偽装、誤ウィンドウ、古い画面への入力

- 資産・入口・影響・露出: OS入力権限を脅かします。対象HWNDの差替え、対象プロセス再起動、別ウィンドウ、古いObservation、偽のゲーム内ボタンが入口です。誤った対象へ入力できた場合はHigh、選択画面の誤クリックだけならMediumです。境界確認は現行、視覚偽装への包括対策は部分対応です。
- Prevent: `SafetyGuard`は対象HWNDの有効性・可視性・PID、必要時のForeground、click座標がclient area内であることを確認します。標準Windows実入力の`ActionExecutor`は入力直前にもtargetを確認し、選択WindowとProcess identityを要求します。テスト用または明示注入されたcustom executorのtarget検証は、別backendの責務です。
- Detect: `DecisionVerifier`は判断に使ったsnapshot / screen / state参照と現在のObservationを比較します。対象が消失・再起動した場合はtarget invalidまたはPID changedとして拒否します。
- Contain: `FailSafeRuntime`とSafety Guardがtarget不一致や停止状態でActionを拒否します。選択中Windowが消えた場合に別Windowへ自動切替して入力することは許可しません。
- Recover: 利用者は停止状態を維持し、対象Windowを再選択して新しいObservationから再開してください。入力が別アプリへ届いた場合、アプリはOSやゲーム状態を元へ戻せないため、対象アプリの状態を確認してください。
- 残余リスク・確認先: 正しいWindow内に描画された偽ボタンや、視覚認識自体の誤りはHWND検証で防げません。Perception / Windows Runtime / Safetyが確認します。回帰確認は`tests/test_safety_guard.py`、`tests/test_action_executor.py`、`tests/test_windows_capture_integration.py`です。

### TM-03 — 悪意ある、侵害された、または誤動作するModel / Provider

- 資産・入口・影響・露出: ゲーム状態と入力権限を脅かします。Providerの不正JSON、許可外Action、古い回答、または意図的に有害な候補選択が入口です。影響はHigh、Provider出力の検証は現行、モデルの意味的正しさの保証は未実装です。
- Prevent: 現行Providerは`RuleProvider`と`OllamaProvider`です。Providerは候補と判断材料を返すだけで、Windows入力APIを直接呼びません。Ollamaの回答は未検証値として扱い、実行境界とは分離します。
- Detect: Provider JSONの形式検査と`DecisionVerifier`が、許可候補・現在画面・重複候補・参照値を照合します。`REJECT`結果は実行へ進めず、未確認の信頼性状態は追加検証へ回します。
- Contain: `ActionSafetyEvaluator`と`SafetyGuard`がProvider検査から独立して危険操作・対象・入力頻度を制御します。無効な形式や候補はfail-closedで拒否します。
- Recover: 判断拒否やProvider障害時は、新しい画面を観測し、Providerを確認・変更してから再開してください。モデルのプロセス自体を強制終了する保証はありません。
- 残余リスク・確認先: 正しい形式で返された許可候補が悪意ある選択であっても、Verifierは意味的安全性を証明しません。Reasoning / Safety領域が確認します。関連テストは`tests/test_provider_decision.py`と`tests/test_decision_verifier.py`です。

### TM-04 — Remote Providerへの画面情報・判断情報の漏えい

- 資産・入口・影響・露出: 画面由来状態、ActionCandidate、目標、判断履歴を脅かします。アプリのendpoint欄で接続先を変更すると、`OllamaProvider`はDecisionContextをそのendpointへ送信します。既定値は`http://127.0.0.1:11434`ですが、endpointは編集可能で、HTTPまたは外部hostへの送信をコードが拒否しません。露出は設定により現行、影響はHighです。
- Prevent: endpointをRemote hostへ変更する前に、送信先の管理者とデータ取扱いを確認し、画面由来情報の送信を利用者が許可していることを確認してください。Remote送信への明示同意、送信データ分類、TLS（通信暗号化）/ endpoint policyは現行コードで確認できず、#226およびProvider APIの#174 / #238が未完了です。
- Detect: endpointは設定値として保持されますが、現在のコードは外部hostかどうかを判定するconsent gateを持ちません。送信先の監査・遮断をSecurity controlとして保証できません。
- Contain: 外部へ送信済みの情報を取り戻す方法はありません。利用者は以後のSessionを停止し、endpointを確認してloopbackまたは信頼する接続先へ戻してください。
- Recover: 送信済みデータの削除・remote credentialの失効・侵害通知の手順は#120のSecurity報告方針で整備する対象です。現行Providerには認証token管理を確認できません。
- 残余リスク・確認先: 現行の`GamePlayerEngine`から`OllamaProvider.choose_context()`へ進む経路はraw frame bytesを送らず、screen ID・寸法・signature・perceptual hash・brightness・検出要素数を含むstate要約、ActionCandidateと評価根拠、目標、直近の操作結果、最近の履歴を送ります。`DecisionContextBuilder`はOCR文字列を最大8件、合計160文字までstate要約に含めます。一方、旧`OllamaProvider.choose()`を直接呼ぶ経路は`ScreenObservation.to_dict()`を使い、OCR全文とfeatures辞書を送ります。Reasoning / Privacy領域が両経路の送信項目とconsentを検証します。関連CI・privacy testは未確認です。

### TM-05 — Malicious MOD / Plugin / Game Adapter

- 資産・入口・影響・露出: OS入力権限、ローカルファイル、Provider dataを脅かします。現行mainに`plugins/`ディレクトリやPlugin実行経路はありません。露出は未実装機能の将来境界、影響はHighです。
- Prevent: 現行mainにPluginを読み込む安全機構はありません。Plugin機能を追加するときは、許可する能力をmanifest（拡張機能が使える機能の宣言）に列挙し、Safety Required処理を迂回できない契約を先に実装します。Isolation RuntimeやCapability enforcementは#150–#153 / #235で追跡中です。
- Detect: Plugin署名、由来、権限逸脱、IPC入力の監査は現行Plugin経路がないため未実装です。
- Contain: Plugin process / IPC（プロセス間通信）分離と、実行権限をSafety境界の外へ出さない設計が必要です。現行の`SafetyGuard`は将来Pluginの一般的な隔離機構ではありません。
- Recover: 導入済みPluginを無効化し、信頼済み版へ戻す管理経路は未実装です。Plugin導入前に更新・削除・状態復元を設計します。
- 残余リスク・確認先: Plugin未実装のため、PluginからOSやファイルへのアクセスを制限できると主張しません。Runtime / Safety / MOD領域が確認します。現行Plugin testはありません。

### TM-06 — 不正Observation、Decision、schemaによる境界突破

- 資産・入口・影響・露出: Action候補・Session状態・判断履歴を脅かします。壊れたJSON、欠落・重複ID、異なる画面の参照値が入口です。露出は現行、影響はMedium〜Highです。
- Prevent: `OllamaProvider`は応答JSONを解析し、型を文字列へ安易に変換しません。`DecisionVerifier`は候補IDの一意性と許可状態、snapshot / screen / state参照を照合します。
- Detect: パース不備は`validation_error`として記録し、Verifierは候補重複・候補と許可一覧の不一致・古い画面参照を検出します。
- Contain: pipelineはREJECTや必要なverificationをOS実行前に処理します。Native Safety Validatorが利用可能な場合も、Action境界の簡単なinvariantを再検査します。
- Recover: 検証失敗を通常の成功として再利用せず、利用者は観測を取り直してから再開してください。任意のschema変更ではversioned contractと回帰testを追加します。
- 残余リスク・確認先: Provider応答や全ての外部入力について、最大byte数・全体のparse budgetが保証されるかはこのIssueでは確認していません。Reasoning / Schema / Safety領域が確認します。関連テストは`tests/test_models_validation.py`、`tests/test_provider_decision.py`、`tests/test_decision_verifier.py`です。

### TM-07 — Replay・履歴・Artifactへの偽装データ混入

- 資産・入口・影響・露出: 学習・判断履歴とArtifactの完全性を脅かします。JSON / JSONL、Replay、外部Model / fixtureの導入が入口です。Artifact Storeは現行、外部Artifactの一般受入は未実装、影響はMedium〜Highです。
- Prevent: `FileSystemArtifactStore`はSHA-256 digestから保存先を作り、保存metadata・payloadのsizeとhashを照合します。digest一致は作者の真正性や学習データの意味的正当性を証明しません。
- Detect: digest、size、metadataに不整合があるArtifactは読込・integrity checkで拒否されます。一般Replayの出所・署名・label品質を全て検証する仕組みは確認できません。
- Contain: 外部Artifact取込経路は現行にないため、受入時の隔離と実行禁止を保証する機構もありません。#119 / #271 / #272の検査を経るまで外部Artifactをtrusted model・training dataとして使いません。
- Recover: 信頼済みsourceから再取得・再生成できる手順はArtifact種別ごとに定義します。誤ラベルや汚染済み学習入力の自動rollbackは現行機能として確認していません。
- 残余リスク・確認先: hash整合性を署名・出所確認と混同しないでください。Evaluation / Learning / Security領域が確認します。関連テストは`tests/test_artifact_store.py`です。

### TM-08 — ローカルpath、symlink、archive展開の悪用

- 資産・入口・影響・露出: 設定・履歴・モデルファイルとユーザーデータを脅かします。任意pathを受け取るimportや圧縮archive展開は現行の共通Artifact intake経路にありません。`FileSystemArtifactStore`はSHA-256形式をpath componentに使い、object directory / payload symlinkを拒否します。親shard pathを含む全OSのsymlink・race条件検査は確認していません。影響はHigh、一般archive入口の露出は未実装です。
- Prevent: 任意の外部archiveを展開しないでください。#271 / #272実装ではroot配下への正規化、symlink拒否、ファイル数・展開後size・圧縮率の上限、atomic publishを必須条件にします。
- Detect: 現行Artifact Storeが検査するobject / payload symlinkとdigestは限定された検査です。任意path・archive traversal検査の回帰testは確認できません。
- Contain: 外部Artifact intakeは未実装のため、隔離dirや実行権限制御はありません。追加後は検証前データを通常cache、Plugin、Model loaderへ渡さないようにします。
- Recover: path検証失敗時は部分展開先を公開せず削除し、信頼済みsourceから再取得します。未実装のclean-up動作は#271 / #272でtestします。
- 残余リスク・確認先: symlinkを使った任意ユーザー書込み先への逸脱を、この脅威モデル作成時に実証したわけではありません。親directory・race条件の検証は未確認事項として残します。Storage / Distribution / Security領域が確認します。

### TM-09 — Dependency・Build・Release supply-chain侵害

- 資産・入口・影響・露出: 配布コード、Native DLL、依存Model、署名鍵、利用者端末を脅かします。GitHub Actions、package依存、外部artifact、Release工程が入口です。影響はHigh、CIのNative sanitizerは現行、dependency / Secret / SASTのblocking Gateは未確認です。
- Prevent: 現行CIは通常testとNative ABI testを行い、`language-ci.yml`はC++ sanitizer / GCC analyzerを実行します。これらはdependency audit、Secret scan、全言語SASTの代替ではありません。multi-language gateは#112 / #113および子Issueが未完了です。
- Detect: 安全なC++ / Windows回帰は`safety-ci.yml`等で検査します。現行workflowからPython security profile、依存脆弱性監査、Secret scanのblocking実装を確認できません。#267 / #285–#287も確認対象です。
- Contain: PR CI実行とmainのrequired-check enforcementは別です。Issue #92はbranch ruleset未整備を記録していますが、現在のGitHub repository settingsはこの作業では確認していません。Main protectionはUnverifiedです。
- Recover: 脆弱性の非公開報告、severity、advisory、修正後regression testは#120の完了条件です。現行mainに`SECURITY.md`はありません。
- 残余リスク・確認先: CIが成功しても悪意ある依存・workflow変更・署名material侵害がない証明にはなりません。CI / Supply Chain / Release領域が確認します。

### TM-10 — Provider停止・Resource exhaustionによるDoS（サービス妨害）

- 資産・入口・影響・露出: CPU・memory・Session可用性を脅かします。停止しないProvider、異常に大きい応答、反復操作が入口です。影響はMedium、timeoutとAction数制限は現行、response-sizeや推論memory budgetは未確認です。
- Prevent: `OllamaProvider`は通信timeoutを設定します。`SafetyGuardConfig`はAction rate、burst、同一Action、no-progress、session action数、duration、hold、waitに上限を持ちます。
- Detect: timeout、no-progress、rate limit、FailSafe heartbeat / observation freshnessの検査は現行です。最大HTTP response byte、推論RAM / VRAM、全pipeline deadlineの共通budgetはこの作業では確認していません。
- Contain: Safety拒否またはEmergency Stop後は新しい入力を停止し、`ActionExecutor`が保持中の入力解放を試みます。重い学習と推論の資源調整は#425等のruntime課題で追跡中です。
- Recover: 利用者はSessionを停止し、Provider・対象processの状態を確認後、原因を除いて再開してください。自動的な第三者process強制終了は保証しません。
- 残余リスク・確認先: 現行回数上限はリソース全体を測るquotaではありません。Inference / Runtime / Safety領域が確認します。回帰確認は`safety-ci.yml`と`fail-safe-ci.yml`で実行する関連testです。

### TM-11 — Action amplificationと停止不能な実行loop

- 資産・入口・影響・露出: OS入力とゲーム内保存状態を脅かします。同じActionの連続、遅いObservation、Session停止後の遅延結果が入口です。影響はHigh、現行SessionのAction制御と停止経路がこの脅威に露出します。
- Prevent: SafetyGuardはAction rate、burst、繰返し、session時間・件数、key hold上限を設定します。FailSafeRuntimeはlease / heartbeat / observation freshnessが失効したとき、追加Actionを許可しません。
- Detect: SafetyLog、FailSafe state、no-progress監視が拒否理由を記録します。遅れて返った旧世代Decisionを実行へ進めないための回帰testもあります。
- Contain: Stop / F12監視で追加入力を拒否し、held key / mouseを解放します。実入力はdry-run既定から明示的に切り替える必要があります。
- Recover: 停止後の再開には明示re-armを要求します。Target・Provider・画面を再確認してからSessionを開始します。
- 残余リスク・確認先: 回数制限の範囲内でも意図しないゲーム内操作は起こり得ます。Emergency Stopの入力が物理利用者由来かAI由来かを確実に識別する拡張は#435で追跡中です。Safety / Windows Runtime領域が確認します。

## 所管領域と更新条件

個人のArea Maintainer / reviewerは未指名です。変更責任は、脅威の入口に応じてSafety / Fail-safe、Perception / OCR、Reasoning / Provider、Evaluation / Learning、Native / Windows Runtime、CI / Supply Chain / Distributionへ割り当てます。複数境界へ影響する変更は関連AreaをPRのレビュー先へ含めてください。

次の変更・発見があった場合、この脅威モデルと関連test / CIを同じIssueまたは追跡Issueで更新します。

- Provider endpoint、送信するObservation項目、認証方式、Remote consent、Transport securityが変わる。
- User Command、画面/OCRの出所・型、DecisionContext、候補選択、Verifierの受入条件が変わる。
- Window target、Capture source、座標、実入力、Emergency Stop、FailSafeの境界が変わる。
- Model / Plugin / MOD / Replay / archive / external artifactの読込・実行・学習経路を追加する。
- Runtime dependency、build workflow、CI permission、署名・配布手順を変更する。
- Security finding、Privacy incident、Safety bypass、または再現可能な攻撃が見つかる。

関連Issue: [#33 SafetyGuard](https://github.com/tomiya7688/AI_game_player/issues/33)、[#40 Fail-safe Runtime](https://github.com/tomiya7688/AI_game_player/issues/40)、[#43 Decision Verifier](https://github.com/tomiya7688/AI_game_player/issues/43)、[#112 Multi-language Security Gate](https://github.com/tomiya7688/AI_game_player/issues/112)、[#119 External Artifact Intake](https://github.com/tomiya7688/AI_game_player/issues/119)、[#120 Vulnerability Reporting](https://github.com/tomiya7688/AI_game_player/issues/120)、[#121 Trust / Privacy / Release Governance](https://github.com/tomiya7688/AI_game_player/issues/121)、[#226 Remote Consent](https://github.com/tomiya7688/AI_game_player/issues/226)。各Issueのopen / closed状態と完了条件はGitHubを正本として確認してください。
