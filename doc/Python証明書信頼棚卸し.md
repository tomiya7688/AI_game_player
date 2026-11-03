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
- pipの信頼源: pipのversion、同梱truststoreの有無、`legacy-certs`の指定（環境変数`PIP_USE_DEPRECATED`があればそれ、無ければ設定ファイルの`global`節。pipと同じく環境変数が優先）から、pipがHTTPS接続で使う信頼源を導いて表示します。pipの設定を読めなかった場合と、`cert`が指定されている場合（設定ファイルの`global`節の`cert`、環境変数`PIP_CERT`）、特定のコマンドだけに効く設定（`install`節の`cert`・`use-deprecated`など）がある場合、CA bundleを変える環境変数（`SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE`）が設定されている場合は、先頭を`unknown`とし、それらの設定が無い場合の信頼源を参考として続けて表示します（`cert`の指定先の内容は解析しません）。
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
| pipの設定ファイル | `/etc/pip.conf`だけが存在し、証明書関連の設定は無し（設定の読み取りを直したツールで、run 41199602615・HEAD 99b85f2でも同じ結果）。他の4か所（`/etc/xdg/pip/pip.conf`、`~/.pip/pip.conf`、`~/.config/pip/pip.conf`、Python直下の`pip.conf`）は存在しない |
| pipの信頼源 | OSの信頼ストア（truststore経由）と、pip同梱certifi |
| 証明書関連の環境変数 | いずれも未設定 |
| uv / sigstore | 未導入 |

runnerのOSのCA bundle（run 41196982172、HEAD 0f5a0d5）:

| 項目 | 結果 |
|---|---|
| `/etc/ssl/certs/ca-certificates.crt` | 120件、SHA-256 `e05a523d99de77c6b1f2c7b7c3af502c8378f29e6e8484358ec55f96b0972ad6` |
| eMudhra/emSign | G1・C1・ECC C3の3件、すべて`no known incident match`。incidentは0件（ECC G3は無い） |

CIでは、pipが使う2つの信頼源（OSのCA bundleとpip同梱certifi）のどちらにも、インシデントの対象ルートはありませんでした。

### リポジトリ内の固定・同梱

- 上の走査範囲で、pip・certifi・uv・sigstoreのversion固定はありませんでした。requirements、lock file、constraints、Dockerfileは存在しません（2026-10-31時点。#514で最低版のconstraintsを追加しました。下の「固定版の検証」を参照）。
- uv、sigstoreは、走査範囲のどこからも使われていません。
- 配布物: `tools/build_windows_bundle.ps1`がPyInstallerでWindows用の実行ファイルを作ります。成果物の中のPython・OpenSSL・CA bundleは、下の「配布物の信頼源（Issue #515）」で確認しました。

## 固定版の検証（Issue #514）

#514では、#482の最低版をリポジトリの1ファイルに固定し、CI・build script・bootstrap scriptが、pipを使う前にその最低版と信頼源を検査するようにしました。

### 最低版の固定

- `requirements/python-trust-constraints.txt`に、pip `>=26.2.2`、certifi `>=2026.10.19`、uv `>=0.13.4`、sigstore `>=4.5.1`を書きました。pipのconstraintsファイル（`-c`、環境変数`PIP_CONSTRAINT`）として使います。constraintsは、そのパッケージがinstallされる場合だけ効き、パッケージを追加しません。
- uvとsigstoreは、#513の走査範囲のどこからも使われていません（上の「リポジトリ内の固定・同梱」）。将来installされた場合に最低版を下回らないよう、constraintsにだけ残しています。
- このファイルはASCIIだけで書きます。pipはrequirements・constraintsファイルを、BOMやcoding宣言がなければロケールの文字コードで読むためです（WindowsのCP932で日本語を読めない）。
- pyproject.tomlの依存には、pip・certifiも、それらを引き込むパッケージもありません。lock fileもありません。そのため、変更したのはconstraintsとそれを使う手順だけです。

### 検査ツール: `tools/trust_gate.py`

`tools/trust_inventory.py`と同じ確認（読み取りだけ）を行い、次のどれかに当たると終了コード1で失敗します。

