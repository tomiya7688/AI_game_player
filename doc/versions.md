## 2026-11-01 ウィンドウ一覧更新ボタンの補足説明（Issue #475 / PR #615）

- Play画面の「一覧更新」へ共通ToolTipを接続し、ゲームのウィンドウを起動・再起動した後に一覧を取得し直す操作を案内します。マウスのホバーとキーボードのフォーカスで表示します。
- mainのPlayer Seat・配色・実入力の許可欄を保持し、一覧更新ボタンの既存のクリック処理を共通ToolTipの追加後も利用します。説明の表示自体はゲームへの入力を開始しません。
- 共通ToolTipはマウス進入とキーボードフォーカスを別々に保持し、両方が離れた時に閉じます。クリック・Esc・対象の非表示・破棄では従来どおり直ちに閉じます。


## 2026-10-31 emSignインシデント対象の特定とpipの信頼源（Issue #513）

- 公開アドバイザリGHSA-6q48-6c7w-9gw2と、certifiの公開版（2026.7.22・10.18・10.19・10.23）の比較から、インシデントの対象ルートは`emSign ECC Root CA - G3`だけと特定しました。前回の棚卸しで残っていたemSign Root CA - G1・C1・ECC C3は対象外です。
- `tools/trust_inventory.py`は、eMudhra/emSignの証明書ごとにインシデントとの照合結果を表示します（対象ルートのフィンガープリント一致、対象ルート名を発行者または主体に持つ証明書、既知の対象に該当なし。最後は安全の判定ではない）。bundleとWindowsストアごとに該当件数も表示します。合否判定ではなく、終了コードは変えていません。
- pipの設定ファイル（場所・有無・証明書関連の設定。`global`節とコマンド別の節を区別し、環境変数を優先）を読み取り専用で記録し、pipのversion・同梱truststore・`legacy-certs`の指定から、pipが使う信頼源を表示します。ローカルのpip 26.2.2は、Windowsの信頼ストアとpip同梱certifiを使います。pip同梱certifiに対象ルートは無く、Windowsストアでは確認時点で未検出です。設定を読めない場合と`cert`指定時は`unknown`と表示します。
- `tests.yml`の棚卸し結果をjobのログにも出力し（ランダムなトークンの`::stop-commands::`でworkflow commandの解釈を止めて出力し、レポートが無い場合は警告）、CIの実際の値を文書へ記録できるようにしました。PyInstaller配布物の確認は#515に残ります。

## 2026-10-29 Provider・参照ゲーム宣言のmain追従（Issue #475 / Draft PR #177）

- 最新mainの安全CI、コンテキスト削減、ソース案内を保持し、旧Draftの競合を解消します。完了済み#178のNoisy ProviderはPR #458の現行実装を使用し、旧Draftの別実装を重複として除外します。
- Provider・設定スキーマと固定ソース宣言のcheckを追加し、リポジトリ内の必須check定義を29件へ合わせます。GitHubの実rulesetは自動変更しません。実推論・全ゲームの実プレイ・1.0配布受入れは未完了です。
- 参照ゲーム取得では固定SHAと保存先を検証し、既存ディレクトリの取得元や未保存・無視対象ファイルを確認してから再取得します。GPU計測では不正な間隔を子の起動前に拒否し、中断時に起動した子だけを回収します。

## 2026-10-31 賢者ミスクのPlayer Seatとアプリ全体のテーマ（Issue #156 / #155の一部）

- Navigatorの下部にPlayer Seatを置き、Development既定のoccupantとして賢者ミスクを表示します。64pxの原画を整数倍のnearest-neighborで128pxへ拡大し、ぼかしを入れません。表示名、状態、短い吹き出し、License名を表示し、停止・要確認以外の状態ではゆっくり上下に浮遊します。
- 画面取得（連続実行を含む）は「観察中」、判断とdry-runの実行は「考え中」、実入力の実行だけを「操作中」、結果表示とSession完了は「評価中」、停止は「停止」（実行中workerの完了待ちでも即時）、失敗・連続実行の画面取得失敗・状態評価の失敗は「要確認」としてPlayer Seatへ反映します。停止・要確認では浮遊を止めます。エラーは従来どおりステータス表示とエラーダイアログでも確認でき、Player Seatだけに依存しません。
- 描画方法から独立したPlayer状態（8種）とPlayer Asset Pack manifest（表示名、画像、SHA-256、License名・参照先、出典、profile、状態別メッセージ）を追加しました。manifestはschema_version 1、Pack外を指さない相対画像パス、記録されたSHA-256と画像の一致を要求します。画像が欠けた場合（SHA-256の記録なし）は表示名の頭文字、manifestを読めない場合は汎用Playerへfallbackし、Player Seatの描画失敗をCore処理へ伝えません。
- 賢者ミスクの原画（Tomiya Character License v1.0.1）を`src/ai_game_player/assets/players/wise_misk/`へ同梱し、Windows bundleにも含めます。1.0.0 Release用のoccupant差し替えは未実装で、#155に残ります。
- アプリ全体を賢者ミスクの原画と同じ黒・白・灰の配色へ揃えました。ヘッダーにミスクのアイコン、選択中のWorkspaceとボタンは白地に黒文字、停止ボタンだけを白文字とのコントラスト比4.5:1以上の赤（無効時は灰色）、要確認を黄で表示し、狭いウィンドウでも停止ボタンを優先して配置します。ToolTipも白い吹き出しに揃えました。操作、停止処理、実入力の既定値は変更していません。

## 2026-10-29 具象クラス起点の多段継承の規約（Issue #475 / PR #604）

- 新規・変更するプロジェクト所有コードでは、連続する3クラスが`具象A → B → C`となる継承を禁止します。暗黙の最上位基底型とインターフェースは対象外です。より厳しい既存規則を優先し、対象機能のGitHub IssueまたはそのIssueが参照する設計書に、理由と範囲を記録した例外を認めます。
- 継承より委譲・合成を優先する設計原則と、実装規約を言語仕様・テスト用入力へ誤適用しない条件を追記しました。実装、継承解析、既存コードやCIの合否判定は変更していません。

## 2026-10-29 実入力の許可欄の補足説明（Issue #475 / PR #601、PR #507を置換）

- 「実入力を許可」のホバーまたはキーボードフォーカスで、許可後に操作を実行した場合の入力、判断だけの場合の入力なし、連続実行中・処理完了待ちの設定変更制限を説明します。
- PR #511で統合した共通ToolTipを使います。PR #507の重複した旧ToolTip・停止案内は最新mainを保持し、実入力の既定値・チェックボックスの処理・停止処理は変更しません。


## 2026-10-29 サンプルE2Eの完了公開と候補取得の競合（Issue #597）

