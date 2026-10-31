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
- 記録する項目: Pythonのversionと実行ファイル、venvかどうか、OpenSSLのversionと既定の証明書パス、pip・certifi・uv・sigstore・truststoreのversion、pipとcertifiの実際のimport元、`certifi.where()`の実ファイル、pipに同梱されたcertifiのversionと実ファイル、pip同梱のtruststoreの有無、証明書関連の環境変数（`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`、`PIP_CERT`、`PIP_USE_FEATURE`、`PIP_USE_DEPRECATED`、`PIP_CONFIG_FILE`）、pipが読む設定ファイルの場所と有無、その中の証明書関連の設定（`cert`、`client-cert`、`use-deprecated`、`use-feature`）、Windowsの場合は`ssl.enum_certificates`が返すROOT・CA・Disallowedストアの証明書。
- eMudhra・emSignの判定: 各証明書のDERバイト列に`eMudhra`または`emSign`（ASCIIの大文字小文字を区別しない。BMPStringの名前のためUTF-16BEでも検索）が含まれるものを、SHA-256フィンガープリントとcommonName付きで列挙します。
- インシデントとの照合: 列挙した証明書ごとに、次の3つのどれかを表示します。各bundleとWindowsストアの行には、インシデントに該当した件数も表示します。根拠は下の「インシデント対象の特定」です。
  - `incident root`: インシデントの対象ルートの既知のフィンガープリントと一致した。
  - `incident-related name`: 発行者または主体のcommonNameが対象ルートの名前（`emSign ECC Root CA - G3`、`emSign Root TLS CA - G3`）と一致した（大文字小文字、空白の数、ハイフンとUnicodeのダッシュの違いは同一視）。別のフィンガープリントの同名ルートや、対象ルートが直接発行した中間証明書がこれに当たります。
  - `no known incident match`: どちらにも当たらない。安全という判定ではありません。証明書チェーンは解析しないため、対象の中間証明書がさらに発行した証明書や、この正規化の範囲を超える別表記の名前は認識できません。
- pipの信頼源: pipのversion、同梱truststoreの有無、`legacy-certs`の指定（環境変数と設定ファイル）から、pipがHTTPS接続で使う信頼源を導いて表示します。pipの設定を読めなかった場合と、`cert`が指定されている場合（設定ファイルの`cert`、環境変数`PIP_CERT`）は、先頭を`unknown`とし、それらの設定が無い場合の信頼源を参考として続けて表示します（`cert`の指定先の内容は解析しません）。
- リポジトリの走査範囲: `.github/workflows/*.yml`・`*.yaml`、`.github/actions/**`、リポジトリ直下の`*.bat`・`*.cmd`・`*.sh`・`*.ps1`・`*.spec`、`tools/**/*.ps1`、`pyproject.toml`、`setup.cfg`、`requirements*.txt`、`requirements/**/*.txt`、`constraints*.txt`、`uv.lock`、`Dockerfile*`、`Makefile`のうち、`pip`・`certifi`・`uv`・`sigstore`・`setup-python`・`pyinstaller`・`truststore`を含む行です。URL中の認証情報とtoken類は伏せ字にします。Pythonのソースコード（`src/`・`tools/*.py`・`tests/`）は、この走査の対象外です（アプリ本体の確認は後述の節で別に行いました）。

### 判定の限界

