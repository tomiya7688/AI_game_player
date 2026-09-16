# Native Runtime Boundary v1

Issue #56 の実装判断を、既存 `runtime_layers.md` の原則から実際の配置・契約・検証へ落とした正本です。

## 結論

Kadoka は **Python high-level core + C++ Native Runtime** を標準構成とする。ただし「C++化率」は目的ではなく、呼び出し頻度、OS APIへの距離、安全責務、FFI/IPC/copyコスト、変更頻度を含む end-to-end の Net Benefit で配置を決める。

通常ユーザーは言語境界を意識しない。Windows配布物は `tools/build_windows_bundle.ps1` から self-contained bundle を生成し、Python/CMake/MSVC等の手動導入を要求しない。

機械可読な配置正本は `config/runtime_boundary.json`、整合性検査は `tools/runtime_boundary_check.py` とする。

## Runtimeの最小構成

```text
src/ai_game_player/
  runtime/
    contracts.py       high-level capability contract v1
    registry.py        backend selection
native/
  include/kadoka/
    runtime_api.h      C ABI v1
  src/
    runtime_api.cpp    native implementation
  tests/
    runtime_api_test.cpp
config/
  runtime_boundary.json
```

High-level contract の `RUNTIME_CONTRACT_VERSION` と C ABI の `KADOKA_RUNTIME_ABI_VERSION` は同じversionを使う。Capabilityも `capture / input / safety / fast_cv` を両側で同じ集合として定義する。追加・破壊的変更ではversionを上げ、旧versionを黙って再解釈しない。

`runtime_boundary_check.py` はversion、capability集合、component配置、language評価、boundary粒度、Windows bundle宣言をCIで検証する。

## Component配置

| Component | 優先言語 | 現状 | Crossing単位 | 判断 |
|---|---|---|---|---|
| Capture / frame acquisition | C++ | Python Windows API | frame | DirectX/Windows API、高頻度処理のためNative target。1 pixelごとには跨がない |
| Input / release-all | C++ | Python Windows API | decision/action batch | OS/safety責務。将来はNativeへ移すが上位Decision契約は変えない |
| Safety validation | C++ | C ABI実装済み | decision | 軽量・決定論的・AI停止時にも必要 |
| Fast CV / screen diff / region primitive | C++ when measured | Python + native libraries | frame/batch | OpenCV/NumPy等が既にnativeなら無理に移さない。Python loopがhotならNative化 |
| Decision Context / provider orchestration | Python | Python | candidate_set/observation | AI/ML ecosystemと変更頻度を優先 |
| Evaluation / Outcome semantic logic | Python | Python | observation | Native Safetyと意味評価を分離 |
| Learning / Experience / analysis | Python | Python | batch | 実験性と分析ライブラリを優先 |
| UI / tooling | Python | Python | batch | 現時点で別runtime導入の利益がない |
| Game-specific rule/plugin | Python、将来Lua候補 | Python | candidate_set | Plugin ABI安定後に再評価 |

重要なのは、Capture/Inputが現在Pythonであることを「最終配置」と見なさない点である。外部契約をframe / decision等の粗粒度で固定し、Native実装へ差し替える時にRecognition/Decision/Evaluation全体を書き換えない。

## 境界ルール

許可する主要単位は `frame / observation / candidate_set / decision / batch / control_lease`。`pixel / token / candidate` 単位のFFI/IPC crossingは禁止する。

large frame/tensorは以下の順で比較する。

1. buffer view / zero-copy
2. shared memory
3. contiguous copy
4. serialization

Safety分離のようにprocess isolation自体に意味がある場合のみIPCを選ぶ。同一process C ABIで足りる処理を、言語が違うという理由だけでprocess分離しない。

## 言語評価

### Python — Adopted

AI/ML、OCR/VLM orchestration、Decision Context、Evaluation、Learning、UI/toolingを担当する。ライブラリ利用と実験速度の利益が大きく、各frame/pixelをPython loopで処理しない限りはhigh-level側に置く。

### C++ — Adopted

Native Runtime、Safety、Windows Input/Capture target、fast CV targetを担当する。C ABIを公開境界とし、C++型やSTL containerを外部contractへ露出しない。

### Lua / LuaJIT — Deferred after design evaluation

**価値:** C++へ埋め込みやすく、game-specific rule/policyを小さく配布しやすい。plugin sandboxやuser ruleの編集体験にも適する可能性がある。

