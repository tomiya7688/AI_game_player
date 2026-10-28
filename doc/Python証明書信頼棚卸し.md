# Python証明書信頼の棚卸し（Issue #513 / 親Issue #482）

この文書は、AI Game Playerの開発・CI・配布で使うPython環境が、実際にどのpip・certifi・uv・sigstoreと、どの証明書信頼源（CA bundleまたはOSの信頼ストア）を持っているかを記録します。読者は、#482の更新作業（次の子Issue）を担当する開発者と、P0の重大性を再評価する保守者です。この文書と`tools/trust_inventory.py`は確認だけを行い、パッケージの更新や設定変更はしていません。ただし確認のため、対象のPythonでpip・certifiなどのモジュールをimportします（importはパッケージのコードを実行します。`__pycache__`は書かないよう`PYTHONDONTWRITEBYTECODE=1`で起動します）。

## 確認方法（再現手順）

```powershell
python tools/trust_inventory.py --python <確認するpython.exe> --json report.json --markdown report.md
```

- `--python`は何度でも指定できます。`--python`も`--bundle`も省略すると、ツールを起動したPythonを確認します。`--bundle`でCA bundleファイルを単体で確認でき、`--bundle`だけを指定した場合はPythonを確認しません。
- CA bundleは、通常ファイルで20MB以下のものだけを読みます。確認したPythonが返したパス（`certifi.where()`など）は絶対パスの場合だけ読みます。`--bundle`で指定したパスは、ツールを起動した作業ディレクトリを基準に解決して読みます。レポートの出力先が、確認するbundleやもう一方のレポートと同じ場合は、何も書かずに終了コード2で止まります。JSONレポートでも、子プロセスの出力と環境変数の値の認証情報は伏せ字にします。読めない、PEM証明書が0件、base64が不正なbundleは「未検査（not inspected）」と表示し、「対象名の証明書が0件」とは区別します。

| 終了コード | 意味 |
|---|---|
| 0 | 指定したすべてのPythonとbundleを確認できた。eMudhra/emSignの証明書が見つかっても0（棚卸しで、合否判定ではない） |
| 1 | 1つ以上のPythonまたはbundleを確認できなかった（理由は標準エラーとレポートの`status`） |
| 2 | レポートファイルを書き込めなかった、または出力先が衝突した |

- ツールは指定されたPythonで確認スクリプトを実行します。確認スクリプトは、モジュールのimport、パッケージのメタデータ、環境変数、証明書ストアの読み取りだけを行います。`uv`や`sigstore`などの外部コマンドは実行せず、PATH上の場所とインストール済みパッケージのバージョンだけを記録します。
- ユーザーのsite-packagesと環境変数も実際の利用状態の一部なので、分離モードでは起動しません。空の一時ディレクトリから起動し、作業ディレクトリのモジュールを読み込まないようにしています。
- 記録する項目: Pythonのversionと実行ファイル、venvかどうか、OpenSSLのversionと既定の証明書パス、pip・certifi・uv・sigstore・truststoreのversion、pipとcertifiの実際のimport元、`certifi.where()`の実ファイル、pipに同梱されたcertifiのversionと実ファイル、pip同梱のtruststoreの有無、証明書関連の環境変数（`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`、`PIP_CERT`、`PIP_USE_FEATURE`、`PIP_USE_DEPRECATED`、`PIP_CONFIG_FILE`）、Windowsの場合は`ssl.enum_certificates`が返すROOT・CA・Disallowedストアの証明書。
- eMudhra・emSignの判定: 各証明書のDERバイト列に`eMudhra`または`emSign`（ASCIIの大文字小文字を区別しない。BMPStringの名前のためUTF-16BEでも検索）が含まれるものを、SHA-256フィンガープリントとcommonName付きで列挙します。
- リポジトリの走査範囲: `.github/workflows/*.yml`・`*.yaml`、`.github/actions/**`、リポジトリ直下の`*.bat`・`*.cmd`・`*.sh`・`*.ps1`・`*.spec`、`tools/**/*.ps1`、`pyproject.toml`、`setup.cfg`、`requirements*.txt`、`requirements/**/*.txt`、`constraints*.txt`、`uv.lock`、`Dockerfile*`、`Makefile`のうち、`pip`・`certifi`・`uv`・`sigstore`・`setup-python`・`pyinstaller`・`truststore`を含む行です。URL中の認証情報とtoken類は伏せ字にします。Pythonのソースコード（`src/`・`tools/*.py`・`tests/`）は、この走査の対象外です（アプリ本体の確認は後述の節で別に行いました）。