- 実入力後の完了確認では未完了でも、次の判断時に許可候補が空になった場合は完了条件を再確認します。先行する実入力と操作記録があり、停止要求や実行世代の変更がなく、完了確認が厳密なTrueを返した場合だけ、追加入力せず到達成功を記録します。
- 完了の再確認は判断呼出し中の規則判断の許可候補なし例外に限定します。観測・完了確認コールバックの例外は再確認しません。不正な候補、安全拒否、キャプチャ失敗、停止、完了確認の例外を成功へ変換しません。再確認の読取失敗は元の候補なし例外とともに記録します。
- サンプル状態の読取ではcompletedのbool値を要求します。ファイル欠落、読取失敗、不正JSON、欠落・非boolのcompletedは例外を伝え、未完了として隠しません。
- 再確認による成功は独立したmilestoneイベントに記録し、実行していない操作や安全評価を追加しません。実入力の確認、600秒の継続条件、CIの合否基準は維持します。

## 2026-10-29 停止ボタンの条件付き案内（Issue #475 / PR #595）

- 停止ボタンのToolTipへ、Escは本アプリにフォーカスがある場合、F12はWindowsの場合に停止を要求することを記載しました。
- Windowsの連続実行中は、自動操作以外のマウス移動を検出すると停止を要求することも説明します。非Windowsの案内にはF12とマウス移動停止を含めません。
- Play画面の固定案内も、共通の停止方法と詳しい条件を確認する操作へ揃え、利用できる追加手段が必ずあるようには案内しません。文章はPlay領域の幅に合わせて折り返します。
- PR #511の共通ToolTipへ案内を接続し、停止処理自体は変更しません。停止完了までの時間を保証する文面は使いません。

## 2026-10-29 ヘッダーの補足説明（Issue #475 / PR #511）

- Inspector・Bottom Panel・停止ボタンへ、400msのホバーまたはキーボードフォーカスで説明を表示します。説明窓はフォーカスを奪わず、退出・クリック・Esc・非表示・対象破棄で閉じます。説明窓の位置はTkが報告する仮想画面の範囲へ調整します。
- 共通ToolTipは対象破棄時に表示タイマーも取り消します。停止ショートカットの説明はOSに合わせ、停止・実入力の処理自体は変更しません。
- PR #511の設定画面ボタンへの案内追加は、この基盤変更から分離してIssue #475で継続します。今回の対象は共通部品とヘッダー3ボタンです。

# Versions

## Versioning policy

Application versionとSchema/API/ABI versionは独立して管理する。

### Application

正式リリース前は先頭を必ず `0` とする。

- `0.0.0`: 開発開始点
- `0.0.1`: 軽微な修正・小変更
- `0.1.0`: 大きめの機能追加・開発マイルストーン
- `1.0.0`: 最初の正式リリース
- `1.0.1`: 正式版のbugfix / 軽微変更
- `1.1.0`: 通常の機能追加
- `2.0.0`: 大規模breaking change

現在の `0.1.0` は既に到達した開発マイルストーンとして維持し、巻き戻さない。

### Schema / API / ABI

対象contract自体が更新された時だけversionを上げる。Application version変更だけでは連動して上げない。

```text
app_version                     0.1.0
native_runtime_abi_version      1
inference_provider_api_version  1
decision_context_schema         1
experience_schema               1
stage_envelope_schema           1
plugin_manifest_schema          1
```

各contractのbreaking/compatible変更規則は、そのmachine-readable schemaまたはarchitecture documentを正本とする。

## Unreleased

- Issue #661: 恒久GUIをQt 6 Widgets/C++ native（埋込WebViewなし）、常駐CoreをRust別プロセス、Pythonを任意workerとする目標配置を正本化しました。現Tkinter/Python+C++ C ABI v1とWindows E2Eを保ち、manifestのplanned配置を既存ABIと分離して検査します。未実装のShell/Core/bridge/Safetyは#664–#667、Qt配布gateは#228/#149/#663へ接続しました。[移行境界と未確認事項](architecture/qt_rust_migration.md)。

- Issue #422: 既定Light Modeを実Sessionへ接続し、非blocking有限queue、CPU/時間/保持量/書込/公開間隔の予算、immutable次版、Gate自身のsoft-prior限定検証、Mode世代CAS後のdurable ackと故障recoveryを追加しました。実送信receiptと明示因果証拠だけを既知効果へ変換し、unknown/得点表示/背景変化を成功やno-effectへ捏造しません。profile保証のhash識別と衝突保留、Stop時の有限事実checkpoint、数値Utility EWMA portを追加しました。[設計・測定・未実装の境界](architecture/light_adaptation.md)を参照してください。

- Issue #637: 既存`--save-failure-evidence`の明示ON時、2048 browser起動失敗のcleanup前に所有PIDの最大3窓の固定分類・可視状態・矩形・phase時間と、取得可能なWGC画素を有界保存します。GDIのdesktop矩形代替は使わず、WGC不可時もmetadataを残します。既存PNG/BGRA writerとatomic JSONを再利用し、診断子processを5秒、画素を窓あたり8MiB・合計16MiBに制限します。Native WGCの任意byte上限はPython allocation前に検査し、拒否時もleaseを解放します。保存失敗でも元の起動例外とcleanupを維持し、PID/title・25秒・600秒・Safety条件は変更しません。自然起動失敗の原因は#459に残り、Windows実測はUnverifiedです。

- Issue #628: `ExecutionReceipt` と `ExecutionObservation` を導入し、判断保存と実操作後結果を分離します。snapshot・action・実行世代・入力前画面を照合し、実送信だけでpending操作を更新します。明示観測完了を次の実判断前とSession/E2E/campaignの実行後へ接続し、終端や次判断失敗でも最後の結果を保存します。dry-run・入力前失敗は過去の実操作を上書きせず、送信開始後の例外はunknownで監査します。停止/rearmをまたぐ結果・履歴を新世代へ混ぜません。Windows backendのdispatch信号を前処理後の最初の入力API直前へ移し、legacy traceの遅延公開が途中の実行auditを消す競合も修正します。Windows OS上の実入力受入れは別途確認が必要です。 campaign初回観測は実操作前の初期化として結果完了から除外し、後続観測のStop/rearm世代検査を保ったまま開始できるようにします。初回リース開始は明示再開と分離し、世代0とdefault/local緊急停止・時間/操作回数制限を保ち、初期化I/O中のStop/rearmも拒否します。

