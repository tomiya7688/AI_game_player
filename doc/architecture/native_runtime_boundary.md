# Native Runtimeの配置宣言と境界コストを検査する

この文書は、AI Game PlayerのPythonとC++の責務を変更する開発者向けです。配置の理由をmanifestへ記録し、契約宣言の不一致を検出し、言語境界のコストを測る手順を説明します。Pythonの基本文法と、共有ライブラリの関数を呼び出すC ABIの概要を知っている読者を想定します。

## 目的と現在の実装

Pythonは画面の意味認識、操作候補の評価、判断、結果評価、UIの組立てを担当します。C++はversion付きC ABIを公開し、決定論的な安全検証や任意のWindows Graphics Capture（WGC）を実装します。WGCはWindowsのウィンドウ画像を取得する仕組みです。Pythonの画面取得や入力処理も残っています。能力を宣言することと、対象環境でその能力が動くことは別に確認します。

- 配置の宣言: [`config/runtime_boundary.json`](../../config/runtime_boundary.json)
- Pythonの契約: [`runtime/contracts.py`](../../src/ai_game_player/runtime/contracts.py)
- C ABIの契約: [`runtime_api.h`](../../native/include/kadoka/runtime_api.h)
- 能力ビットの読取: [`runtime_boundary_contract.py`](../../tools/runtime_boundary_contract.py)
- レポートの公開: [`runtime_boundary_report.py`](../../tools/runtime_boundary_report.py)
- 宣言の検査: [`runtime_boundary_check.py`](../../tools/runtime_boundary_check.py)
- コストの測定: [`runtime_boundary_benchmark.py`](../../tools/runtime_boundary_benchmark.py)

Native実装はC++の型やSTLコンテナーを公開せず、C ABIを使います。配置manifest v1は、現在のPython契約とNative ABIのversionが一致することを要求します。アプリの製品バージョンと契約のversionは別の値です。

## 配置を決める基準

| 責務 | 現状と選定理由 |
|---|---|
| 画面取得 | Python実装と任意のC++ WGC実装があります。OS APIへの距離と取得頻度を考慮し、画像一枚を単位に渡します。 |
| 入力とrelease-all | Python実装が残っています。将来のNative実装でも判断・評価側の契約を作り直さないよう、操作のまとまりを境界にします。 |
| 安全制御 | 意味判断・停止管理はPython、決定論的な操作検証はC ABIにもあります。Native側の独立した停止機構の成立は、このmanifest検査では証明しません。 |
| 画像の差分・領域処理 | Nativeライブラリで処理済みの部分を無根拠に移植しません。Python内の反復が負担になる場合は実測して配置を選びます。 |
| 判断材料、モデル連携、結果評価 | AI・認識ライブラリとの接続と変更頻度を考慮し、Pythonに置きます。 |
| 経験・学習・分析、UI・開発ツール | Pythonに置き、別言語の導入による利益が確認できた場合に再検討します。 |
| ゲーム固有の規則 | 現在はPythonです。Luaはプラグインの契約と権限境界が成立した後に再評価します。 |

manifestにはPython、C++、Lua、C#、JavaScript/TypeScriptごとの採否と用途を記録します。各componentの`current_languages`は評価済みの言語IDを重複なく並べた一覧です。複数言語を含む場合も架空の言語名へ結合しません。保留する言語には再評価条件も記録してください。新たな言語を使う開発者は、呼出し頻度、遅延、Python内の反復、OS APIとの関係、安全責務、AI依存、配布・起動コストをIssueまたはPRへ記載してください。

言語の切替回数は`frame / observation / candidate_set / decision / batch / control_lease`のまとまりに限定します。ピクセル、トークン、候補一件ごとのFFI・IPCは許可しません。FFIは同一プロセスの外部関数呼出し、IPCは別プロセスとの通信です。

大きな画像は、ビュー、共有メモリ、連続したコピー、直列化のコストと、所有権・解放時期を比較して方式を選びます。プロセス分離が必要な理由がある場合にIPCを選び、言語が異なることだけを理由にIPCを増やしません。C++化率そのものは成果指標にしません。

## 配置manifestを検査する

開発者はリポジトリ直下で実行してください。

```powershell
python tools/runtime_boundary_check.py
python -m unittest discover -s tests -p "test_runtime_boundary*.py" -v
```

検査はmanifestとPythonのAST、Cヘッダーを読み、コードを実行せず宣言を照合します。成功時は`RUNTIME BOUNDARY OK`と終了値0を返します。不正な型、重複、不明な粒度、version・能力の不一致、能力ごとの配置の欠落・重複、読取失敗は終了値1になります。`--manifest PATH`で変更前のmanifest案を指定できます。

配布欄は、self-containedの宣言、リポジトリ内にあるビルド入口、その入口に宣言したDLL名があることを検査します。この文字列検査では、実際のDLL生成、Native bufferの所有権、一般利用者のWindows環境での起動を証明できません。

