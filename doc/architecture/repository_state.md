# Repository Map: Pythonソース構造の機械可読インデックス

この文書は、Codex支援ツールの利用者と実装者に向けて、Pythonソースの宣言構造を検索する `generated/repo_map.json` の生成方法と収録範囲を説明します。Repository MapはTask Context PackやTest Impact Analysisが共通利用する索引です。Repository Map自体はソースコードを要約せず、実行時の呼び出し関係も推測しません。

## Repository Mapを生成する

リポジトリのルートで次のコマンドを実行します。

```powershell
python tools/analyze_repo.py
```

このコマンドは `config/repo_map.json` の `include` と `exclude` に従ってPythonファイルを選び、`generated/repo_map.json` を生成します。出力先の親フォルダーがない場合、generatorがフォルダーを作成します。生成前の解析に失敗した場合、generatorは既存のRepository Mapを置き換えません。

生成物が設定と一致することを確認するには、次のコマンドを実行します。

```powershell
python tools/analyze_repo.py --check
```

`finish_task.bat` とCIの `python tools/generate_docs.py --check` も、`tools/completion_config.json` で有効化されたRepository Mapの鮮度を確認します。実装を終えるときは `python tools/generate_docs.py` を実行し、図とRepository Mapの生成物をまとめて更新します。

`--check` はRepository Mapを上書きしません。現在のPythonソースから作ったJSONと保存済みファイルが一致すれば終了コード0を返します。不一致、構成エラー、読込失敗、Python構文エラーがある場合は、対象パスと理由を表示して0以外を返します。

別のcheckoutや一時fixtureを解析するときは `--root` でRepository Mapのルートを、`--config` でルート相対の設定ファイルを指定します。`--output` は設定ファイルの出力先をルート相対パスで上書きします。Generatorはルート外を指す設定パス・出力パス・source symlinkを拒否します。

Repository Map Generatorは出力先が設定ファイルまたは選択済みPythonソースと一致すると生成を拒否します。出力先に既存ファイルがある場合、同じ形式・schema versionのRepository Mapだけを置き換えます。Markdownや設定ファイルなど別用途の既存ファイルを誤ってJSONで上書きしないためです。

## 入力範囲を設定する

`config/repo_map.json` はversion 1のJSON設定です。`include` は解析するPythonファイルのPOSIX glob、`exclude` は選択後に除外するPOSIX glob、`output` は生成先です。初期設定は `src/`、`tools/`、`tests/` のPythonファイルを含み、`__pycache__`、生成コード、`.venv` 下のファイルを除外します。

設定を変更するときは、Task ContextとTest Impactが必要とする宣言・テストを含むことを確認します。globパターンに絶対パス、親ディレクトリ参照 `..`、Windows区切り `\` は指定できません。

## Repository Mapのレコードを読む

`config/repo_map.schema.json` は出力契約のJSON Schemaです。生成ファイルの `format` は `kadoka-repository-map`、`schema_version` は `1` です。schema変更時は互換性を確認してversionを上げます。

各module recordはルート相対のsource path、Python module名、source行範囲、宣言されたimport、同じRepository Map内で対応付けられたローカルmodule dependency、class/function/method symbolを持ちます。Module recordはルート相対POSIX path、symbolはsource行とqualified nameの順で出力するため、WindowsとLinuxで同じ並びになります。`from . import name` のようにpackageから属性を読み込むimportは、package moduleもdependency候補に含めます。

Symbol recordはqualname、宣言行、signature、parameter kind、annotation、decorator、class baseを保持します。Decorator内のstring・数値などのliteral値は `REDACTED` に置き換え、`True`、`False`、`None` など構造を示す値は残します。Classの `data_model` は構文上確認できたdataclass、Enum、Protocol継承だけをbooleanで示します。

Repository Mapのdependencyはimport文と解析対象module名の一致から作る静的関係です。実行時import、条件付きimportの有効性、動的生成、関数呼び出し、型の意味、継承先の実体を保証しません。解析不能なPythonファイルを黙って省略せず、ファイル名・行・列を付けたエラーで生成を止めます。

Repository Mapには関数本文、docstring、定数値、デフォルト値の内容を保存しません。Function signatureは引数名・型annotation・引数種別を含み、defaultの実値は `…` で隠します。Context Pack consumerは候補moduleとsymbolを絞ってから、必要なsourceとtestを読みます。

## Task Context Packが関連候補を示す

利用者はリポジトリのルートで `start_task.bat --issue NUMBER` を実行すると、指定Issueの本文と現在の作業ツリーに基づく `.codex/next_issue.md` を作成できます。Issue番号を省略すると、既存の優先順位規則で選ばれたIssueを使います。Task Context Packは、`git status` から変更パスと状態だけを読み、ファイル内容やdiff hunksは読みません。Repository Mapが有効なら、Issue本文と変更パスに一致するmodule、symbol、testの候補を追加します。

候補のconfidenceは、根拠の強さを示す0から1までのヒューリスティック値です。値は確率でも依存関係の保証でもありません。変更パスとの一致は0.98、Issue本文中のpath一致は0.92、module名一致は0.86、symbol名一致は0.84、識別子の部分一致は一致数に応じて0.34から0.68です。symbol候補の部分一致は0.36から0.68です。英語の識別子はunderscoreで分割するため、Issue本文の「screen capture」はmodule名 `screen_capture` と照合できます。各候補には一致理由を併記します。利用者は候補を探索の入口として扱い、Issueの要求と実装・テストで関係を確認してください。

Task Context Packは最大文字数の範囲でIssue本文を先に保持し、長い本文には省略位置を示します。候補証拠が上限に近づいた場合、低順位の候補から省略しますが、test候補または候補なしの説明と必須確認コマンドは残します。Repository Mapを読み込めない場合は理由を出し、mapに基づく候補を出しません。Task Context Packは候補test pathと必須確認コマンド `finish_task.bat` を示しますが、候補testの成功や機能動作を保証しません。実装者は変更後に該当testと `finish_task.bat` を実行してください。

Symbol名は `Class.method` 形式だけでなく、Issue本文に記載されたterminal method名とも照合します。Test候補は、表示対象に選ばれた上位4件のsource候補を基準に評価します。Repository Mapのdependency一致はsource候補のconfidenceに0.03を加算し、0.95を上限にします。sourceとtestのファイル名stem一致はsource候補のconfidenceに0.04を加算し、0.99を上限にします。両方の根拠があるtestでは高い方のconfidenceを使います。Issue本文がtest pathを直接指定した場合はconfidenceを0.99にします。