- Issue #608: 動作順と実行場所の日本語図を追加しました。単独操作の登録済み観測と連続取得、前回判断と実入力、確認運転と実入力時の追加確認を区別し、停止競合と記録の未保証範囲を明示します。
- Issue #475 / PR #136（Issue #56の部分実装）: 言語・責務配置manifestとPython/C ABI宣言の整合性検査を追加します。不正な型、重複した能力配置、細かすぎる境界、読取失敗を検出します。能力ビットの数値をPython bindingへ照合し、現在の言語一覧も検査します。試行IDと既存の原子的保存・ファイルロックでレポートの競合と旧成功の残存を防ぎます。NativeのABI照会・安全検証、JSON変換、画像コピー・ビュー作成、応答期限付きIPCの時間をUbuntu/Windows CIの成果物へ記録します。既存のNative sanitizers・CTest反復・配布物起動・E2Eは保持します。Native bufferの所有権（#233）、独立したNative停止機構、clean Windows配布受入れ（#149）は完了扱いにしません。

- Issue #475 / PR #626: Play画面の「再開」ボタンへ共通ToolTipを接続し、停止状態の解除と実行世代の更新（re-arm）を案内します。マウスのホバーとキーボードのフォーカスで表示し、処理の終了待ち中は再開できないこと、ゲーム操作は自動では始まらないことを説明します。既存の停止処理・旧世代結果の破棄・再開処理は変更しません。

- Issue #357 / PR #366: ルートREADMEにPython、ネイティブ実装、GUI、テスト、開発用ツールへのリンクを追加しました。PythonパッケージのREADMEは責務別の実装入口、一回の操作を読む順序、現在の配置とIssue #356の移行計画の区別を説明します。`tests/test_source_navigation.py`は二つのREADMEの相対リンク先の存在を確認し、外部URLとページ内見出しは検査しません。ファイルの移動や入力処理の変更は行いません。

- Issue #607: READMEから入る日本語の全体案内、完成形と現状の比較、実装状況、用語集、執筆様式を追加しました。保存済みの実入力許可が次回起動時に復元されることを案内し、モデル設定・実呼出し・実機検証を区別します。

- Issue #475 / PR #593: InspectorとBottom Panelの切替ボタンに、次に行う操作を「を表示」「を非表示」で表示します。保存済みの非表示状態から起動した場合もラベルを合わせ、クリック後はPanel配置・保存状態・ラベルを同期します。停止操作とPanel状態の保存形式は変更しません。

- Issue #475 / PR #488: 既にmainにあるSafetyLogの直近4096件保持を維持し、メモリ上限超過後もファイルへ全監査イベントが残る回帰テストを追加します。メモリのみの保持順、返却一覧の分離、許可・拒否イベントと日本語のJSONL保存を検査します。保持上限・公開API・安全判定は変更しません。

- Issue #513: Python環境と証明書信頼源の棚卸しツール`tools/trust_inventory.py`と、2026-10-28時点の確認結果`doc/Python証明書信頼棚卸し.md`を追加する。指定したPythonで確認スクリプトを実行し（`PYTHONDONTWRITEBYTECODE=1`、空の一時ディレクトリから起動）、pip・certifi・uv・sigstore・truststoreのversion、pip・certifiのimport元、`certifi.where()`の実ファイル、pip同梱certifi、証明書関連の環境変数、OpenSSLの既定パス、WindowsのROOT・CA・Disallowedストアと信頼設定を記録する。外部コマンドは実行しない。CA bundleとWindowsストアのうちeMudhra/emSignを含む証明書を、SHA-256とcommonName付きで列挙する。bundleは絶対パスの通常ファイル20MB以下だけを1回読み、読めない・空・不正なものは「未検査」として0件と区別する。リポジトリ内のpip・certifi等の利用・固定箇所と子プロセス出力・環境変数の値を、認証情報を伏せて出力する。終了コードは確認不能1・書込失敗2・検出の有無に関わらず確認できれば0。`tests.yml`のquality jobでCIのPython環境を確認しstep summaryへ出力する。パッケージの更新・設定変更はしない。インシデント対象の証明書の特定、PyInstaller配布物・CI実値・pip設定の確認は未実施。
- Issue #459（起動失敗の限定診断）: 2048の専用ブラウザー起動がタイムアウトした場合、ゲームページGETに対してローカルサーバーが生成したHTTP 200応答とそれ以外の応答の件数をエラー文へ追加します。件数はサーバー起動ごとに分離し、再起動前の遅延応答は新しい起動の件数へ加算しません。同じサーバーの起動・終了を排他制御し、同時起動による管理漏れを防ぎます。生のURL・query・タイトル・本文は保存しません。応答生成はブラウザーでの受信や描画完了の証拠ではありません。起動PID・2048タイトル照合、25秒の期限、停止条件は変更せず、既存の起動失敗の根本原因解消とは扱いません。

- Issue #418: 学習Modelを読み込まないmodel-less CI profileとして`tests/test_model_less_core_path.py`を追加する。`JsonObservationSource`が読むObservation fixture（`tests/fixtures/model_less/`）から、OCRテキストによる候補生成、`BaselineDecisionProvider`の判断、`DecisionVerifier`の検証、dry-run実行までを通す。テスト中は`socket`の`connect`・`connect_ex`・`sendto`・`create_connection`・`getaddrinfo`と`urlopen`を遮断し、通信の試行を記録して0件であることを確認する（他のHTTPクライアントや非同期I/Oは対象外）。同点の候補では`abstain`、認識確信度が低い候補では`observe_more`で保留し、どちらも実行履歴が増えないことを確認する。既存のPython unit test CIで実行され、Model artifactは不要。GPU状態は検査しない。#161 Realtime Routerは未実装（P5）のため、Router経由のBaselineへのfallbackは未対応。
- Issue #92: `main`ブランチ保護の正本として`.github/rulesets/main-protection.json`（Repository Ruleset `Mainguard`）と方針文書`doc/main保護方針.md`を追加する。必須7 workflowの全27 jobをGitHub Actions（integration_id 15368）からの必須status checkとし、PR必須・未解決threadの禁止・削除とforce pushの禁止・Copilot自動レビューを設定する。承認数0、最新化の要求なし、bypassなしとし、その理由と緊急時の手順、merge queueを導入しない評価を記載する。`tools/ruleset_check.py`は必須workflowのjob名（matrixのインラインリストと`include:`を展開）とrulesetの必須checkを比べ、差があれば`tests.yml`と`finish_task.bat`で失敗する。名前を予測できない形（`name:`のないmatrix job、`uses:`、job単位の`if:`、matrix以外の式、引用符のない数値・真偽値、`exclude:`など）と、workflow間で重複するjob名はエラーにする。rulesetファイルが有効・既定ブランチ対象・bypassなし・全checkのintegration_idが15368・削除/force push/PR規則ありであることも検査する。必須workflowの一覧は`tools/pr_workflow_evidence.py`の`REQUIRED_WORKFLOW_PATHS`を共有する。マージゲート`tools/finish_pr_merge.py`は、workflow欄が空文字のGitHub App check `CodeRabbit`・`Sourcery review`が`skipping`の場合に限り記録して停止しない（無料枠切れでPR #512のマージが止まったため）。失敗・保留・取消、workflow欄の欠落、同名のActions job、任意checkしか報告されない場合は従来どおり停止する。GitHub上のrulesetの有効化はリポジトリ所有者の許可後に行う。