- pipまたはpip同梱certifiが最低版未満、pre-releaseなどで判定できない、またはpipが無い。
- 単体certifi・uv・sigstoreがinstallされていて最低版未満。単体certifiは、配布metadataが無くimportだけできる場合もversion不明として失敗にし、そのbundleも検査します。
- pipの`trusted-host`（環境変数`PIP_TRUSTED_HOST`、または設定ファイルの`trusted-host`）が設定されている。pipはそのホストへの接続で証明書を検証しないためです（`tools/trust_inventory.py`もこの設定を記録するようにしました）。uv・sigstoreのコマンドがPATH上にだけあり、パッケージのversionが無い（コマンドは実行しないため、versionを確認できない）。
- 確認したCA bundle（単体certifi、pip同梱certifi、`--bundle`で指定したファイル）か、WindowsのDisallowed以外のストア（ROOT・CA、想定外の名前のストアも含む）に、`incident root`または`incident-related name`の証明書がある。WindowsのDisallowedストアは不信頼の一覧なので、そこにある対象証明書は失敗にしません。
- 確認できなかったもの（Pythonの確認スクリプトの失敗、読めないbundle、WindowsでDisallowed以外のストアの展開できなかったエントリ、Windowsでストアの一覧が無い、pipの信頼源が無い・空・`unknown`）。actionは、LinuxでOSのCA bundleが無い場合も失敗にします。確認できないものは合格にしません。

| 終了コード | 意味 |
|---|---|
| 0 | 指定したすべてのPythonとbundleが条件を満たした |
| 1 | 1つ以上のPythonまたはbundleが条件を満たさない、または確認できなかった |
| 2 | constraintsファイルを読めない、`名前>=version`以外の行がある、または必要な最低版が無い |

`--python`を省略すると、ツールを起動したPythonを確認します。各Pythonについて、pipとpip同梱certifiのversion、pip同梱bundleのSHA-256、pipの信頼源を1行で表示します。合格は、上の「判定の限界」の範囲での合格です。証明書チェーンの解析や実際のTLS接続はしていません。

### 適用した場所

| 場所 | 内容 |
|---|---|
| `.github/actions/python-trust-floor` | 後続のpip installへ`PIP_CONSTRAINT`を設定する。Linuxでは、OSの信頼ストア（ca-certificates）で`mozilla/emSign_ECC_Root_CA_-_G3.crt`が有効なら`/etc/ca-certificates.conf`で無効にし、`update-ca-certificates --fresh`で作り直す。次に、既存のpipがgateに合格しない場合だけ、pipを最低版以上へ更新する（更新では既存のpipがネットワークから取得するため、合格している場合は更新しない）。その後`tools/trust_gate.py`を実行する。Linuxでは`/etc/ssl/certs/ca-certificates.crt`と、`/etc/ssl/certs/*.pem`を連結したファイルも検査する（truststoreはOpenSSLの既定のファイルとディレクトリの両方を読むため） |
| `tests.yml`（ubuntu） | `python-quality`と`python-coverage`で、最初のpip installの前に上のactionを使う。`python-quality`は、更新前の状態を`tools/trust_inventory.py`で記録してからactionを実行する（更新前後の検査）。テストゲーム用のvenvは、ensurepipが入れるpipが最低版より古い場合があるため、検査済みのホストのpip（`--python`）でvenvのpipを更新し、そのvenvのPythonを検査してから、constraints付きで依存を入れる |
| `upd-architecture-ci.yml`（ubuntu） | `upd-checker`で、git+httpsのinstallの前にactionを使う |
| `language-ci.yml`（ubuntu、windows） | `contract-assets`（ubuntu）、`windows-app-bundle`・`windows-e2e`・`windows-planning-comparison`（windows）で、最初のpip installの前にactionを使う。windowsではROOT・CA・Disallowedストアも検査する |
| `tools/test_games/launch.ps1` | `-Setup`で、依存をダウンロードするホストのpipを検査し、constraintsを付けてinstallする。検査に失敗したら、pipの更新コマンドを表示して止まる |
| `tools/build_windows_bundle.ps1` | PyInstallerを実行する前に、build用のPythonを検査する。成果物の中のCA bundleは、build後に`tools/windows_bundle_check.py trust`で検査する（#515） |

pip installを実行しないCI job（`bug-ci`、`safety-ci`、`fail-safe-ci`、`performance-ci`、`tests.yml`の`unit-tests`など）は、pipの信頼源を使わないため対象にしていません。`tests/test_trust_gate.py`が、workflowの各jobでpip installがactionの後にあること（またはその行がconstraintsを指定していること）を検査します。