### 判定の限界

- 今回のインシデントで信頼されなくなるべき証明書の正確なフィンガープリントは、この作業では確認できていません。ツールは名前で一致する証明書をすべて列挙するだけで、どれがインシデントの対象かは判定しません。インシデントの告知と、下のフィンガープリントを照合する必要があります。
- 名前の判定はDERのバイト列検索です。名前が別の表記や別の符号化で入っている証明書は見逃す可能性があります（偽陰性）。commonNameの抽出も簡易的な検索で、X.509の完全な解析ではありません。
- バージョンが#482の最低版を満たすことと、対象の証明書が取り除かれていることは別の事柄です。下の結果のとおり、最低版を満たしていても対象名の証明書は残っています。
- pipがHTTPS接続で同梱certifiと同梱truststore（OSの信頼ストア）のどちらを使うかは、pipの設定と起動オプションで決まります。この作業で確認したのは同梱の有無と環境変数だけで、pipの設定ファイルの内容と実際の接続は確認していません。
- Windowsの結果は、`ssl.enum_certificates`が返すROOT・CA・Disallowedストアの内容です。Windowsは必要になったルート証明書を自動で取得することがあるため、ストアに無いことは「確認時点でストアに存在しない」という意味にとどまり、Windowsがその証明書チェーンを拒否する証明ではありません。
- 実際のTLS接続で証明書チェーンを検証する確認（対象サイトへの接続試験）は行っていません。
- 確認したWindows環境は開発機1台だけです。

## 確認結果（2026-10-28）

### ローカル開発環境: Windows 10、開発者がインストールしたPython 3.14（venvではない本体）

