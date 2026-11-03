# Windows配布bundle説明書（Issue #149、#515）

この文書は、`tools/build_windows_bundle.ps1`が作るWindows用配布物（以下、bundle）の配置と、bundleの起動・同梱CAを確かめる検査を説明します。検査で確認できる範囲と、確認できない範囲を区別して記録します。

## bundleの配置

`tools/build_windows_bundle.ps1`は、`build/app-bundle/dist/Kadoka/`に次の配置を作ります。PyInstallerを実行する前に、前回のbuildで作った`pyinstaller-work`・`dist`・`spec`の各directoryを削除します（Issue #515）。前回の成果物に残ったファイル（古い`cacert.pem`など）が、新しいbundleへ混ざらないようにするためです。Native buildのdirectory（`native`）は削除しません。PyInstallerの`--clean`は、PyInstallerのcache（`%LOCALAPPDATA%\pyinstaller`）も削除します。bundleの形式はonedirだけで、onefileのexeは作りません。

| path | 内容 | 配置の理由 |
|---|---|---|
| `Kadoka.exe` | 利用者が起動する正式な入口（PyInstaller onedir、GUI版） | 利用者はPythonを導入せずにこのexeを起動します |
| `kadoka_native_runtime.dll` | C++ Native Runtime | `runtime/native.py`の`discover_native_runtime`と`safety_guard.py`の`NativeSafetyValidator.try_load`は、`sys.executable`（`Kadoka.exe`）と同じdirectoryを探します |
| `_internal/python3XX.dll` ほか | PyInstallerが集めたPython runtime、拡張module（`.pyd`）、Tcl/Tk、OpenSSL、VC runtime（`vcruntime140.dll`） | PyInstallerの起動部（`SetDllDirectoryW`をimportしています）が`_internal`をDLL探索先へ登録します |
| `_internal/ai_game_player/assets/` | GUIの同梱資源 | `--add-data`で同梱します |

Native DLLはMSVC runtimeを静的にリンクします（`-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded`、`/MT`）。動的リンク（`/MD`）のNative DLLは`MSVCP140.dll`を必要としますが、PyInstallerはこのDLLを集めません。この状態では、VC++再頒布パッケージが入っていないWindowsでNative DLLの読み込みが失敗し、アプリはNative Runtimeなしで起動します。静的リンクは、bundleの組み立てに限って指定します。開発用・CI用のNative buildの設定は変えていません。Native RuntimeのC ABIはCRTが確保したmemoryを返さない（`runtime_api.h`）ため、静的リンクしてもmemoryの解放先は混ざりません。

## 検査1: 静的な配置とDLL依存の検査（`layout`）

```powershell
python tools/windows_bundle_check.py layout build/app-bundle/dist/Kadoka
```

`tools/build_windows_bundle.ps1`はbundleを作った後にこの検査を実行し、不合格ならbuildを失敗させます。検査内容は次のとおりです。

- `Kadoka.exe`と`kadoka_native_runtime.dll`がbundle直下にあること。`_internal`に`python3*.dll`が1つだけあること。同梱資源が空でないこと。
- bundle内の全`.exe`/`.dll`/`.pyd`のPE import表（通常の表と遅延読込の表）を読み、全てx64であること。
- importされる各DLLが、次のどちらかに当たること。
  - bundle内の探索先（そのmodule自身のdirectory、bundle直下、`_internal`）にある。
  - Windows自体の部品である。API Set（`api-ms-win-*`、`ext-ms-win-*`）と、`tools/windows_bundle_check.py`の`OS_PROVIDED_DLLS`に列挙したDLLだけを部品として扱います。
- VC++ runtime（`msvcp140*.dll`、`vcruntime140*.dll`など）はWindowsの部品として扱いません。GitHubの`windows-latest` runnerや多くの開発機は、これらのDLLを`System32`に持っています。そのため、runnerでの起動成功ではDLLの同梱漏れを検出できません。この検査は、build機の`System32`を参照せずに判定します。

`OS_PROVIDED_DLLS`へDLL名を追加するのは、そのDLLがWindows 10/11 x64の標準構成に含まれることを確認した場合だけです。再頒布が必要なDLLは、一覧へ追加せずbundleへ同梱します。