### 既存環境の更新・再構築の手順

```powershell
# 1. pipを最低版以上へ更新する（単体certifiがあれば同じconstraintsで更新する）
python -m pip install --upgrade -c requirements/python-trust-constraints.txt pip
python -m pip install --upgrade -c requirements/python-trust-constraints.txt certifi  # 単体certifiを使っている場合だけ
# 2. 更新後の信頼源を検査する（終了コード0なら条件を満たす）
python tools/trust_gate.py
```

- venvは、venvのPythonで1を実行し、`python tools/trust_gate.py --python <venvのpython>`で検査します。古いpipのまま作られたvenvを残す理由がなければ、削除して作り直してから同じ手順を実行してください。
- Windowsのテストゲーム環境（`build/test-games-venv`）はpipを持たず、ホストのpipが依存を入れます。ホストのPythonで1と2を実行してから、`run_test_games.bat -Setup`を実行してください。Linuxの手順は`tools/test_games/README.md`にあります。
- 2で失敗した場合は、表示された理由を確認してください。`incident`の行は、そのbundleかストアに対象ルートが残っていることを示します。pipの更新だけでは直らないため、そのPython・OSの信頼ストアを更新してから、もう一度検査してください。

### CIで見つかった対象ルート（2026-11-03）

PR #715の最初のCIで、`tests.yml`の`python-coverage` job（ubuntu-latest、Python 3.14.8、pip 26.3、pip同梱certifi 2026.10.23）のOSのCA bundle `/etc/ssl/certs/ca-certificates.crt`（SHA-256 `ecd9dc38bc3efb7dbd6431f57e29d2f8d6a0f0d211e1464b3fef2cbfe266fcd2`）に、対象ルート`emSign ECC Root CA - G3`（`86a1ecba…be6b`）が含まれていました。gateはこのjobを失敗にしました。同じrunの`python-quality` jobと、#513の確認（run 41196982172、SHA-256 `e05a523d…6ad6`）のbundleには含まれていませんでした。runnerのimageによってOSのCA bundleが異なります。

pip 24.2以降はLinuxでもOSのCA bundleを信頼点に加えるため、このrunnerのpipは対象ルートを信頼していたことになります（pip同梱certifiに対象ルートが無くても、truststore側で検証が通る）。そのため、上のactionでOSの信頼ストアから対象ルートを無効にしてから検査するようにしました。
2回目のCI（HEAD 5242010、2026-11-03）では、gateを使う全jobが合格しました。ただし、このrunで割り当てられたubuntuのrunnerはすべて、対象ルートを含まないbundle（SHA-256 `e05a523d…6ad6`、`/etc/ssl/certs/*.pem`の連結は`d9ed0127…40fe`）でした。対象ルートを含むimageで無効化の手順が働くことは、まだ確認していません（そのimageでは、無効化できなければgateが失敗にします）。

| job（runner） | Python / pip / pip同梱certifi | 結果 |
|---|---|---|
| python-coverage・python-quality・upd-checker・contract-assets（ubuntu-latest） | 3.14.9 / 26.3 / 2026.10.23（SHA-256 `2ef52ad5…bd2b`） | 合格。OSのbundleとディレクトリの両方にincidentなし |
| windows-app-bundle・windows-planning-comparison（windows-latest） | 3.14.10 / 26.3 / 2026.10.23 | 合格。WindowsのROOT・CAストアにincidentなし |

setup-pythonが入れたpipは最初から26.3で最低版以上のため、更新はしていません。

### この節の限界

- `--upgrade`は、最低版以上の最新のpipを入れます。特定のversionへの固定ではありません。
- 既存のpipが最低版未満で更新が必要な場合、その更新のダウンロードは、検査に合格していない既存のpipが行います。Linuxでは、その前にOSの信頼ストアから対象ルートを無効にしています。
- `upd-checker`のgit+httpsのinstallでは、リポジトリの取得をgitが行います。gitが使う信頼源（OpenSSLまたはWindowsの信頼ストア）は、このツールでは確認していません。
- `PIP_CONSTRAINT`がpipのbuild isolation環境へ渡るかは、pipのversionで変わります。このリポジトリのbuild依存（setuptoolsなど）はcertifiを含まないため、確認していません。
- OSの信頼ストアから無効にするのは、ca-certificatesの上のファイル名の1件だけです。別の名前で入っていた場合は無効にできず、gateがフィンガープリントで失敗にします。WindowsのrunnerとWindowsの開発機の信頼ストアは変更しません（gateの検査だけ）。
- 配布物（PyInstallerの成果物）の中のCA bundleは、次の節（#515）で扱います。