- インシデント対象の判定は、下の公開情報（2026-10-31時点）に基づきます。告知が更新されて対象が増えた場合は、`tools/trust_inventory.py`の`INCIDENT_ROOT_FINGERPRINTS`と`INCIDENT_ROOT_NAMES`を更新する必要があります。アドバイザリが名指ししているルートは`emSign ECC Root CA - G3`です。`emSign Root TLS CA - G3`は、certifiの保守者がissue #439で影響を受けるルートとして挙げたため、予防的に名前だけで照合します。certifiに含まれたことがなく、フィンガープリントは確認していません。
- 名前の判定はDERのバイト列検索です。名前が別の表記や別の符号化で入っている証明書は見逃す可能性があります（偽陰性）。commonNameの抽出も簡易的な検索で、X.509の完全な解析ではありません。
- バージョンが#482の最低版を満たすことと、対象の証明書が取り除かれていることは別の事柄です。下の結果のとおり、最低版を満たしていても対象名の証明書は残っています。
- pipの信頼源は、設定ファイルと環境変数から導いたものです。pipを起動するときのコマンドラインオプション（`--cert`、`--use-deprecated`）は確認スクリプトからは見えません。また、実際のHTTPS接続では確認していません。
- Windowsの結果は、`ssl.enum_certificates`が返すROOT・CA・Disallowedストアの内容です。Windowsは必要になったルート証明書を自動で取得することがあるため、ストアに無いことは「確認時点でストアに存在しない」という意味にとどまり、Windowsがその証明書チェーンを拒否する証明ではありません。
- 実際のTLS接続で証明書チェーンを検証する確認（対象サイトへの接続試験）は行っていません。
- 確認したWindows環境は開発機1台だけです。

## インシデント対象の特定（2026-10-31）

#482のインシデントは、GitHub Security Advisory [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)（深刻度critical、2026-10-20公開）です。告知の要点は次のとおりです。

- `emSign ECC Root CA - G3`の下の中間証明書8件の秘密鍵が漏えいし、eMudhraが失効させた。
- certifi 2026.10.18はemSignのルートをすべて削除し、2026.10.19は影響のないemSignのルートを戻した。2026.10.19以降へ更新する。

certifiの公開wheelから`certifi/cacert.pem`を取り出し、ツールの判定で比較しました。

| certifi | 証明書数 | emSign・eMudhraの証明書 |
|---|---|---|
| 2026.7.22 | 121 | G1、C1、ECC C3、ECC G3（`86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`）の4件 |
| 2026.10.18 | 117 | なし（4件とも削除） |
| 2026.10.19 | 120 | G1、C1、ECC C3の3件（ECC G3は戻されていない） |
| 2026.10.23 | 118 | G1の1件（C1とECC C3が削除） |