`finish_task.bat`と`language-ci`がこの検査を実行します。現在のC ABIやPythonの能力を増やした開発者は、配置manifest、契約、対応テストを同じ変更で更新してください。能力名だけでなく64bitの非ゼロ・単一ビット・重複も検査し、Python bindingの実割当と照合します。Cの任意の式を実行せず、整数・左シフト・ORの宣言だけを読みます。Python bindingのABI versionとCAP定数も照合します。

## 境界コストを測定する

開発者はCMakeと対象OSのC++20ビルド環境を用意し、リポジトリ直下で実行してください。WindowsではC++20と互換のあるC++/WinRTヘッダーも必要です。MSVC 19.51とWindows SDK 19041の組合せでは、古いexperimental coroutineヘッダーとの互換エラーが発生することを確認しています。この組合せでのローカル測定は未確認です。

```powershell
cmake -S native -B build/native-boundary -DCMAKE_BUILD_TYPE=Release
cmake --build build/native-boundary --config Release --parallel
ctest --test-dir build/native-boundary -C Release --output-on-failure
python tools/runtime_boundary_benchmark.py --search-root build/native-boundary --output build/runtime-boundary/local.json --samples 1000
```

ライブラリが複数見つかった場合は`--native-library PATH`で対象を一つ指定してください。読込対象はABI v1のビルド済みライブラリです。測定中にOS入力は送らず、Native呼出しはABI照会と操作内容の安全検証に限定します。

測定は次の六項目をJSONに保存します。各項目は実行回数、マイクロ秒単位のp50・p95・p99と平均を記録します。分位点は、計測値を昇順に並べた後、`(計測数 - 1) × 分位`を切り捨てた位置の値です。

| 項目 | 測る範囲 |
|---|---|
| `ffi_abi_version` | ABI versionを一回照会する呼出し |
| `ffi_safety_validate` | 固定のクリック候補をNativeで検証する呼出し。実クリックは行いません。 |
| `json_observation_candidate_batch_roundtrip` | 固定した観測と32候補をPython内でJSONへ変換して戻す処理 |
| `frame_1080p_rgb_copy` | Python内の1920×1080 RGBデータをbytesへコピーする処理 |
| `frame_1080p_rgb_memoryview` | 同じPythonデータへのmemoryviewを作る処理。Nativeへのzero-copy転送は測りません。 |
| `ipc_pipe_4k_roundtrip` | spawnで起動した専用workerと4KiBを往復する処理。起動時間は測定値へ含めません。 |

IPCの応答待ちは既定5秒で打ち切ります。`--ipc-timeout-seconds`で有限の正数を指定できます。workerが返答しない・終了しない場合は終了を要求して回収し、測定を失敗させます。CIの測定stepにも3分の上限があります。失敗時は`RUNTIME BOUNDARY BENCHMARK ERROR`と終了値1を返し、成功した測定として扱いません。

レポートは、引数解析後に保存先を登録した時点で旧成功を`running`状態へ原子的に置換します。計測・読込に失敗した登録済み試行は`failed`を保存します。`running`は未完了であり、生存中のプロセスを保証する状態ではありません。保存自体に失敗した場合もCLIは非ゼロで終了し、成功を表示しません。

同じ保存先はOSファイルロックと試行ID（`run_id`）で調停し、最後に登録した試行だけが`status: success`の測定値を公開します。古い試行の完了・失敗で新しい結果を消しません。完全なJSONを一時ファイルへ同期してから置換するため、通常の書込失敗で途中のJSONを公開しません。利用者は終了値0、成功行の`run_id`、JSONの同じIDと`status: success`を合わせて確認してください。過去の測定値を比較する場合は、試行ごとに別の`--output`を指定してください。

CIはUbuntuとWindowsでNativeをビルド・テストした後に測定し、`runtime-boundary-<OS>`成果物を保存します。既存のASan・UBSan・GCC解析、CTestの反復、Windows配布物の起動検査、実ゲームE2Eは維持します。

初期の測定値だけでNative配置の優位性を宣言しません。既存`performance-ci`の部品検査、境界コストの測定、`language-ci`の閉ループE2Eを分けて確認し、十分な基準値を得た後に回帰予算を検討します。

## この変更で完了しない要求

PR #136はIssue #56の配置宣言・整合性検査・境界コスト記録を実装します。Issue #56全体の完了を宣言する変更ではありません。

- Native bufferのownership・release・lifetimeと実データのFFIコピー回数はIssue #233で検証します。Pythonのmemoryview生成時間を代用しません。
- Native側の入力・停止・lease・watchdogは、Issue #56が委譲した入力・fail-safe側の要求として残ります。
- 一般利用者がPython・pip・CMake・MSVCなどを導入せず起動する配布物はIssue #149で扱います。CI runnerの起動検査だけで、clean Windowsの受入れを完了扱いにしません。
- 外部モデルやGPUドライバーを必須にする変更は、不足条件と利用者の対処を示す起動前検査も同時に実装してください。任意のモデルやRemote ProviderをCoreの起動条件へ加えません。