- Issue #614: 既存の資料リンク検査をRuntime境界・人向けガイドへ広げ、Markdownのタイトル付き・参照形式リンク、コード例の除外、バッククォート/チルダのフェンス、Mermaidの図形式・コメント・引用ラベル・括弧型ノードを扱う軽量検査を追加しました。生成物の鮮度検査と同じ既存CIへ接続し、偽陽性と見逃しの回帰テスト、資料更新・目視確認手順、PRの資料確認欄を追加しました。厳密な描画構文・呼出し順の意味・実機動作は保証しません。
- Issue #318 / PR #346追加レビュー: クラスの状態キーを「フィールド」へ統一し、Issue #19の指定手法・本番接続・検証・延期成果物の対照表をレビュー基準へ反映します。Core / Runtime / Real-playの証拠と未確認・明示対象外を区別し、安全検査とプライバシー同意の迂回を差し戻します。自動ゲート実装はIssue #508で追跡します。

- Issue #318: JSON-like Comment Outsを本体コードの宣言コメント規約として明確化し、レビュー基準・コーディング規約を追加。命名、責務分離、処理単位コメント、エラー・安全性・性能・CI/E2E、仕様書と日本語説明の差し戻し条件を定義。コメントは記法だけでなく対象・条件・理由・結果まで説明し、説明不足のレビュー指摘には欠けた意味と修正案を示す基準を追加。C/C++のstructと公開フィールドを対象に含め、C++公開契約のヘッダーをコメントの正本とする配置規則を定義。Python/C++例の宣言名・戻り値・例外を示した契約を一致させ、参照するDraft v0.9仕様とテンプレートのリンクを同一の不変コミットに固定。

- Issue #487: 初回観測猶予の単体テストで制御可能な時計を共有し、実際のsleepなしで猶予内・期限ちょうど・期限切れと実観測前の入力拒否を検証します。本番の安全期限は変更しません。固定版Ruffによる対象指定の整形コマンドと、このテストのformat/checkをローカル・CIへ追加します。

- Issue #490: Session Journalの旧reader互換履歴を返す際、全記録の`deepcopy`後に再び辞書・配列を複製していた処理を一度の走査へ変更します。Decimalからfloatへの変換、返却値と内部cacheの分離、SQLiteのcommit契約は維持します。JSON以外の可変値は引き続き`deepcopy`で分離します。Providerの候補検証と性能予算は変更しません。

- Issue #459（browser起動・後処理の部分対応）: 専用2048 browser profileの一時的な削除アクセス拒否を0.1秒間隔・最大10回で待ち、恒久的失敗と別のI/Oエラーは隠さず報告します。起動失敗が後処理例外に置き換わらないよう主例外を保持し、失敗した後処理の所有者も保持します。起動検出失敗にはPID・終了コード・直近のタイトル付きウィンドウ件数を記録し、他アプリのタイトルは記録しません。対象PID・タイトル照合と停止条件は変更しません。CIの起動タイムアウトの根本原因を特定した変更ではありません。

- PR #484（Issue #475の既存PR整理）: Windowsの停止ボタンを「■ 停止 (Esc / F12)」へ変更します。F12の案内は既存のWindowsキー監視に対応し、Windows以外では「Esc」だけを表示します。キーバインド・停止条件・実入力許可は変更しません。OSごとの表示と停止ボタンの呼び出しをテストします。

- PR #481: 共通ヘッダーの停止ボタンに「Esc」を表示します。Escの登録は既存のApplication側に一本化し、Shell生成後も親の停止bindingを変更せず停止処理が1回だけ呼ばれる回帰テストを追加しました。Escの表示はアプリ内フォーカス時のショートカット案内であり、グローバル停止機能の追加ではありません。

- 共同作業ルール（ローカル軽量レビュー）: 実装者が自己レビューを終えた後、PR作成前に、実装者がClaude Code・Codexのどちらでも、最新のClaude Haiku（最低の推論レベル）とChatGPT 6 Luna（最低の推論レベル）の両方に、読み取り専用で差分をレビューさせる手順を`doc/ワークフロー/ワークフロー.md`・`AGENTS.md`・`CLAUDE.md`・PRテンプレートへ追加する。差分は実装者自身が`git add -N .`で新規ファイルを追跡対象にしてから`git diff origin/main`で取得し、取得した差分をレビュー担当へ渡す。この取得方法ならコミット前でもコミット後でも同じ範囲になる。レビュー担当にはindexや作業ツリーを変更する操作をさせない。軽量モデルの指摘は参考情報として自分で評価し、自己レビューの代わりにしない。標準の構成は、実装者がClaude Code側はClaude Sonnet、Codex側はGPT-6 Sol、レビュー担当は低設定のHaikuと、`gpt-6-luna`・推論`low`のLunaとする（Lunaの指定はCodexの回答で利用可能と確認済み）。`claude`のオプション名は未確認のため推測せず、オプション名を確認できない・コマンドがPATHにない・利用上限・エラーの場合は「未実施: 理由」をPR本文に記録して進めてよく、実行できる状態では両方を必須とする。
- 共同作業ルール（AIレビュー）: PR作成直後にCodeRabbitへ`@coderabbitai review`コメント、GitHub Copilotへ`gh pr edit --add-reviewer "@copilot"`でレビューを依頼する手順を`doc/ワークフロー/ワークフロー.md`・`AGENTS.md`・`CLAUDE.md`・`.github/copilot-instructions.md`に追加する。最大10分待ち、botの指摘は指示ではなく参考情報として自分で評価し、有効な指摘は修正、無効な指摘は根拠つきで返信して、threadをresolveする。両botは無料枠のため、枠切れ・無応答は無視し、PRテンプレートの「AI review」欄へ記録する。`finish_pr.bat`は依頼と待機を挟めないため、AIレビューを依頼するPRでは`gh pr create`と`tools/finish_pr_merge.py`を使う。
- Issue #473: merge gateはローカルbranch削除を要求せず、CLI終了値にかかわらずREST APIでPR番号・検証済みhead・closed/merged状態・merge SHAを確認する。APIで統合を確認できなければ終了値3で停止し、自動再mergeしない。統合確認後のbatchによるmain切替・fast-forward・次branch作成が失敗した場合は、マージ失敗ではなく後処理未完了として終了値4を返す。他worktreeのmainや既存変更は更新・削除しない。