| 項目 | 結果 |
|---|---|
| Python | 3.14.7（venvではない）、OpenSSL 3.5.7 |
| pip | 26.2.2（#482の最低版26.2.2以上）。import元はPython本体のsite-packages |
| certifi（単体） | 2026.10.23（最低版2026.10.19以上）。import元と`certifi.where()`は`%APPDATA%\Python\Python314\site-packages\certifi\cacert.pem`（ユーザーsite-packages） |
| 単体certifiのCA bundle | 118件、SHA-256 `2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b` |
| 単体certifi内のeMudhra/emSign | 1件: emSign Root CA - G1（`40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367`） |
| pip同梱certifi | 2026.10.19、pip同梱truststoreあり |
| pip同梱のCA bundle | 120件、SHA-256 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785` |
| pip同梱certifi内のeMudhra/emSign | 3件: emSign Root CA - G1（`40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367`、単体certifiの1件と同一）、emSign Root CA - C1（`125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f`）、emSign ECC Root CA - C3（`bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3`） |
| Windowsストア | ROOT 59件・CA 33件・Disallowed 0件。eMudhra/emSignは0件。PKCS#7形式など展開できなかったエントリは無し |
| 証明書関連の環境変数 | いずれも未設定 |
| uv / sigstore | 未導入（パッケージもPATH上のコマンドも無い） |

表の「最低版以上」は、#482が求めるバージョンを満たすという意味だけです。対象名の証明書が取り除かれたことは意味しません。上のとおり、最低版以上のpipとcertifiにも、emSignの名前を含む証明書が残っています。

### CI（GitHub Actions）

- すべてのworkflowは`actions/setup-python@v5`でPythonを用意し、pip・certifiのversionを固定していません。`tests.yml`と`upd-architecture-ci.yml`は`python -m pip install`を、`language-ci.yml`はPyInstallerのinstallを実行します。
- `tests.yml`の`Python quality (Ruff + syntax)` job（ubuntu）が、このツールでCIのPython環境を確認し、結果をjobのstep summaryへ書き出します。確認スクリプトが失敗した場合だけjobを失敗させ、対象名の証明書が見つかってもjobは失敗させません。
- CIの実際の値は、この文書を書いた時点ではまだ記録していません。PRのCI結果を確認したら、run URLと値をこの節へ追記します。Windowsのrunner、LinuxのOS側の信頼ストア（`ca-certificates`）は、このjobでは確認していません。

### リポジトリ内の固定・同梱

- 上の走査範囲で、pip・certifi・uv・sigstoreのversion固定はありませんでした。requirements、lock file、constraints、Dockerfileは存在しません。
- uv、sigstoreは、走査範囲のどこからも使われていません。
- 配布物: `tools/build_windows_bundle.ps1`がPyInstallerでWindows用の実行ファイルを作ります。成果物の中に同梱されたPython・OpenSSL・CA bundleは、この作業では確認していません。

## アプリ本体のTLS利用

`src/ai_game_player`を`certifi`・`requests`・`httpx`・`urllib3`・`import ssl`・`create_default_context`で検索し、該当がないことを確認しました。HTTP通信は標準ライブラリの`urllib`だけで、既定ではloopbackのOllamaへ接続します。Providerの接続先をHTTPSのリモートにした場合、`urllib`は標準の`ssl`の既定コンテキストを使います。既定コンテキストは、どのOSでもOpenSSLの既定パス（ツールの`ssl_default_verify_paths`。`SSL_CERT_FILE`・`SSL_CERT_DIR`が設定されていればその値）を読み込み、Windowsではそれに加えてOSの信頼ストアを読み込みます。これはPythonの仕様からの説明で、実際の接続では確認していません。確認したローカル環境では、これらの環境変数は未設定でした。依存パッケージ経由でcertifiが使われるかは、アプリの依存が標準ライブラリとOCR用の任意依存（Pillow、pytesseract）だけであるため対象外としました。

## 影響の整理と未確認事項

| 対象 | 確認できたこと | 未確認 |
|---|---|---|
| ローカルのpip | 同梱certifiにemSign 3件が残っている。同梱truststoreがある | pipが実際にどちらの信頼源を使うか（設定ファイル） |
| 単体certifiを直接使うクライアント | このリポジトリのコードには無い。ローカルの単体certifiにemSign Root CA - G1が残っている | 他のツールが単体certifiを使うか |
| アプリ本体 | certifiを使わず、標準`ssl`の既定コンテキストを使う | 実際のHTTPS接続での検証 |
| CI | workflowの構成 | 実際の値（本PRのCI summaryで確認予定） |
| PyInstaller配布物 | 作成方法 | 成果物内のPython・OpenSSL・CA bundle |

未確認事項（次の子Issueで扱う）:

1. 残っているemSign Root CA - G1・C1・ECC C3が、インシデントの対象かどうか。#482は「certifi 2026.10.19以上」を求めていますが、2026.10.19（pip同梱）にも3件とも含まれ、2026.10.23ではC1とC3が無くなっています。インシデントの告知とフィンガープリントを照合してください。
2. PyInstallerで作った配布物の中のPython・OpenSSL・CA bundle。配布物は利用者の手元で動くため、最も影響が大きい対象です。
3. CIのPython環境のpip・certifi。
4. pipの設定ファイルの内容と、pipが実際に使う信頼源。

## P0の重大性についての評価案

この評価案は、保守者が重大性を判断するための材料です。「OSの信頼ストアに対象名の証明書が無い」ことは、上の限界のとおり安全の根拠にしません。P0から下げられるのは、少なくとも次の条件がすべて確認できた場合です。

1. 未確認事項1で、残っている証明書がインシデントの対象外と確認できる。対象であれば、それを含むcertifiを使う環境はすべて更新対象とする。
2. 未確認事項2で、PyInstaller配布物にcertifiが同梱されていないか、同梱されていても対象の証明書を含まないことを確認できる。
3. 未確認事項3・4で、CIとローカルのpipが対象の証明書を含む信頼源を使わないことを確認できる。

どれかが確認できない間は、P0のまま扱います。