経緯はcertifiのissue [#439](https://github.com/certifi/python-certifi/issues/439)とPR [#440](https://github.com/certifi/python-certifi/pull/440)に記録されています。

- 2026.10.18で全emSignルートを削除した。中間証明書だけを無効にする仕組みがcertifiに無いためです。
- 2026.10.19で、影響のないG1・C1・ECC C3を戻した（PR #440）。アドバイザリにも、2026.10.19で影響のないemSignのルートを戻したと書かれています。
- 2026.10.23でC1とECC C3が削除された。PR #440のコメントによれば、eMudhra自身がMozillaへ削除を依頼したもので（Mozilla bug 2083709）、Mozillaの信頼ストアとの通常の同期です。インシデントとは別の理由です。Mozilla bug 2083709の本文は、この作業では直接確認していません。

結論:

- インシデントの対象ルートは`emSign ECC Root CA - G3`です（certifiの中ではフィンガープリント`86a1ecba…be6b`の1件）。
- 前回の確認で残っていたG1・C1・ECC C3は、インシデントの対象外です。
- 2026.10.18より前のcertifiは対象ルートを含むため、更新が必要です。

この比較は、`tools/trust_inventory.py --bundle`で再現できます。2026.7.22のbundleでは対象ルートが`incident root`と表示され、incidentの件数は1件です。

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

表の「最低版以上」は、#482が求めるバージョンを満たすという意味だけです。最低版以上のpipとcertifiにもemSignの名前を含む証明書が残っていますが、上の「インシデント対象の特定」のとおり、残っている3件はどれもインシデントの対象外です。

### ローカル開発環境の再確認（2026-10-31）

インシデント照合とpipの信頼源の判定を加えたツールで、同じPythonをもう一度確認しました。

- pip・certifiのversionとbundleのSHA-256は、2026-10-28と同じでした。
- 単体certifi（G1の1件）、pip同梱certifi（G1・C1・ECC C3の3件）とも、判定はすべて`no known incident match`でした。incidentの件数はどちらも0件です。
- Windowsストアは、ROOT 61件・CA 33件・Disallowed 0件でした（ROOTは2026-10-28の59件から増えています。Windowsの自動取得によるものと考えられますが、確認はしていません）。eMudhra/emSignの証明書は0件でした。
- pipの設定ファイル: 次の4か所とも存在しませんでした。証明書関連の環境変数も未設定です。
  - global: `C:\ProgramData\pip\pip.ini`
  - user: `%USERPROFILE%\pip\pip.ini`
  - user: `%APPDATA%\pip\pip.ini`
  - site: Python本体直下の`pip.ini`
- pipの信頼源: OSの信頼ストア（truststore経由）と、pip同梱certifiのbundleです。
  - pip 26.2.2のソース（`pip/_internal/cli/index_command.py`）では、`legacy-certs`が指定されない限り、truststoreの`SSLContext`を作り、そこへpip同梱certifiの`where()`を読み込みます。
  - 同梱truststoreのWindows実装（`pip/_vendor/truststore/_windows.py`）は、まずWindowsの信頼ストアで検証し、失敗したときだけ、読み込んだcertifiのbundleを信頼点として検証し直します。
  - つまり、pipはWindowsストアとpip同梱certifiのどちらかで検証できれば接続します。pip同梱certifiに対象ルートはありません。Windowsストアには確認時点で対象ルートがありませんでしたが、Windowsはルートを自動で取得することがあるため、これはWindowsが対象のチェーンを拒否する証明ではありません。MicrosoftがECC G3や漏えいした中間証明書をどう扱っているかは確認していません。
  - Linux（CI）では、pip同梱truststore 0.10.4の`_openssl.py`が、OpenSSLのコンテキストに既定の証明書パス（`set_default_verify_paths`と既知のCA bundleの候補）を読み込みます。pipは同じコンテキストへpip同梱certifiも読み込むため、どちらかの信頼点に届けば検証が通る点はWindowsと同じです。これはソースを読んだ理解で、Linuxの実際の接続では確認していません。

### CI（GitHub Actions）

- すべてのworkflowは`actions/setup-python@v5`でPythonを用意し、pip・certifiのversionを固定していません。`tests.yml`と`upd-architecture-ci.yml`は`python -m pip install`を、`language-ci.yml`はPyInstallerのinstallを実行します。
- `tests.yml`の`Python quality (Ruff + syntax)` job（ubuntu）が、このツールでCIのPython環境を確認し、結果をjobのstep summaryへ書き出します。確認スクリプトが失敗した場合だけjobを失敗させ、対象名の証明書が見つかってもjobは失敗させません。
- CIの実際の値は、PR #594の時点では記録していませんでした。GitHubのAPIではstep summaryを読めないため、このjobは同じ内容をjobのログにも出力するようにしました（2026-10-31）。値はこの節の下に追記します。
- Windowsのrunnerは、このjobでは確認していません。LinuxのOS側の信頼ストアは、2026-10-31から`/etc/ssl/certs/ca-certificates.crt`を`--bundle`で同じjobが検査します。

#### CIの値（2026-10-31、PR #617、run 41196398517、ubuntu-latest、HEAD 8dab35d）

| 項目 | 結果 |
|---|---|
| Python | 3.14.9（`actions/setup-python`のtoolcache、venvではない）、OpenSSL 3.0.13 |
| pip | 26.2.2（最低版以上） |
| certifi（単体） | 未導入（`certifi.where()`なし） |
| pip同梱certifi | 2026.10.19、120件、SHA-256 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785`（ローカルと同一） |
| pip同梱certifi内のeMudhra/emSign | G1・C1・ECC C3の3件、すべて`no known incident match`。incidentは0件 |
| pipの設定ファイル | `/etc/pip.conf`だけが存在し、証明書関連の設定は無し。他の4か所（`/etc/xdg/pip/pip.conf`、`~/.pip/pip.conf`、`~/.config/pip/pip.conf`、Python直下の`pip.conf`）は存在しない |
| pipの信頼源 | OSの信頼ストア（truststore経由）と、pip同梱certifi |
| 証明書関連の環境変数 | いずれも未設定 |
| uv / sigstore | 未導入 |

OSのCA bundle（`/etc/ssl/certs/ca-certificates.crt`）の値: この節の変更を含むCI実行の後に追記します。

### リポジトリ内の固定・同梱

- 上の走査範囲で、pip・certifi・uv・sigstoreのversion固定はありませんでした。requirements、lock file、constraints、Dockerfileは存在しません。
- uv、sigstoreは、走査範囲のどこからも使われていません。
- 配布物: `tools/build_windows_bundle.ps1`がPyInstallerでWindows用の実行ファイルを作ります。成果物の中に同梱されたPython・OpenSSL・CA bundleは、この作業では確認していません。

## アプリ本体のTLS利用

`src/ai_game_player`を`certifi`・`requests`・`httpx`・`urllib3`・`import ssl`・`create_default_context`で検索し、該当がないことを確認しました。HTTP通信は標準ライブラリの`urllib`だけで、既定ではloopbackのOllamaへ接続します。Providerの接続先をHTTPSのリモートにした場合、`urllib`は標準の`ssl`の既定コンテキストを使います。既定コンテキストは、どのOSでもOpenSSLの既定パス（ツールの`ssl_default_verify_paths`。`SSL_CERT_FILE`・`SSL_CERT_DIR`が設定されていればその値）を読み込み、Windowsではそれに加えてOSの信頼ストアを読み込みます。これはPythonの仕様からの説明で、実際の接続では確認していません。確認したローカル環境では、これらの環境変数は未設定でした。依存パッケージ経由でcertifiが使われるかは、アプリの依存が標準ライブラリとOCR用の任意依存（Pillow、pytesseract）だけであるため対象外としました。

## 影響の整理と未確認事項

| 対象 | 確認できたこと | 未確認 |
|---|---|---|
| ローカルのpip | 信頼源はWindowsストアとpip同梱certifi（2026.10.19）。pip同梱certifiに対象ルートは無い。Windowsストアは確認時点で未検出。設定ファイルは無い | Windowsの自動取得とMicrosoft側の扱い、実際のHTTPS接続 |
| 単体certifiを直接使うクライアント | このリポジトリのコードには無い。ローカルの単体certifi（2026.10.23）にインシデントの対象ルートは無い | 他のツールが単体certifiを使うか |
| アプリ本体 | certifiを使わず、標準`ssl`の既定コンテキストを使う | 実際のHTTPS接続での検証 |
| CI（ubuntu、quality job） | 上の「CI（GitHub Actions）」の節を参照 | Windowsのrunner、Linuxの`ca-certificates`の中身 |
| PyInstaller配布物 | 作成方法 | 成果物内のPython・OpenSSL・CA bundle（#515） |

残る未確認事項:

1. PyInstallerで作った配布物の中のPython・OpenSSL・CA bundle。配布物は利用者の手元で動くため、最も影響が大きい対象です。#515で、クリーンビルドした成果物を確認します。
2. Windowsのrunnerと、Linuxの`ca-certificates`の中身。
3. 実際のHTTPS接続での検証。

## P0の重大性についての評価案

この評価案は、保守者が重大性を判断するための材料です。「OSの信頼ストアに対象名の証明書が無い」ことは、上の限界のとおり安全の根拠にしません。

前回の評価案では、P0から下げる条件を3つ挙げていました。2026-10-31の確認で、それぞれ次の状態です。

1. 残っていた証明書がインシデントの対象かどうか: **確認済み**。G1・C1・ECC C3は対象外で、対象ルート（ECC G3）はローカルのどのbundleにもありません。Windowsストアでは確認時点で未検出ですが、上の理由で安全の根拠にはしません。
2. PyInstaller配布物: **未確認**（#515）。
3. CIとローカルのpipの信頼源: ローカルは、pip同梱certifiについて**確認済み**、Windowsストア側は**確認時点で未検出**です。CIは、上の「CI（GitHub Actions）」の節の値で判断します。

条件2が未確認なので、#482全体はP0のまま扱います。本Issue（#513）の棚卸しで見つかったbundleには、更新が必要なものはありません（CIのubuntu環境を含む）。ただし、2026.10.18より前のcertifiを含む環境が見つかった場合は、更新の対象です。