**現在採用しない理由:** plugin ABIとpermission modelがまだ安定しておらず、現段階でLua runtime、binding、debugging、packagingを増やすとP0閉ループに対するNet Benefitが負になる。Python rule層で機能上のblocking gapもない。

**再評価条件:** versioned plugin ABIとpermission boundaryが成立し、Lua埋め込みによるstartup/memory/deployment costを含めても拡張性・sandbox性が上回る時。

### C# / .NET — Deferred after design evaluation

**価値:** Windows shell、tray、accessibility、installer、lifecycle、native interopで有利になり得る。

**現在採用しない理由:** Tk/PyInstaller + C++ DLLのbundleがclean Windows相当CIで起動できており、現状のUI要件だけでは追加runtime/IPC/packagingの利益が不足する。

**再評価条件:** WinUI/Accessibility/tray/service/installer等のWindows shell要件が現在のUIを明確に上回る時。採用時もAI coreをC#へ移植することは前提にしない。

### JavaScript / TypeScript — Optional

Web control panelやdeveloper toolingには適する。Safety/Input/Capture中核には置かず、追加時はpackage lockと既存language-ciを必須とする。

## Performance判定

`tools/runtime_boundary_benchmark.py` は、Native Runtimeをbuildした上で以下を実測してJSON artifactへ保存する。

- C ABI function call
- Native Safety call
- representative Observation + Candidate batchのJSON roundtrip
- 1080p RGB frameの実copy
- 同frameのmemoryview生成
- 4KiB control messageのprocess IPC roundtrip

p50 / p95 / p99を保存する。単体hot pathは既存 `performance-ci`、実際のclosed-loopは `language-ci` のWindows E2Eで測る。したがって評価面は以下で分担する。

```text
component        -> performance-ci
FFI/IPC/copy     -> runtime-boundary benchmark
end-to-end step  -> Windows E2E
```

runner差が大きいmicrobenchmarkを厳しい固定値でmerge blockしない。初期は計測値をartifactとして蓄積し、十分なbaselineが得られた項目のみregression budgetへ昇格する。

## Native Runtime test policy

Native ABIはUbuntu/Windows両方でCMake/CTestを通す。少なくとも次をNative単体で検査する。

- ABI version/query
- capability reporting
- invalid struct/action fail-closed
- coordinate boundary
- denied system key
- hold timeout
- safety-required primitiveの正常系

Python側はNativeをoptional backendとして扱っても、Safety required contractそのものはoptionalにしない。Native libraryが無い環境で黙って「Native capabilityあり」と報告してはならない。

## Distribution / startup

一般ユーザー向けWindows配布は以下を満たす。

- `tools/build_windows_bundle.ps1` がC++ Release buildとPython bundleを同じentrypointで実行する
- `kadoka_native_runtime.dll` を同梱する
- `Kadoka.exe --smoke-test` をCIで実行しApplication初期化まで確認する
- PATH、Python、pip、CMake、MSVC等の手設定を要求しない
- runtime中にpackage downloadを必須にしない

Minimal profileでは現在vendor GPU/NPU driverを必須としない。将来GPU/NPU等を必須化するProvider/Profileは、同梱不能なdriverを起動前に検出し、名称・不足条件・fallbackをユーザーへ提示するpreflightを**同じ変更で**追加しなければならない。preflight無しの外部driver必須化は禁止する。

ModelについてもMinimal profileは外部model download無しで安全に起動できる状態を維持する。Remote/Ollama等のoptional providerはCore起動条件にしない。

## 新規Issueの言語選定

性能/OS/Safetyに触れる新規実装は実装前に以下をIssue/PRへ残す。

- expected call frequency / latency
- Python/high-level loop量
- OS/native API proximity
- Safety責務
- AI/ML依存と変更頻度
- FFI/IPC/serialization/copy cost
- zero-copy/batch可能性
- distribution/runtime cost

既存コードを温存できることだけを理由にPythonを選ばない。同様に「C++の方が速そう」だけで境界を増やさない。

## Issue #56 完了判定

この境界確立Issueは、Capture/InputそのもののNative実装完了を要求しない。それらの**最終配置と交換可能なboundaryを先に固定する**Issueである。個別Native実装は実測と機能Issueに従う。

完了時点で必要なのは、versioned contract、配置決定、language評価、Native test、boundary benchmark、self-contained bundle/E2E、AI agent向け言語選定ruleが相互に整合しCIで検証されることである。