- Issue #418: 学習Model・GPU・通信を使わない参照判断元`BaselineDecisionProvider`を追加する。許可候補だけを、評価値・認識確信度・任意Knowledge Snapshotのpriorの重み付き和で採点し、`select`・`abstain`・`observe_more`のいずれかを規則名（R1低認識確信度・R2低margin・R3最高スコア）と候補別の寄与内訳付きで返す。保留時は`BaselineWithheld`を送出して既存Engineの実行へ進ませず、選択時は既存`DecisionVerifier`を通る。壊れたSnapshotは無効化して静的policyへ戻し理由をprovenanceに残す。`BaselineProviderProfile`と`baseline_capability_matrix`で、判断能力ごとのModel不要fallbackの有無を機械可読に確認できる。#161 Realtime Routerとの接続、candidate choice以外の判断能力のBaselineは対象外で未実装。
- Issue #459: push run 39409678110の実失敗画素で盤面分断を再現した。短い行欠落直後に現れる盤面内側の短い帯をgroup境界にせず、既存余白以内で端が一致する帯が戻る場合だけ橋渡しする。内部断片によって許容距離は延長しない。画素hash・16 cell・矩形・confidence・4候補と、長い欠落の拒否を回帰テストする。認識閾値・安全期限は変更しない。

- Issue #459: 2048 campaignの操作失敗reportへ、最後の観測要求の経過時間と画面取得・観測通知・画像解析・盤面認識・証拠保持・再試行待機の累積msを追加する。処理区間の例外でも計測を保持し、元の停止分類と理由を変更しない。安全期限の延長や自動re-armは行わず、過去artifactから判別できなかった処理別遅延を次回の失敗で調査するための診断とする。

- 共同作業ルール: Claude Code・Codex・ChatGPT・GitHub Copilot・人が同じ`main`へ変更を入れるための共通ルールを追加する。`doc/ワークフロー/ワークフロー.md`へ、毎回のpull、Issueの占有、1 Issue=1 worktree=1 branch=1 PR、PR前の再pullと`origin/main`取り込み、自己レビュー、CI全成功と未解決threadなしの場合だけのマージ、マージしてはいけない条件、引き継ぎ手順を記載する。`doc/コーディングルール.md`は、クラス・関数・コマンド指向の規則に加え、本番コードの構造化コメント、文章の書き方、自己レビュー7観点を含む正本とする。`AGENTS.md`、`CLAUDE.md`、`.github/copilot-instructions.md`を同じルールへの入口にし、PRテンプレートへSelf-review欄を追加し、`start_task.bat`は`git fetch origin main`に失敗すると開始しない。
- Issue #465 追加レビュー: check一覧と別に、固定PR headのpush・pull_request Workflow RunをREST APIで照合する。必須7 workflow×2 eventについて作成時刻（同時刻はrun ID）で最新runを選び、現在attemptがcompleted/successの場合だけmergeへ進む。欠落・未成功・別head/event/repo/PR・重複ID・件数変動・不完全page・1000件上限到達・API/JSON異常は停止する。check・review・head固定の既存検証は維持する。
- Issue #465 診断文: CI待機が失敗した場合、merge gateは対象PRのURLと`gh pr checks`の確認commandを表示する。長いwatch出力で失敗jobの行が省略されても、開発者が対象PRのcheck一覧を確認できるようにする。
- Issue #465: `finish_pr.bat`はPR作成後にCI status checkの完了を待ち、GitHub CLIが返す全checkのbucketが`pass`であることを確認する。checkがない場合、pending、failed、cancelled、skipped、未知状態、認証・API・JSON解析エラーの場合はmergeしない。GitHub GraphQL APIからreview threadを全ページ確認し、未解決commentの場所・投稿者・本文を表示して停止する。checkとreview確認中にhead commitが変わった場合も停止し、確認済みhead SHAを`gh pr merge --match-head-commit`へ渡す。ゲート停止後は既存PRを再利用して確認を再開できる。11件の単体テストで成功、CI異常、空check、comment、認証失敗、head更新を検証する。
- Issue #459 / PR #469追加レビュー: 保存証拠の全階層で重複JSONキーと非有限float（NaN・Infinity・1e999などの変換overflow）を拒否する。boardはnullまたは4×4、cellはnull・整数0・2以上の2冪整数、confidenceはbool/nullを除く0〜1の数値として比較前に検証する。比較元が不正なframeが一つでもあれば全frameの画素読込・認識を開始せず、CLI終了値2で理由を表示する。記録値の欠落は比較不能null、正当な認識結果の差は終了値1のままとする。
- Issue #459: CIの実失敗画像で盤面未検出を再現し、盤面色の帯が2行欠落しただけで正方形候補を上下に分断していた処理を修正する。縦方向の短い欠落を固定版CSSの盤面余白3%以内で橋渡しし、従来の2 pixelを超える欠落では横端の一致も要求して近接score欄の混入を防ぐ。実失敗BGRAのhash・期待盤面を回帰fixtureへ残し、大きい欠落の拒否・score欄の分離・通常画面・終了画面を検査する。confidence閾値と安全期限は変更しない。WGC timeoutそのものや他の認識失敗をすべて解消したとは扱わない。
- Issues #459 / #465: language-ciの任意言語jobが対象なしでskippedとなり、全check成功を要求する統合判定を満たせない状態を解消する。Lua・.NET・JavaScript/TypeScriptの各jobは検出値がtrue/falseであることを検証し、falseの場合は対象なしをログへ記録して成功する。trueの場合だけ既存のsetup・build・testを実行し、未定義や不正な検出値は失敗させる。PR #467で用意したworkflow変更を診断機能の統合に先行して適用する。
- Issue #459: 保存済み2048失敗bundleを再解析する`python -m tools.replay_2048_recognition`を追加する。manifestとBGRAの寸法・SHA-256を検証し、OCRなしの現行Adapterへ同じ画素を渡して盤面・confidenceを記録値と比較する。bundle外の参照、破損、標準外JSON数値、過大入力を拒否し、元の資料は変更しない。比較元なしをnullとして、再解析成功とゲームの正常動作を区別する。
- Issue #459: 固定版2048をheadlessブラウザで描画した、文字と再試行ボタンを含む終了画面の再生fixtureを追加する。0.75倍・1倍のBGRAとSHA-256、生成元commit、ブラウザ版を保存し、通常のunit testでOCRなしの盤面復元・終了判定・操作候補なしを確認する。生成ツールは任意依存Pillow・playwrightを使い、通常テストは追加依存なしで保存済み画素を検査する。HWND取得や過去のCI失敗の再現を証明するテストではない。
- Issue #459: 固定版2048のCLIに既定OFFの`--save-failure-evidence`を追加する。認識失敗時だけ最後に受理した画面と最新取得画面をPNG・正確なBGRAで保存し、認識結果、寸法、取得時刻、frame ID、SHA-256、失敗診断をmanifestへ対応付ける。CIで保存を有効にし、artifactから認識失敗の原因を調べられるようにする。診断保存の失敗は元の認識失敗を置き換えず、保存先は新規directoryとして既存証拠を保持する。
- Issue #459: 2048 campaignが新しいwindow frameを取得した直後、画像解析や操作候補の検証を待たずにFailSafeRuntimeへ観測時刻を記録する。最初のplayable boardでFailSafeをre-armする順序と5秒の安全期限は変えず、画面状態の取得・解析が続く間は実入力を開始しない。認識timeout reportには、盤面confidenceに加えて候補生成に必要な最低confidenceも記録する。raw frame取得後に観測更新callbackが動く順序と、episode初回・2回目の観測更新を回帰テストで確認する。
- Issue #287: GitHub Actions workflow checkerでトップレベルtoken権限、write/all-scope grant、危険trigger、run scriptへのevent/input/output/secret直接展開、checkout credential永続化、workflow_run artifact、Reusable workflowのsecret継承を検査する。例外には規則ID、UTC有効期限、10文字以上の理由が必要で、期限切れ例外は抑止しない。7つのworkflowに`contents: read`が既に定義されていることを確認し、19個の読み取り用checkoutに`persist-credentials: false`を設定する。`finish_task.bat`と`.github/workflows/tests.yml`からcheckerを実行する。
- Issue #341: ExecutionHistoryとKnowledgeStoreは共通のJSON配列writerで保存し、HistoryStoreは同じ原子保存基盤のstage APIで遅延確定する。共通保存基盤は同一ディレクトリの一時ファイルへ書き、ファイル内容をOSへ同期した後に置換する。ExecutionHistoryは不正JSONを上書きせず報告し、書込・置換失敗時に既存ファイルを保持する。
- Issue #122: 現行実装と未実装の対策を区別したThreat Modelを追加する。ゲーム画面由来の間接Prompt Injection、UI偽装・誤対象、Provider / Remote data、Plugin、Artifact、path / archive、supply-chain、Resource exhaustion、反復入力を信頼境界ごとに記録し、各脅威のprevent / detect / contain / recover、残余リスク、関連test・CI・担当領域、更新条件を示す。Provider endpointはloopback既定だが画面から変更できること、Remote consent・Plugin隔離・外部Artifact intake・main required-check設定が未確認または未実装であることを明記する。
- Issue #77: Contributor向けSubsystem Ownership Mapを追加し、9つのAreaについて現行コード、契約境界、関連Issue、作業スキル、レビュー感度、未指名の担当候補を整理する。READMEから地図へ移動できるようにし、未指名の領域別レビュアーを推測でCODEOWNERSへ割り当てない方針を記録する。現行リポジトリに存在しない`plugins/`の案内をruntime architectureから除き、MOD / Processing Hookの実装Issueを示す。
- Issue #247: `CampaignRunner`に600秒PR/1800秒Releaseのprofile、対象ゲームごとに差し替える`CampaignEpisode`契約、deadlineまでのlive-input閉ループ、任意の`CampaignState` milestone callback、game-over後のepisode再起動、回数制限付きstall recoveryとstep上限を追加。callbackはaction後の状態を判定し、到達・判定失敗をstop reasonへ分ける。状態遷移をJSONLへ追記し、stop reason/failure category、milestone有効・到達状態、episode/restart/recovery数、last/peak score・score観測数・max tile、action-to-observation latency、Gitとpinned-game metadataをJSON reportへ保存する。2048のUP/DOWN/LEFT/RIGHT候補は対応するWindows仮想キーコードでNative SafetyGuardへ渡す。2048 episodeは画面状態を取得した後にFailSafeRuntimeの観測完了時刻を更新する。Windows CIで記録したaction-to-observationの最大値2,666msを超える猶予を設けるため、episodeの観測期限は5秒とする。5秒間観測時刻が更新されなければwatchdogが追加の実入力を拒否する。2048 CLIは専用browser HWNDへのwindow-message inputを使い、CIでPR profileを実行する。偽episodeの単体テストでduration、milestone、live-input条件、game-over restart、bounded recovery、hard failure分類、既存artifact保護を検証し、矢印キー変換、観測時刻の更新、FailSafe停止理由のartifact記録も単体テストする。
- Issue #245: MIT版2048のソースを固定commitから取得するsource lockへGit tree SHAと全34ファイルのGit blob SHAを記録し、cacheに欠落・追加・変更されたファイルがあれば再利用を拒否する。検証済みsourceはloopback限定HTTP serverと専用Edge/Chrome profileでWindows上に起動し、launcherは起動したPIDと2048のwindow titleで対象を識別してdesktop内に最大1180×850で配置する。Windowsのforeground制限で前面化できない場合も起動を失敗させず、その状態をJSON出力へ記録する。終了時は起動したbrowser process tree・一時profile・HTTP serverを閉じ、検証済みsource cacheは再利用のため保持する。