## 配布物の信頼源（Issue #515）

配布物（`tools/build_windows_bundle.ps1`が作るPyInstallerのonedir bundle）が、実際にどのCAを同梱し、どのCAを信頼するかを確認しました。検査の手順は`doc/Windows配布bundle説明書.md`の検査2（`trust`項目）と検査3です。

### 凍結したアプリの信頼源

凍結したアプリのTLS接続は、標準の`ssl.create_default_context()`だけを使います（下の「アプリ本体のTLS利用」）。その信頼源は次のとおりで、それぞれ次の方法で確認します。

| 信頼源 | 確認方法 | 判定 |
|---|---|---|
| bundleが同梱するCA資料（PyInstallerが集めた`certifi/cacert.pem`、pip同梱certifiの`cacert.pem`、名前を変えたPEM、zip内のPEM） | 検査3がbundleの全ファイルとzip memberを読む | 対象証明書、または読めないCA資料があれば失敗 |
| `certifi`・`pip`・`truststore`のmoduleがbundleに入っているか | 凍結したexe内で`importlib.util.find_spec`を実行する（検査2の`trust`項目） | 記録する。`certifi`があれば`certifi.where()`がbundle内であることを要求 |
| 凍結した`_ssl`の既定TLS contextが読み込んだCA一覧 | 凍結したexe内で`get_ca_certs(binary_form=True)`を記録し、`tools/trust_inventory.py`で照合する | 対象証明書があれば失敗 |
| WindowsのCA・ROOTストア（凍結した`_ssl`が`load_default_certs`で読む） | 凍結したexe内で`ssl.enum_certificates`の`x509_asn`の項目を記録して照合する。`get_ca_certs`はCAでない証明書を返さないため、別に記録する | 対象証明書があれば失敗。Windowsで一覧が無ければ失敗 |
| OpenSSLの既定のCA file・directory（`ssl.get_default_verify_paths`） | path・環境変数名を記録する。このhostに存在すれば、各ファイルを読んで照合する | 対象証明書、または読めないファイルがあれば失敗 |

### 確認結果（2026-11-04、ローカルのクリーンbuild）

- 環境: Windows 10、Python 3.14.7（OpenSSL 3.5.7）、PyInstaller 6.22.3、pyinstaller-hooks-contrib 2026.7、build用のpip 26.2.2（pip同梱certifi 2026.10.19）、単体certifi 2026.10.23。baseは`origin/main`の5f4eb75。
- buildの前に`pyinstaller-work`・`dist`・`spec`を削除し、PyInstallerを`--clean`で実行しました。bundleは83ファイルです。
- 同梱するCA資料: **0件**。PEM・証明書の拡張子を持つファイルは無く、`_internal/base_library.zip`（155 member）と`tcl90.dll`・`tcl9tk90.dll`の末尾のzip（839・90 member）にもありませんでした。PyInstallerのPYZ（`PYZ-00.toc`）にも`certifi`・`pip`・`truststore`・`requests`・`urllib3`は入っていません。アプリが`certifi`をimportしないため、`hook-certifi`（`cacert.pem`を集める）は使われません。凍結したexe内の`find_spec`も3つのmoduleを全て`None`と報告しました。
- 凍結した`_ssl`: `_internal/_ssl.pyd`（SHA-256 `4cfb154c…13b8`）と同梱の`libssl-3.dll`・`libcrypto-3.dll`を使い、OpenSSL 3.5.7です。既定のCA fileは`C:\Program Files\Common Files\SSL\cert.pem`、directoryは同じ場所の`certs`で、このhostにはどちらも存在しません（`cafile=None`、`capath=None`）。`SSL_CERT_FILE`・`SSL_CERT_DIR`は未設定です。
- 既定TLS contextのCA一覧は407件で、eMudhra・emSignの名前を持つ証明書は3件（`emSign Root CA - G1`、`emSign Root CA - C1`、`emSign ECC Root CA - C3`）、インシデントの対象は0件でした。WindowsストアはROOT 503件、CA 33件で、対象は0件でした。
- つまり、このbuildのbundleが信頼するのは、**実行したhostのWindowsのCA・ROOTストア**と、そのhostにOpenSSLの既定のCA file・directoryがあればその内容だけです。bundle自身はCA bundleを持ちません。
- 負例: このbundleの複製に、対象ルート（`emSign ECC Root CA - G3`）だけを持つ`_internal/certifi/cacert.pem`を置くと、検査3は終了コード1で失敗しました。単体テスト（`tests/test_bundle_trust_check.py`）は、前回のbuildの残骸と同じ位置・名前を変えたファイル・zip member・DLL末尾のzip・DER・不正なbase64・`TRUSTED CERTIFICATE`・PKCS7・上限超過で失敗することと、build scriptがPyInstallerの前に残骸を削除し後に検査3を実行することを確認します。
- CIの`windows-app-bundle` jobは、build scriptの検査3と、移動後の起動（検査2の`trust`項目）を毎回実行します。runnerのWindowsストアに対象ルートがあれば、このjobは失敗します。

