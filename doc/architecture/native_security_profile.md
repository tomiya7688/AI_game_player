# Native C++ Security Profile（Issue #115 第1段）

[動作と配置の見取り図へ戻る](README.md)

この文書は、Native Runtime（`native/`）のC/C++ソースへ適用するSecurity検査の構成、判定基準、実行方法、未対応範囲を記録します。既存のNative build/test基盤（#87）とsanitizer実行（`language-ci` の `Native C++ ASan + UBSan + GCC analyzer`）を再実装せず、静的検査を追加します。

## 第1段で追加した検査

| 検査 | 実行口 | CIジョブ（`language-ci`） | 失敗条件 |
|---|---|---|---|
| 危険C/C++ API検出 | `python tools/native_security_check.py native` | `Native dangerous C/C++ API detector` | 許可リストに登録されていない medium 以上の検出、現在のソースに一致しない許可リスト項目、または検査対象ソース0件 |
| clang-tidy（bugprone / cert / clang-analyzer） | `python tools/native_clang_tidy.py --clang-tidy clang-tidy` | `Native clang-tidy security profile (ubuntu-latest)`、`(windows-latest)` | `native/.clang-tidy` で有効なcheckの警告が1件以上ある（`WarningsAsErrors: '*'`）、または解析対象0件 |
| ASan + UBSan + GCC analyzer（既存） | `cmake -DKADOKA_ENABLE_SANITIZERS=ON -DKADOKA_ENABLE_GCC_ANALYZER=ON` | `Native C++ ASan + UBSan + GCC analyzer` | sanitizer検出・analyzer警告・テスト失敗（既存のまま） |

3つの追加ジョブ名は `.github/rulesets/main-protection.json` の必須チェックへ追加しました。GitHub上のrulesetへの適用はリポジトリ設定の操作であり、このPRでは行いません。

## 危険API検出の規則

検出器はコメント、文字列リテラル、raw文字列、文字リテラルを空白へ置き換えてから、規則表の関数名の呼出し（`name(`、`std::name(`、`::name(`）を検出します。関数名と `(` の間に改行がある呼出しも検出し、関数名がある行を報告します。対象は標準C/C++とWindowsのグローバルAPIだけで、メンバー呼出し（`obj.free(`）、`std` 以外の名前空間・クラス修飾（`kadoka::memcpy(`、`Pool::free(`）、別名の識別子（`strcpy_s(`）は検出しません。独自名前空間で危険APIを包む関数の中身は、その定義側の呼出しとして検出されます。

| 規則 | 重大度 | 対象API | 理由 |
|---|---|---|---|
| NSC001 | high | `gets`、`strcpy`、`strcat`、`sprintf`、`vsprintf`、`wcscpy`、`wcscat`、`swprintf`、`lstrcpy*`、`wsprintf*` など | 出力容量を受け取らない文字列/書式API |
| NSC002 | high | `system`、`popen`、`WinExec`、`ShellExecute*`、`CreateProcess*` など | Native Runtimeからの外部コマンド/プロセス起動 |
| NSC003 | high | `alloca`、`_alloca`、`_malloca` | 入力サイズ由来のスタック溢れ |
| NSC004 | medium | `atoi`、`atol`、`atoll`、`atof`、`strtok`、`rand`、`srand` | 変換失敗を検出できない、再入不能、または予測可能 |
| NSC005 | medium | `memcpy`、`memmove`、`memset`、`CopyMemory`、`RtlCopyMemory` など | 容量検査を呼出し側に依存する生のメモリ転送 |
| NSC006 | medium | `malloc`、`calloc`、`realloc`、`free`、`_aligned_malloc`、`_aligned_free` | RAIIを経由しない手動ヒープ管理 |

- 既定の `--fail-on medium` では、すべての規則がmergeを止めます。`--fail-on high` を指定すると medium の検出は `advisory:` として表示だけ行います。
- 許可リストは `config/native_security_allowlist.json` です。各項目は `path`、`rule`、`function`、`code`（コメント・文字列を除き空白を1つに正規化した該当行）、10文字以上の `reason` を必須とします。行番号では照合しないため、該当行の内容が変わると項目は一致しなくなり、未使用項目として失敗します。1項目が許可するのは1件の検出だけで、同じ内容の行が複数あればその件数分の項目が必要です。`path` は `./` や `\` を含んでいても相対POSIXパスへ正規化して照合します。
- 現在の許可は `native/src/wgc_capture.cpp` の行単位 `std::memcpy` 1件だけです。コピー前に必要容量を `uint64_t` で検査しています。D3D Mapの `pData` / `RowPitch` の防御検査は #652 で扱います。

## clang-tidyの固定設定

- バージョンはPyPIの `clang-tidy==22.1.8` に固定し、`tools/native_clang_tidy.py` が `--version` を照合します。異なるバージョンでは解析せず終了コード2を返します。clang-tidy 18系はWindows CIのMSVC標準ライブラリ（Clang 20以上を要求）を解析できないため採用していません。
- 有効なcheckは `bugprone-*`、`cert-*`、`clang-analyzer-core.*`、`clang-analyzer-cplusplus.*`、`clang-analyzer-deadcode.*`、`clang-analyzer-security.*`、`clang-analyzer-unix.*` です。無効化したcheckと理由は `native/.clang-tidy` と `native/tests/.clang-tidy` の先頭コメントに記録します。
- 解析対象は `native/src/*.cpp` と `native/tests/*.{c,cpp}` です。`native/tests/wgc_capture_test.cpp` は `windows.h` を必要とするため、Windowsジョブだけで解析します。Linuxジョブは `wgc_capture.cpp` の非Windows stubを、WindowsジョブはWGC / Direct3D実装を解析します。
- 第1段の導入で検出された `runtime_api.h` の負のstatusマクロ（`bugprone-macro-parentheses`）は括弧で囲みました。値とC ABIは変わりません。

## ローカルでの実行

```powershell
python tools/native_security_check.py native
python -m pip install "clang-tidy==22.1.8"   # 開発用。通常の利用者セットアップには含めない
python tools/native_clang_tidy.py --clang-tidy clang-tidy
```

危険API検出は `tests/test_native_security_check.py` の実ソース検査として `finish_task.bat` の単体テストにも含まれます。clang-tidyはツールの導入を前提とするため、`finish_task.bat` には含めずCIで実行します。

## 未対応（#115の後続）

- CodeQL等のC++ SAST、cppcheck等の別系統の静的解析。
- Windows上のsanitizer（MSVC `/fsanitize=address`）。現行のASan/UBSanはLinuxでWGC stubを検査するだけです（#652のF3）。
- FFI境界の異常入力（不正stride、過大buffer等）のfuzz/mutation testと、WGC handle/resourceのlifetime・並行呼出し検査（#652、#233）。
- Safety / Input / FFIの変更に通常のNative変更より厳しいGateを適用する仕組み（#112のseverity policy）。
