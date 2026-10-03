# Native Runtime Python接続

`ai_game_player.runtime.native`は[C ABI v1](architecture/native_abi_v1.md)に対する薄い`ctypes`接続層です。importだけでは共有ライブラリをロードせず、GUIや既存Python実行経路を自動的に切り替えません。

## 発見と互換性

`discover_native_runtime(path=None)`は`NativeLoadResult`を返します。

- `available`: ABI検証と初期化に成功。`runtime`の終了責任を呼び出し元へ渡します。
- `unavailable`: 指定または同梱先にライブラリがありません。
- `incompatible`: 必須symbol、ABI version、返却structの契約が不一致です。
- `load_failed`: DLL依存解決、path、query/initなどで失敗。`reason`で原因を返します。

探索順は明示`path`、環境変数`KADOKA_NATIVE_RUNTIME`、自動同梱探索の順です。明示pathまたは非空の環境変数を指定した場合、その一件のみ試し、失敗しても別ライブラリへ置き換えません。自動探索はPython実行ファイルと同じディレクトリ、次にPyInstallerの`_MEIPASS`内のOS別ライブラリ名を試します。作業ディレクトリやPATHは探索しません。WindowsのDLL依存解決はDLL隣接ディレクトリと既定の安全な探索先に限定します。明示指定するライブラリは信頼できるものにしてください。

libraryのABI versionをstructを渡す前に照合し、全関数の`argtypes/restype`を設定します。query結果のsize/versionも検証してからinitします。未知capability bitは`info.capability_bits`に保持しますが、既知の機能として宣伝しません。Python fallbackの採否は呼び出し元の責任であり、この層はエラーを隠して自動fallbackしません。

## 所有権と終了

```python
from ai_game_player.runtime import NativeRuntime

with NativeRuntime.load("/absolute/path/to/libkadoka_native_runtime.so") as runtime:
    print(runtime.info.abi_version, runtime.descriptor.capabilities)
```

成功したinitごとに一つのhandleを所有します。必ず`with`または`close()`で終了してください。GC/destructor任せの解放はありません。instanceはDLLへの参照を保持し、batchとshutdownをinstance単位のlockで直列化します。`close()`はPython側では冪等です。終了後のbatch呼び出しはC ABIへ渡しません。block内で例外が発生しても終了し、通常は元の例外を伝播します。shutdownが失敗した場合は終了エラーを伝播します。

終了エラーの場合も通常呼び出しは無効化します。handleが残っていれば`close()`で終了だけ再試行できます。終了または不正な返却structで不健全になったinstanceは`RuntimeRegistry`の解決対象から外れます。

## batchとエラー

`process_batch(NativeBatchKind, batch_id)`はsize/version付きメタデータを一括送信します。batch IDはboolを除くunsigned 64-bit整数です。正常な返却値は`NativeBatchResult`へ変換します。C ABIの非ゼロstatusは`NativeRuntimeError`になり、`operation`と生の`status_code`を保持します。未定義statusも失いません。

現時点のNative Runtimeは全6種類のbatchを`NOT_IMPLEMENTED`として明示的に拒否します。queryはSAFETYのみを広告し、capture/inputを利用可能とは扱いません。batchの戻り値がsize/version/ID/status/reservedの契約と異なる場合は`NativeContractError`を発生させ、不健全として以降のbatchを止めます。

この接続層は実キャプチャ・画素buffer・zero-copy・buffer所有権を実装しません。PythonのSafetyGuard用既存ABIとも別のlifecycle接続です。

## 検証

`tests/test_native_runtime.py`はfake ABIによる失敗経路、返却値検査、shutdown再試行、batchとshutdownの直列化、および実共有ライブラリによるlifecycleを検証します。`KADOKA_NATIVE_RUNTIME`指定時は実ライブラリ検証を必ず実行し、ロード失敗をskipしません。未指定の場合のみ実ライブラリの2テストをskipします。`safety-ci`でビルドしたLinux/Windowsライブラリを指定して実行します。テストは実入力やゲーム操作を発生させません。