### 過去の成果物

- GitHub Release: **0件**（`gh release list`、2026-11-04）。tagは`archive/refactor-source-package-layout-2026-11-01`の1件だけで、配布物は添付されていません。onefileのexe、ZIP、portable版を公開した記録はリポジトリ内にありません。
- CIのartifact `kadoka-windows-smoke-bundle`（`language-ci.yml`の`windows-app-bundle` jobがbundleのdirectoryをuploadしたもの）: **1560件**が未失効で残っています（作成日2026-09-14〜2026-11-03、各約15MB、失効予定2026-12-13〜2027-02-01）。リポジトリは公開のため、GitHubにログインした利用者はこれらをダウンロードできます。
- これらの過去の成果物は、作り直すことができず、この作業では中身を検査していません（ダウンロードしていません）。当時のbuild scriptもアプリのcertifiのimportを持たないため、同じ構成ならCA bundleを同梱しない見込みですが、確認ではありません。確認する場合は、artifactを新しい空のdirectoryへ展開して`python tools/windows_bundle_check.py trust <展開先>/`を実行します。対象証明書が見つかった場合は、そのartifactを削除し、利用者へ交換を案内します。artifactの削除と、確認を省いて一括削除するかの判断は保守者が行います（この作業では削除していません）。

### この節の限界

- 実際のTLS接続での検証（対象ルートが発行したchainを、凍結したアプリが拒否するかの試験）はしていません。
- Windowsのストアの結果は、確認したhostの、確認した時点の内容です。Windowsは必要になったルート証明書を自動で取得するため、利用者の手元でストアに無いことは、Windowsがそのchainを拒否する証明ではありません（上の「判定の限界」）。利用者の手元でOpenSSLの既定のCA file・directoryが作られていた場合、凍結したアプリはその内容も信頼します。
- 検査3は、zipでない圧縮データ（`Kadoka.exe`に埋め込まれたPYZ・PKGの中身）は展開しません。PYZはPython moduleのbytecodeで、PyInstallerはdataファイルをonedirの`_internal`へ置くため、`cacert.pem`はファイルとして現れます。moduleの文字列として埋め込まれた証明書は見つけられません。代わりに、検査2が`certifi`・`pip`・`truststore`のmoduleの有無を凍結したexe内で確認します。
- 証明書の照合は`tools/trust_inventory.py`と同じ名前・フィンガープリントの判定で、同じ限界があります。
- 確認したのはローカルの開発機1台のbuildです。CIのbuildの結果は、そのjobのログで確認します。

## アプリ本体のTLS利用

`src/ai_game_player`を`certifi`・`requests`・`httpx`・`urllib3`・`import ssl`・`create_default_context`で検索し、該当がないことを確認しました。HTTP通信は標準ライブラリの`urllib`だけで、既定ではloopbackのOllamaへ接続します。Providerの接続先をHTTPSのリモートにした場合、`urllib`は標準の`ssl`の既定コンテキストを使います。既定コンテキストは、どのOSでもOpenSSLの既定パス（ツールの`ssl_default_verify_paths`。`SSL_CERT_FILE`・`SSL_CERT_DIR`が設定されていればその値）を読み込み、Windowsではそれに加えてOSの信頼ストアを読み込みます。これはPythonの仕様からの説明で、実際の接続では確認していません。確認したローカル環境では、これらの環境変数は未設定でした。依存パッケージ経由でcertifiが使われるかは、アプリの依存が標準ライブラリとOCR用の任意依存（Pillow、pytesseract）だけであるため対象外としました。