- Issue #634 / #96 / #475: 保留中の独立10種テストゲームだけを取り込み、GPL専用環境・テスト・license境界へ分離します。通常入力、pause/resume、同seed restart、メニューと終了を全10種のLinux/X11実入力で検証し、将棋の欠落glyphと失敗時cleanupを修正しました。SDL描画とOS入力・AI自律攻略を区別し、後者とWindows実機・長時間試験は#30/#96へ残します。python-shogi 1.1.1の実配布宣言GPL-3.0-onlyを記録します。
- Issue #246: pinned 2048画面のCSS色と4x4配置からboardを復元し、同じcapture時点のOCR Observationから現在scoreとgame-over状態を得るScreen Adapterを追加する。状態にはboard矩形・cell/board confidence・再現可能なstate signatureを記録し、board confidenceが0.65以上かつgame-overでない場合だけUP/DOWN/LEFT/RIGHTのkeyboard候補を返す。CSS・grid・OCRデータから作る合成pixel fixtureでboard/score/terminal/action経路を検証し、Windows launcher integration testでは起動windowのcaptureからboardと4候補を検査する。launcherは小さいWindows desktopで盤面全体をcaptureするためChromium device scale factorを0.75にする。

- Issue #479: 汎用の任意登録`TransitionModel`契約と、実入力を発行しない2048参照遷移を追加しました。移動・一手一合成と確率spawnを分離し、不変盤面、未認識score、勝利後継続、終了、seed付き仮想restartを扱います。未知ゲームのモデルは利用不可として返します。
- Issue #479: hash検証済み固定版JavaScriptとslide/move/continue/restartを比較するOracle、CPU/heap測定、単体試験とCIの比較を追加しました。仮説更新・探索・本番Decisionへの接続は#478/#565-#567に残り、この追加だけでは実プレイの強化済みとは扱いません。[設計と検証手順](architecture/transition_simulation.md)を追加しました。
- Issue #563（親 #478）: typed immutable WorldStateHypothesis / BeliefSnapshot と bounded Candidate Store を追加。校正確率と比較score、予測と観測仮説を区別し、同ID再送・相関証拠を再加算せず、Top-K / byte / TTLで除外した質量をunknownへ残します。Frame / turn / session / action epoch / source generation / clock originの混在、可変state・循環・NaN・timeoutを検査し、CPU測定と8/16曖昧2048・カード・位置fixtureを追加。本番Bayesian更新・Recognition接続は #564/#405/#407、実プレイは後続です。詳細は `doc/architecture/belief_snapshot.md`。

