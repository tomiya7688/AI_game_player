"""設計書の検査（クラウド用。外部通信なし・標準ライブラリのみ）。

使い方: python3 tools/check_docs.py [docs_dir ...] [--strict]
  docs_dir 省略時は docs、無ければ doc を対象にする。どちらも無ければ「対象なし」で終了コード0。
検査:
  1. 相対リンクの存在（リンク切れはエラー）
  2. 目次(README.md)の存在と、目次から全文書への到達性
  3. 必須見出し（H1、冒頭の要約: 目的・状態・最終確認日）
終了コード: 0=エラーなし / 1=エラーあり。目次の欠落・到達性・要約の不足は警告で、--strict の時だけエラーにする。
設計書検査スクリプト Check-Docs.ps1（ローカル版）のリンク検査部分のPython移植（markdownlint・textlintは含まない）。
"""

# モジュール: 設計書ディレクトリのMarkdownを走査し、リンク・見出し・到達性を検査するCLI。

import os
import re
import sys
from urllib.parse import unquote

FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_RE = re.compile(r"`[^`\n]*`")
LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
WIN_ABS_RE = re.compile("^[A-Za-z]:[/" + re.escape(chr(92)) + "]")
SCHEME_RE = re.compile("^[A-Za-z][A-Za-z0-9+.-]*:")
SUMMARY_KEYS = ("目的", "状態", "最終確認日")
SKIP_DIRS = {"node_modules", ".git", "generated"}


# {"責務":{"strip_code":"リンク検査の邪魔になるコードブロックとインラインコードを除く"},
#  "処理":["コードフェンスを空にする","インラインコードを空にする"],
#  "引数":{"text":"Markdown本文"},"戻り値":{"text":"コードを除いた本文"}}
def strip_code(text):
    return INLINE_RE.sub("", FENCE_RE.sub("", text))


# {"責務":{"collect":"docs配下のMarkdownを集める"},
#  "処理":["再帰的に走査する","除外ディレクトリを飛ばす","POSIX形式の相対パスで返す"],
#  "引数":{"root":"docsディレクトリ"},"戻り値":{"files":"Markdownの相対パス一覧(昇順)"}}
def collect(root):
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.lower().endswith(".md"):
                out.append(
                    os.path.relpath(os.path.join(cur, f), root).replace(os.sep, "/")
                )
    return sorted(out)


# {"責務":{"links_of":"1つの文書から検査対象の相対リンクを取り出す"},
#  "処理":["コードを除く","リンク先を取り出す","外部URL・アンカーのみ・空を除く","アンカーとクエリを落とす"],
#  "引数":{"text":"Markdown本文"},"戻り値":{"targets":"リンク先(デコード済み)の配列"}}
def links_of(text):
    targets = []
    for raw in LINK_RE.findall(strip_code(text)):
        if raw.startswith("#"):
            continue
        if not WIN_ABS_RE.match(raw) and SCHEME_RE.match(raw):
            continue  # http:・mailto: など外部スキーム
        path = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if path:
            targets.append(path)
    return targets


# {"責務":{"check_summary":"文書のH1と冒頭の要約(目的・状態・最終確認日)を検査する"},
#  "処理":["H1の有無を見る","冒頭40行に要約キーが揃うか見る"],
#  "引数":{"text":"Markdown本文"},"戻り値":{"problems":"警告メッセージの配列"}}
def check_summary(text):
    problems = []
    if not re.search(r"^# \S", text, re.M):
        problems.append("H1見出し(# )がありません")
    head = "\n".join(text.splitlines()[:40])
    lacking = [k for k in SUMMARY_KEYS if k not in head]
    if lacking:
        problems.append("冒頭40行に要約の項目がありません: " + "・".join(lacking))
    return problems


# {"責務":{"check_dir":"1つのdocsディレクトリを検査する"},
#  "処理":["Markdownを集める","リンク切れを数える","目次から到達できる文書を幅優先で求める","見出し・要約を警告に集める"],
#  "引数":{"root":"docsディレクトリ"},"戻り値":{"result":"(errors, warnings, 文書数)"}}
def check_dir(root):
    errors, warnings = [], []
    files = collect(root)
    graph = {}
    for rel in files:
        path = os.path.join(root, rel)
        try:
            text = open(path, encoding="utf-8-sig").read()
        except (OSError, UnicodeError) as exc:
            errors.append("%s: 読み取り失敗 (%s)" % (rel, exc))
            continue
        graph[rel] = set()
        base = os.path.dirname(path)
        for t in links_of(text):
            if WIN_ABS_RE.match(t):
                warnings.append(
                    "%s: ローカル絶対パスのリンクはクラウドで辿れません -> %s"
                    % (rel, t)
                )
                continue
            target = os.path.normpath(
                os.path.join(os.getcwd(), t.lstrip("/"))
                if t.startswith("/")
                else os.path.join(base, t)
            )
            if not os.path.exists(target):
                errors.append("%s: リンク切れ -> %s" % (rel, t))
            elif target.lower().endswith(".md"):
                nxt = os.path.relpath(target, root).replace(os.sep, "/")
                if not nxt.startswith(".."):  # docs外のリンクは到達性の対象にしない
                    graph[rel].add(nxt)
        warnings.extend(
            "%s: %s" % (rel, p)
            for p in check_summary(text)
            if rel.lower() != "readme.md" or "H1" in p
        )
    index = next((f for f in files if f.lower() == "readme.md"), None)
    if files and index is None:
        warnings.append("目次(README.md)がありません: %s" % root)
    elif index:
        seen, queue = {index}, [index]
        while queue:
            for nxt in graph.get(queue.pop(0), ()):
                if nxt not in seen and nxt in graph:
                    seen.add(nxt)
                    queue.append(nxt)
        warnings.extend("%s: 目次から辿れません" % f for f in files if f not in seen)
    return errors, warnings, len(files)


# {"責務":{"main":"引数を解釈して検査し、結果と終了コードを返す"},
#  "処理":["対象ディレクトリを決める","各ディレクトリを検査する","結果を表示する","strictなら警告もエラーに数える"],
#  "引数":{"argv":"コマンドライン引数"},"戻り値":{"code":"終了コード(0/1)"},
#  "副作用":{"stdout":"検査結果の表示"}}
def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windowsの文字化け対策
    except (AttributeError, ValueError):
        pass
    strict = "--strict" in argv
    unknown = [a for a in argv if a.startswith("-") and a != "--strict"]
    if unknown:
        print("未知のオプション: " + " ".join(unknown))
        return 2
    dirs = [a for a in argv if not a.startswith("-")]
    if not dirs:
        dirs = [d for d in ("docs", "doc") if os.path.isdir(d)][:1]
    if not dirs:
        print("対象なし: docs/ も doc/ もありません")
        return 0
    bad = 0
    for d in dirs:
        if not os.path.isdir(d):
            print("エラー: ディレクトリがありません: " + d)
            bad += 1
            continue
        errors, warnings, n = check_dir(d)
        print("== %s (%d 文書)" % (d, n))
        for e in errors:
            print("エラー: " + e)
        for w in warnings:
            print("警告: " + w)
        bad += len(errors) + (len(warnings) if strict else 0)
    print("結果: " + ("指摘あり" if bad else "問題なし"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