## 影響の整理と未確認事項

| 対象 | 確認できたこと | 未確認 |
|---|---|---|
| ローカルのpip | 信頼源はWindowsストアとpip同梱certifi（2026.10.19）。pip同梱certifiに対象ルートは無い。Windowsストアは確認時点で未検出。設定ファイルは無い | Windowsの自動取得とMicrosoft側の扱い、実際のHTTPS接続 |
| 単体certifiを直接使うクライアント | このリポジトリのコードには無い。ローカルの単体certifi（2026.10.23）にインシデントの対象ルートは無い | 他のツールが単体certifiを使うか |
| アプリ本体 | certifiを使わず、標準`ssl`の既定コンテキストを使う | 実際のHTTPS接続での検証 |
| CI（ubuntu、quality job） | 上の「CI（GitHub Actions）」の節を参照 | Windowsのrunner |
| PyInstaller配布物 | 同梱するCA資料は0件。凍結した`_ssl`はhostのWindowsのCA・ROOTストア（確認したhostで対象0件）と、hostにあればOpenSSLの既定のCA file・directoryを信頼する。build・CIで毎回検査する（#515） | 過去のCI artifact 1560件の中身、利用者の手元のWindowsストア、実際のTLS接続 |

残る未確認事項:

1. 過去のCI artifact（`kadoka-windows-smoke-bundle`、1560件）の中身。上の「過去の成果物」を参照してください。現在のbuildの成果物は#515で確認済みです。
2. 実際のHTTPS接続での検証。

WindowsのrunnerのROOT・CAストアは、#514のgateと#515の検査2が毎回検査します。

## P0の重大性についての評価案

この評価案は、保守者が重大性を判断するための材料です。「OSの信頼ストアに対象名の証明書が無い」ことは、上の限界のとおり安全の根拠にしません。

前回の評価案では、P0から下げる条件を3つ挙げていました。2026-10-31の確認で、それぞれ次の状態です。

1. 残っていた証明書がインシデントの対象かどうか: **確認済み**。G1・C1・ECC C3は対象外で、対象ルート（ECC G3）はローカルのどのbundleにもありません。Windowsストアでは確認時点で未検出ですが、上の理由で安全の根拠にはしません。
2. PyInstaller配布物: 現在のbuildについて**確認済み**（#515、2026-11-04）。bundleは同梱するCA資料を持たず、凍結した`_ssl`の信頼源はhostのWindowsストアとOpenSSLの既定のCA file・directoryです。確認したhostでは対象0件でした。build scriptとCIは、対象証明書や読めないCA資料があれば失敗します。過去のCI artifactは作り直せず、中身は**未確認**です（GitHub Releaseは0件）。
3. CIとローカルのpipの信頼源: ローカルは、pip同梱certifiについて**確認済み**、Windowsストア側は**確認時点で未検出**です。CIは、上の「CI（GitHub Actions）」の節の値で判断します。

#513の時点では条件2が未確認だったため、#482全体をP0のまま扱っていました。本Issue（#513）の棚卸しで見つかったbundleには、更新が必要なものはありません（CIのubuntu環境を含む）。ただし、2026.10.18より前のcertifiを含む環境が見つかった場合は、更新の対象です。

#515の後の評価案（2026-11-04）: 3つの条件は、残りの未確認事項を除いて満たされました。対象ルートを同梱・信頼する配布物・build環境は見つかっておらず、build・CIは対象ルートを検出すると失敗します。配布物の経路でP0の理由として残るのは、公開リポジトリのCI artifact（1560件、失効予定2026-12-13〜2027-02-01）の中身が未確認であることだけです。これらはGitHub Releaseとして配布したものではなく、開発・確認用のartifactです。そのため、#482をP0から下げられると評価します（下げる場合の目安はP2）。ただし次の2つを条件とします。

- このPRのCI（`windows-app-bundle`）で、検査2の`trust`項目と検査3が合格すること。
- 過去のCI artifactの扱い（展開して検査3を実行する、削除する、または失効を待つ）を保守者が決めること。

P0から下げるかどうかは保守者が判断します。