- Issue #564（親 #478）: 型付きBeliefへPredict→Observe→Bayesian posterior更新を追加。排他的な遷移経路の確率合算、joint観測尤度、log空間、unknown尤度不明時の比較score、receipt/frame/相関の二重適用防止、旧prior版CAS、候補/byte/履歴/deadline上限を検証します。実Recognition校正と本番Planner接続は#405/#407/#565-#567に残します。詳細は `doc/architecture/belief_update.md`。
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

- Issue #613: Session記録、Experience／Knowledge、Artifact参照、Dataset生成とモデル学習・採用を分ける図解を追加しました。未実行と実入力、出所、保存とデータ保護の制約を区別します。

- Issue #261: versioned Session Event EnvelopeとSQLite WAL Journalを追加。UTC/monotonic timestamp、session sequence、correlation IDs、compact payload/artifact referencesを定義し、complete record検証・未コミットtransactionの復旧・flush/checkpointを実装。SQLite本体とfsync付きJSONL参照の30分相当append benchmarkを追加。
- Issue #204: Native Runtimeへ粗粒度FAST_CV frame-preprocess C ABIを追加。BGRAからRGB/brightness統計、dHash、4近傍bright-regionをNativeでまとめて計算し、caller-owned region buffer・buffer-too-small retry・timing/frame-payload/output/copy metricsを定義。NativeRuntime bindingはproviderが報告したinput-copy metricsを受け入れ、capacity retryを含む各ABI呼び出しの値を合算する。FrameAnalyzer注入点と同じPython参照実装へのfallbackを追加。C/C++ ABI testとWindows DLLを使ったPython/native parity testで検証。

- Issue #610: 画面取得・画像特徴・任意OCR・候補統合の局所図と具体例を追加しました。古いフレームの再待機と期限切れ、OCR依存不足による省略と実行エラーを区別し、Native前処理の明示注入を説明します。
- Issue #147: GUIからProvider・Pipeline・候補評価器の生成をRuntimeCompositionへ分離。GameSessionControllerが保持するPipelineとProviderをSession中に共有し、終了時はPipelineを先に閉じてProviderを解放する。部分初期化・終了失敗をFakeで検証し、失敗したresourceだけを再試行できるようにする。

- Issue #145: start/stop/one-step/continuous-loopをTkinterからGUI-freeのGameSessionControllerへ移動。repeat/outcome stop policy、generation-guarded timer cancellation、error/terminal lifecycleとSession-scoped DecisionPipeline/Engineの再利用・一括shutdownを追加し、fake runtime/schedulerで検証。

- Issue #145 review follow-up: 実入力許可の解除で即時停止し、各実行stepでも再検証。loop observer例外をFAILEDへ遷移させ、終了に失敗したruntimeを保持して再開を拒否。状態通知失敗を隔離し、各経路を回帰テスト。
- Issue #331: 判断時に評価・許可された候補オブジェクトを実行まで引き継ぎ、操作IDから元候補を再検索しないようにする。操作IDが重複する候補はすべて拒否し、Providerが候補リストを同じIDの別候補へ置き換えても元の評価済み候補だけを実行する。Decision Contextには一括評価した重複拒否理由を記録し、既存の`GamePlayerEngine.step()`拡張も実行経路で呼び出す。

- Issue #611: 判断・結果評価と処理選択の図解を追加しました。既定規則の目標意味評価、複数時点の追跡観測、未送信判断への結果帰属を保証済みとせず、現状と拡張要求を分けます。

- Issue #332: Ollamaの画面評価・候補判断をUI thread外で実行し、推論中の停止を受理する。実行中のキー保持・待機もRunControllerの停止状態とstep開始世代を監視して中断し、保持キーを解除する。停止後・再開後に返った旧推論結果は判断履歴と実入力へ進めず、停止理由をログへ記録する。mouse方式の連続実行では、入力workerがSetCursorPosの開始・完了とクリック先をUIへ通知し、自動カーソル移動を手動移動による停止と誤認しない。未通知の手動カーソル移動による停止は維持する。

- PR #345: 重複した画面評価workerは起動順IDで新旧を判定し、最新より古い結果を観測履歴・評価表示へ反映しない。停止中に完了した手動画面評価は実行世代に関係なく評価表示へ反映する。古い評価結果が現在有効な連続実行stepに属する場合は連続実行を停止する。「再開」操作で実行世代が更新された場合も旧連続実行状態と予約callbackを解除し、旧workerの結果を破棄した後に連続実行を再開できる状態に戻す。

- PR #345: 判断履歴と判断traceを一時ファイルへ先に準備し、RunControllerが実行世代を検証してから両ファイルとエンジンの前回判断状態を公開する。Stopが一時ファイル準備中に届いた場合、workerは未公開データを破棄する。

 - Issue #336: 実入力開始時に対象HWND/PIDの選択・有効性を検証し、HWND再利用による別プロセスへの入力を実行直前に拒否する。連続実行は対象未選択で開始せず、各ステップ失敗時に停止する。

- Issue #612: 操作許可・安全検査・入力・停止の図解を追加しました。確認運転、Tk依存のGUI監視と背景監視、最終停止確認との競合、実行器依頼とOS送信開始の区別を説明します。

- Issue #137: Tkinter UIを上部Command/Status + 左Navigator / 中央Workspace mount points / 右Inspector / 下部Bottom Panelへ分離。Play/Vision/Reasoning/Evaluation/History/Mods間を切り替え、Inspector/Bottom Panelをresize・開閉可能。Workspaceと表示状態を`data/shell_state.json`へ独立保存し、実行制御は既存Applicationから停止commandとして接続。

- Issue #313: ai-context-reducerのTask Routing / Remote Delta Firstを開発入口へ適用。Issue一覧は本文なしのmetadataだけを全ページ取得し、P0-P5・親Issue除外・明示指定で対象1件の本文のみ取得。文字数上限・省略表示・原典ポインタ・失敗時の古いpack再利用禁止と回帰テストを追加。AST索引/Test Impactは別Issueのまま。

- Issue #232: Native Runtime C ABI v1の薄いPython loader/binding、ABI versionとstruct検証、init/shutdown所有権、batch/status変換、明示的availability、Linux/Windows実共有ライブラリ結合テストを追加。capture/inputやbuffer実装・GUI自動切替は含まない。