## 検査2: 移動後・最小環境での起動（`launch`）

```powershell
python tools/windows_bundle_check.py launch build/app-bundle/dist/Kadoka --workdir <新しい空のdirectory>
```

1. bundleを`<workdir>/Kadoka 配布確認/`へ複製します（空白と日本語を含むpathへの移動）。
2. 次の最小の環境変数だけで、複製した`Kadoka.exe`を起動します。
   - `PATH`はWindowsのdirectory（`System32`、`Windows`、`System32\Wbem`）だけ。
   - `PYTHONPATH`、`PYTHONHOME`などの`PYTHON*`と、`KADOKA_NATIVE_RUNTIME`などの`KADOKA_*`は渡しません。
   - `USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、`TEMP`、`TMP`は`<workdir>/profile`配下の新しいdirectoryにします。
   - 作業directoryは`<workdir>/cwd`（リポジトリの外の空directory）です。
3. `Kadoka.exe --bundle-self-check <report>`を実行します。exe自身が次の項目を検査し、JSON reportを書きます（GUI版exeには標準出力がないため）。1項目でも不合格なら終了code 1です。

   | 項目 | 合格条件 |
   |---|---|
   | `frozen` | PyInstallerで凍結されたprocessである |
   | `import_paths` | `sys.path`の全要素と`ai_game_player`packageがbundle内にある（開発treeのsourceを読んでいない） |
   | `native_runtime` | 通常の探索手順でbundle直下のNative DLLがABI互換として読める |
   | `native_safety` | 安全検証の補助経路が同じbundle直下のDLLを読める |
   | `assets` | 同梱資源がbundle内にあり空でない |
   | `tk` | Tcl/Tkを読み込み、Tk rootを作成・破棄できる |
   | `ssl` | 同梱OpenSSLで既定のTLS contextを作れる（通信はしない） |
   | `trust` | 凍結した`_ssl`の既定TLS contextが信頼するCA証明書の一覧（DERのbase64）、OpenSSLの既定のCA file・directory、`SSL_CERT_FILE`・`SSL_CERT_DIR`、`certifi`・`pip`・`truststore`がbundleに入っているかを、reportの`trust`へ記録する。`certifi`が入っている場合は、`certifi.where()`がbundle内でなければ不合格（Issue #515） |

4. reportの`trust`を、`tools/trust_inventory.py`で照合します。既定TLS contextのCA一覧と、このhostに存在するOpenSSLの既定のCA file・directoryの各ファイルに、インシデント（GHSA-6q48-6c7w-9gw2）の対象証明書があれば不合格です。読めないファイルや不正なreportも不合格です。複製したbundleにも検査3を実行します。
5. 続けて`Kadoka.exe --smoke-test`（Application画面の構築と破棄）を同じ最小環境で実行します。

CIの`Windows application bundle + startup smoke` job（`.github/workflows/language-ci.yml`）は、build直後に検査1と検査3、build済みbundleに検査2を実行します。

## 検査3: 同梱CAの検査（`trust`、Issue #515）

```powershell
python tools/windows_bundle_check.py trust build/app-bundle/dist/Kadoka
```

`tools/build_windows_bundle.ps1`は検査1の後にこの検査を実行し、不合格ならbuildを失敗させます。検査内容は次のとおりです。

- bundle内の全ての通常ファイルを読み、証明書の拡張子（`.pem`、`.crt`、`.cer`、`.cert`、`.der`、`.p7b`、`.p7c`、`.pfx`、`.p12`）か、PEMの証明書・PKCS7の開始行（`-----BEGIN CERTIFICATE-----`など）を持つファイルを、CA資料として扱います。開始行は名前に関係なく探すため、名前を変えた`cacert.pem`も見つけます。
- zip形式のファイル（`_internal/base_library.zip`と、Tcl 9の`tcl90.dll`・`tcl9tk90.dll`の末尾に付いたzip）は、各memberを展開して同じ基準で調べます。
- CA資料は`tools/trust_inventory.py`の`read_pem_bundle`と`scan_der_certificates`で読みます。次の場合は不合格です。
  - インシデントの対象（`incident root`または`incident-related name`）の証明書がある。
  - base64が不正、PEMの証明書が0件（DER形式の`.der`・`.cer`を含む）、終わりの行が無い`BEGIN CERTIFICATE`がある、`CERTIFICATE`以外のPEMの種類（`TRUSTED CERTIFICATE`、`PKCS7`など）がある。
  - 20MBを超えるCA資料・zip member、member数20000超または展開後の合計512MB超のzip、読めないファイル、開けないzip（拡張子`.zip`・`.pyz`・`.whl`・`.egg`・`.jar`で壊れたものを含む）、symbolic link。
- 合格時は、見つかったCA資料ごとに証明書の件数とSHA-256を表示します。CA資料が無いbundleは「0 CA files carried」と表示して合格です。

検査3はbundleのdirectoryだけを読むため、過去に作ったbundleを展開したdirectoryにも実行できます。

## 確認できる範囲と、確認できない範囲

確認できること:

- bundle内のPE fileがimportするDLLは、全てbundle内にあるか、上記の基準でWindowsの部品と判定したDLLである（検査1）。
- bundleを別のpath（空白・日本語を含む）へ移動しても、開発用の`PATH`・Python・環境変数・リポジトリなしで、`Kadoka.exe`がbundle内のPython・Native DLL・資源を使って起動部品を読み込み、Application画面を構築できる（検査2）。
- bundleが同梱するCA資料にインシデントの対象証明書が無く、凍結した`_ssl`の既定TLS contextが検査したhostで信頼するCA一覧にも無い（検査3、検査2の`trust`）。結果と限界は`doc/Python証明書信頼棚卸し.md`の「配布物の信頼源（Issue #515）」に記録します。

確認できないこと（clean Windowsでの起動確認は未実施です）:

- CIの検査2は`windows-latest` runner（Windows Server）上で動きます。runnerにはVisual Studio、VC++再頒布パッケージ、Pythonなどが導入済みで、`System32`にもDLLがあります。最小環境は環境変数を絞るだけで、OSに導入済みのDLLは見えたままです。検査1はimport表に載るDLLを判定しますが、実行時に名前を指定して読むDLL（`ctypes`による読込など）は判定しません。検査1は各moduleの探索先を「そのmodule自身のdirectory・bundle直下・`_internal`」で近似します。実際のloaderは、最初にfull pathで読んだDLLのdirectoryを依存の探索先にするため、別のdirectoryにある依存の依存は近似と異なる場合があります（2026-11-04のlocal buildでは、bundle直下と`_internal`以外にあるPE fileは、Pythonがfull pathで読む`_internal/PIL/*.pyd`だけでした。実loaderでの解決は検査2で確認します）。
- 検査1は`api-ms-win-*`・`ext-ms-win-*`の名前を全てWindowsの部品として扱い、対象のWindows版がそのAPI Setを提供するかは判定しません。古いWindowsに無いAPI Setへの依存は、対象版での起動確認で見つける必要があります。
- Windows 10/11のclient版、新規の利用者profile、管理者権限のない利用者、SmartScreen・Defenderの判定、GPU・D3D11の有無による違いは確認していません。
- 実際のゲーム入力・画面取得・推論は、どちらの検査でも実行しません。

clean Windows相当の確認（VC++再頒布パッケージなどを導入していないWindows 10/11の仮想機械やWindows Sandboxで、bundleだけを複製して検査2と同じ手順を実行する）は、Issue #149の残りの作業です。

## 関連Issue

- #515: bundleに同梱されるCAと、凍結した`_ssl`の信頼源の検査（検査3、検査2の`trust`）。#514: build用Pythonの信頼性検査（`tools/trust_gate.py`、`tools/build_windows_bundle.ps1`が実行します）。
- #228・#663: Qt 6を同梱する将来のShellの配布物inventoryとLGPLv3の要件（`windeployqt`による配置、Qt DLLの差し替え）。この文書の検査は、現在のTkinter版bundleだけを対象とします。
- #234（Model・runtimeの自動取得）、#241（Local推論processの監視）、#251（Releaseの受入gate）、#230（署名）は対象外です。
