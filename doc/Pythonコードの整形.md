# 変更したPythonファイルを整形する

この手順はPythonコードを変更する開発者向けです。整形漏れを検出しつつ、別の作業者の変更や既存ファイル全体の一括書き換えを避けます。

## 準備と対象

リポジトリのルートで`python -m pip install -e ".[dev]"`を実行してください。`pyproject.toml`の開発用依存に固定したRuff 0.15.22を使います。別のformatterやグローバル最新版は必要ありません。

今回変更したPythonファイルまたはモジュールを明示してください。引数なしの`format_code.bat`は終了コード2で停止します。`build`、`generated`、`vendor`、`.codex`、添付ファイルは整形対象から除外します。担当外の未保存変更や原文保持が必要なサンプルは指定しないでください。

## 整形と確認

リポジトリのルートから、次のコマンドを順に実行してください。

```bat
format_code.bat tests/test_fail_safe_initial_observation_grace.py
format_code.bat --check tests/test_fail_safe_initial_observation_grace.py
```

最初のコマンドは指定ファイルを整形します。`--check`は書き換えず、整形済みなら終了コード0、整形漏れがあれば終了コード1を返します。Ruffが見つからない場合は、開発用依存を準備したPython環境で実行してください。

整形後は対象のテストと`finish_task.bat`を実行してください。レビュー指摘を修正した場合も、整形と確認を繰り返してください。

## ローカルとCIで確認する範囲

既存コード全体の一括整形は行いません。最初の適用範囲は`tests/test_fail_safe_initial_observation_grace.py`です。`finish_task.bat`と`tests.yml`のPython quality jobは、このファイルを同じ固定版Ruffで確認します。ほかのファイルの変更担当者は変更対象を明示して整形し、CIでの確認範囲を広げる場合も、無関係なファイルを一括変更しないでください。

このコマンドはPython専用です。Markdown、YAML、TOML、batchの整形は実行しません。これらの設定・文書は差分と対応する構文検査で確認してください。
