# Python Security Profile

この文書は、Python層のセキュリティ静的解析（Issue #114）の判定規則、抑制の管理方法、CIでの実行方法を説明します。一般的なlint・型検査（Issue #86の`tests.yml`のRuff・mypy）とは別のjobで実行し、両方の結果を別々に確認します。checkerの指摘は脆弱性の確定ではなく、レビューが必要な箇所の検出です。

## 構成

| ファイル | 役割 |
|---|---|
| `tools/python_security_requirements.txt` | Bandit 1.9.4とpip-audit 2.10.1の固定版です。通常の`.[dev]`とは別の環境へ入れます。 |
| `config/python_security_profile.json` | 走査対象、blockingの閾値、重点監視するmodule、抑制の一覧を宣言します。 |
| `tools/python_security_check.py` | Banditとpip-auditのJSON出力をprofileで判定し、`build/python-security/`へ判定結果のJSONを保存します。判定処理はネットワークへ接続しません。 |
| `.github/workflows/python-security-ci.yml` | push・pull_request・毎週月曜の定期実行・手動実行で、Bandit判定とdependency auditを別jobとして実行します。`tools/pr_workflow_evidence.py`の必須workflowと`.github/rulesets/main-protection.json`の必須checkへ追加しています（GitHub上のruleset適用は所有者が別途行います）。 |

## Banditの判定規則

- 走査対象は`src`と`tools`です。`scan_paths`は空にできず、絶対path、`..`を含むpath、Banditのoptionと解釈される`-`始まりのpathは拒否します。Bandit起動は固定argvでshellを使わないため、Sourcery/opengrepの`dangerous-subprocess-use-audit`はその行だけ`nosemgrep`で理由付きに抑止しています。`tests`はfixtureやテスト用の危険な入力を含むため対象外です。
- severityとconfidenceがともにMEDIUM以上の指摘はblockingです。変数を含むcommandをshellで実行するsubprocessの`shell=True`や`os.system`、pickle等の危険なdeserialize、`yaml.load`、`tempfile.mktemp`、`eval`/`exec`、TLS検証の無効化、tarfileの無検査展開、弱いhashなどが該当します。
- 重点監視module（Provider、動的importするPlanning worker、worker起動、Native読込、file/network境界）では、LOWの指定rule（B105〜B107の秘密値の直書き、B110/B112の例外の握りつぶし、B603/B607のsubprocess起動）もblockingにします。confidenceの下限（MEDIUM）は重点moduleでも変えません。対象のpathは`sensitive_modules.paths`で宣言します。
- 上記以外のLOW指摘は判定結果のJSONへ記録するだけで、CIを失敗させません。
- Banditの行内`# nosec`は理由と期限を持てないため、`--ignore-nosec`で無効化します。抑制はprofileの`suppressions`だけで管理します。
- Banditがfileを解析できなかった場合は、走査が不完全なためblockingにします。走査file数が0件の報告、未知のseverity/confidence、repository外のpathもblockingにします。走査対象に`.bandit`設定fileがある場合はprofileを上書きできるため、`--run-bandit`を中止します（終了コード2）。

## 抑制の管理

`suppressions`の各項目は、tool、rule、対象（Banditは`path`、pip-auditは`package`）、理由、期限、Issue番号を必須とします。

- Banditの抑制は`match`に指摘行の文字列を指定し、同じfileの同じruleでも別の呼出し箇所を隠さないようにします。1項目が抑制する件数は`count`（省略時1）までで、同じ文字列の呼出しが増えた場合は超過分がblockingになります。
- 理由は10文字以上、Issue番号は`#114`の形式、期限は`YYYY-MM-DD`で判定日から`max_suppression_days`以内でなければprofileを読み込めません（終了コード2）。`max_suppression_days`自体も366日を超える値にはできません。
- 期限切れの抑制（PSP901）と、どの指摘にも一致しない抑制（PSP902）はblockingです。対象fileを削除・改名した場合や、指摘が閾値未満になった場合もPSP902になります。修正後は抑制を削除し、継続する場合は再レビューして期限を更新します。

現在の抑制は、利用者が設定するOllama endpointへの`urlopen`（http/https以外のschemeを拒否する検証は未実装）、https固定・SHA-256照合済みの参照ゲーム取得、127.0.0.1だけを読む開発用probe、固定argvのPlanner worker起動の4件です。

## dependency audit（Issue #90との接続）

`dependency-audit` jobは、`.[dev,ocr,browser-validation]`を別のvenvへ入れ、`PIPAPI_PYTHON_LOCATION`でそのvenvをpip-auditへ監査させます。pip-auditは公開の脆弱性情報へ問い合わせるため、結果は実行日に依存します。

profileの`dependency_audit.mode`は現在`advisory`です。既知の脆弱性は警告として表示し、判定結果のJSONへ保存しますが、PRを失敗させません。pip-auditが報告を書けなかった場合（fileが無い、または空）、push・pull_requestでは「未確認」の警告を出してjobを成功させ、定期実行・手動実行ではjobを失敗させます。blockingへの移行と依存lockの方針はIssue #90で決めます。

## ローカルでの実行

```powershell
python -m venv E:\path\to\security-venv
E:\path\to\security-venv\Scripts\python -m pip install -r tools\python_security_requirements.txt
E:\path\to\security-venv\Scripts\python tools\python_security_check.py --run-bandit
```

既存のBandit JSONを判定するときは`--bandit-report PATH`、pip-auditのJSONは`--pip-audit-report PATH`を指定します。終了コードは0が合格、1がblocking指摘あり、2がprofileまたは報告を読めない状態です。

## 未対応の範囲

- Semgrep・CodeQL等の追加SASTは導入していません。
- `finish_task.bat`はBanditを実行しません。CIの`python-security-ci`だけで判定します。
- Ollama endpointのscheme検証、dependency auditのblocking化は未実装です。
