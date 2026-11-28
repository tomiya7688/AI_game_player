"""クラウド環境用の軽量PreToolUseフック（個人情報・Eドライブ依存なし）。

1. 前景の5秒超sleepを拒否する（待機ごとに文脈全体を再読込するため）。
2. 環境変数 CLOUD_GUARD_BLOCK_TEXT（カンマ区切り）の文字列を外部送信系の入力に含めない（任意）。
3. コミット・push・外部送信コマンド（git commit/push、gh、curl/wget など）と、Web/MCP系ツールの入力に、
   ローカル絶対パス（Cドライブのユーザーフォルダ、E:のprojects・program_files）や
   秘密らしい文字列（ghp_・sk-・AKIA で始まる長い文字列）が含まれる場合に拒否する。
   git add・ls・テスト実行などの通常コマンドは対象外（誤検知を避ける）。

違反は終了コード2とstderrで拒否する。解析できない入力は許可し、理由をstderrへ出す。
"""

# モジュール: PreToolUseフックのスクリプト。標準入力のツール呼び出しJSONを検査する。

import json
import os
import re
import sys

SLEEP_RE = re.compile(r"(?:Start-Sleep(?:\s+-(?:Seconds|s))?|\bsleep)\s+(\d+)", re.I)
NET_RE = re.compile(r"curl|wget|https?://|\bgh\s|git\s+push", re.I)
# コミット・push・外部送信を行うコマンド（検査対象）。git add / ls / テスト実行などは含めない
OUTBOUND_RE = re.compile(
    r"\bgit\b[^;&|\n]{0,80}?\s(?:commit|push|tag|notes)\b|\bgh\s+(?:pr|issue|api|release|gist|repo)\b"
    r"|\b(?:curl|wget|scp|sftp|rsync)\b|\bInvoke-(?:WebRequest|RestMethod)\b|\bnc\s",
    re.I,
)
SEP = "[/" + re.escape(chr(92)) + "]+"  # スラッシュまたはバックスラッシュ（連続可）
# ローカル絶対パス（個人環境のパスをコミット・外部へ出さない）
LOCAL_PATH_RE = re.compile(
    r"\b[A-Za-z]:"
    + SEP
    + r"Users"
    + SEP
    + r"|\bE:"
    + SEP
    + r"(?:projects|program_files)\b",
    re.I,
)
# 秘密らしい文字列（接頭辞だけでなく後続の文字数も見て誤検知を減らす）
SECRET_RE = re.compile(
    r"\bghp_[A-Za-z0-9]{10,}|\bsk-[A-Za-z0-9_-]{10,}|\bAKIA[0-9A-Z]{10,}"
)


# {"責務":{"deny":"理由をstderrへ出して終了コード2で拒否する"},
#  "処理":["理由を出力する","終了コード2で終了する"],
#  "引数":{"msg":"拒否理由"},
#  "戻り値":[],
#  "副作用":{"stderr":"理由を出力","exit":"終了コード2"}}
def deny(msg):
    sys.stderr.write("[guard] " + msg + "\n")
    sys.exit(2)


# {"責務":{"is_external":"外部送信しうるツール呼び出しかを判定する"},
#  "処理":["Web系・MCP系は外部とみなす","シェルは通信を示す語を含むかで判定する"],
#  "引数":{"tool":"ツール名","cmd":"シェルコマンド文字列"},
#  "戻り値":{"external":"外部送信しうるならTrue"}}
def is_external(tool, cmd):
    if tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
        return True
    return tool in ("Bash", "PowerShell") and bool(NET_RE.search(cmd))


# {"責務":{"leak_kind":"コミット・送信しようとする入力に漏えいしやすい内容があるかを判定する"},
#  "処理":["コミット・push・外部送信コマンドまたはWeb/MCP系ツールか確認する","ローカル絶対パスを探す","秘密らしい文字列を探す"],
#  "引数":{"tool":"ツール名","cmd":"シェルコマンド文字列","inp":"ツール入力"},
#  "戻り値":{"kind":"検出した種別の説明。無ければNone"}}
def leak_kind(tool, cmd, inp):
    if tool in ("Bash", "PowerShell"):
        # 通常コマンド（git add・ls・テスト実行など）は検査しない
        if not OUTBOUND_RE.search(cmd):
            return None
        text = cmd
    elif tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
        text = json.dumps(inp, ensure_ascii=False)
    else:
        return None
    if LOCAL_PATH_RE.search(text):
        return "ローカル絶対パス"
    if SECRET_RE.search(text):
        return "秘密らしい文字列"
    return None


# {"責務":{"main":"標準入力のツール呼び出しを検査し、違反なら拒否する"},
#  "処理":["入力JSONを読む","sleep待ちを検査する","ローカルパス・秘密らしい文字列を検査する","任意の遮断文字列を外部送信系で検査する"],
#  "引数":[],
#  "戻り値":[],
#  "副作用":{"exit":"違反時は終了コード2"},
#  "エラー":{"JSON解析失敗":"許可して理由をstderrへ出す"}}
def main():
    try:
        data = json.load(sys.stdin)
    except ValueError as exc:
        sys.stderr.write("[guard] 入力を解析できないため検査を省略: %s\n" % exc)
        return
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    if tool in ("Bash", "PowerShell"):
        waits = [int(x) for x in SLEEP_RE.findall(cmd)]
        if waits and max(waits) > 5 and not inp.get("run_in_background"):
            deny(
                "5秒超のsleep待ちは禁止です。長い処理は run_in_background=true で実行し、終了通知を待ってください。"
            )
    kind = leak_kind(tool, cmd, inp)
    if kind:
        # 検出した値そのものは表示しない
        deny(
            "コミット・push・外部送信の入力に%sが含まれる可能性があるため拒否しました。"
            "該当部分を取り除いてから再実行してください。" % kind
        )
    block = [
        t.strip().lower()
        for t in os.environ.get("CLOUD_GUARD_BLOCK_TEXT", "").split(",")
        if t.strip()
    ]
    if block and is_external(tool, cmd):
        text = json.dumps(inp, ensure_ascii=False).lower()
        if any(t in text for t in block):
            deny("個人情報を含む外部送信の可能性があるため拒否しました。")


main()