- Issue #148: C ABI v1へsize/version付き初期化options、opaque runtime handleのinit/shutdown lifecycle、安定status code、未実装のcoarse batch入口をfail closedする契約とC/C++ contract testを追加。Issue #86の段階導入方針に沿い、mypyは依存moduleの型推論を維持しつつ、指定root 6ファイルのstrict検査へ範囲を明示。

- Issue #182: Learning Capabilityのversioned schemaに沿うExperience Dataset Builder、5種のtyped sample、provenance/label・sensitive artifact制御、dedup、再現可能split、JSON export/importとcontent digestを追加。

- Issue #529: game/scene/profile/Player lineageで隔離した不変KnowledgeSnapshot v1と、既存Artifact Storeを使う保存・読取APIを追加しました。行動結果4分類、prior、数値hint、出所、uncertainty、期限、重複投入追跡用watermarkを検証し、欠落・破損・scope/版不整合でstatic defaultへ戻す結果を返します。Artifactの有界読込も追加し、metadata/payloadの過大入力をSHA検証付きで拒否します。[契約と検証範囲](architecture/knowledge_snapshot.md)を参照してください。Builder/判断反映/昇格は#530-#532に残ります。

- Issue #530: 検証済みExperienceからcount・Beta推定・EWMA・遷移頻度と数値hintを増分集計し、昇格前Knowledge候補を生成するBuilderを追加しました。unknownとno-effect、実入力とdry-runを分け、source/操作世代のwatermarkで再投入・相関frameを排除します。元Snapshotを変更せず、scope・保持期限・資源上限・privacyの条件を保持します。[設計と計測](architecture/knowledge_builder.md)を追加しました。Runtime更新・判断反映・昇格は#422/#531/#532で扱います。

- Issue #531: scope・期限を照合したKnowledgeをstep固定のsoft evidenceとしてBaseline/Engine/Pipelineへ接続します。候補許可・Safety・認識最低値を変えず、priorと重みhintで判断が変化します。unknownはno-effectに変換しません。モデル公開は明示opt-inの4KiB圧縮文脈、ローカルTraceは版・hash・出所を保持します。[設計・残件・検証](architecture/knowledge_feature_adapter.md)を追加しました。

- Issue #532: 不変KnowledgeのChampion/Challengerを同じ根拠付きケースで実Provider・DecisionVerifierへ通して比較し、成果・回帰・Python時間/heapから昇格を判定するGateを追加しました。対象SHA・Policy・測定を保存し、世代CASと原子的pointer/監査更新、step固定、過去のvalid版へのrollbackを提供します。外部passedフラグを受け付けず、#171/#173の汎用engine・#422の自動学習配線・Windows実機検証とは区別します。[設計と検証](architecture/knowledge_promotion.md)を参照してください。

- Issue #168: JSONL Experience Readerの任意Step/検索index、snapshot/delta再構築、schema v0移行、破損tail回復、checkpoint検証とArtifact参照検査を追加。

- Issue #167: SHA-256 content-addressed Artifact Store、atomic publication、artifact type metadata schema、dedup/integrity/orphan検査とlocal benchmarkを追加。

- Issue #166: Episode/Step/Event canonical Experience schema、source/model provenance、sensitive artifact referencesと論理Reader APIを追加。

- Issue #181: Model learning capability、Dataset/Trainer compatibility、artifact provenanceおよびuntrainable状態を表すversioned contractとJSON Schemasを追加。

- Issue #220: Capability Manifestを使うComposition Resolver、preferred/default選択、dependency/health検査、fallbackとdegraded reason、provider lifecycle cleanupを追加。

- Issue #609: モデルと手法の配置図、標準・任意経路、設定／選択／読み込み／実呼出しを区別する証拠の案内を追加しました。GUI状態評価と条件付き意味評価、外部Ollama送信を区別し、実行証拠表示の残作業を#627へ接続します。

- Issue #219: Optional feature capability manifestのJSON Schema、runtime検証、required/optional dependency・lifecycle・config namespace・fallback/degraded mode宣言と検証を追加。

- Issue #475 / PR #177（#174・#96・#124の部分成果）: Provider API v1のJSON契約・例、モデル候補と4参照ゲームの固定ソース宣言、ソース取得とVRAM計測の補助ツール、契約CI、1.0配布・ライセンス方針を追加します。取得済みのゲームとlockを置く`build/ci-games/`はGit管理から除外します。v1 HTTP接続・認証・Adapter適合は#238-#240、bundled実推論は#241-#244/#280、全ゲーム・実GPU・配布受入れは#96/#248-#251、release用Player Asset差替えは#155へ残します。v2汎用Task契約の正本#447と区別し、現在のNoisy Provider・2048・Player Seat実装を保持します。

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
- Issue #421: 5 Mode / 8 Phase、既定 Light、Mode 由来の能力 flag と矛盾 config を拒否する Schema・default・operation 表を追加。共有 RunController の停止世代、安全境界での Mode 変更、Step の Artifact 版固定、held input がある学習移行の拒否、評価合格後の公開 gate を実装します。遅い probe / Journal IO を Stop lock から分離し、有界 metadata 記録、atomic checkpoint、失敗時の停止、crash 後の停止 IDLE 復元を提供します。通常の Outcome / Verifier / Safety 評価は学習 Artifact 評価の flag から独立して継続します。既存 Session / InputLedger / EventJournal 接続はテスト済みで、本番 Light Runner は #422、交互実行 #423、並行学習 #424、資源制御 #425、UI #188 は後続です。詳細は `doc/architecture/runtime_modes.md`。
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


## 1.0.0 scope

1.0.0 release boundary is defined in `doc/release_1_0.md` and Issue #19.

Key acceptance:
- Windows x64 / local-first
- distribution-root executable as the normal user entrypoint
- bundled/automatic Local Inference setup
- Basic Game/Window Select -> Start/Stop path
- default privacy: telemetry/remote/raw-frame persistence/replay persistence OFF
- routine CI: smoke-tiny endurance, standard-small functional inference, Noisy Provider robustness
- pre-release: #179 30-minute-class automated/reference and GTX 1080 real-game acceptance
- first-party project code/docs use MIT; Kadoka/Maru character assets use Obake License
- final Product/Engine name must be recorded in #154 before 1.0.0


### 1.0.0 licensing boundary

- 1.0.0 first-party shipped scope is MIT-only.
- Wise Misk artwork is excluded from 1.0.0 while its license is undecided; the planned bundled player asset is MIT-licensed Super Icon.
- The current development manifest records Tomiya Character License v1.0.1, but that declaration does not resolve the asset-rights acceptance still open under Issue #19/#124/#228. The earlier unresolved status and 1.0.0 exclusion are retained; release-specific replacement remains tracked by #155.
- Obake License assets are deferred to 1.1+.
- Kadoka/Maru Character Mode and related Obake-license UI work are not 1.0.0 blockers.
