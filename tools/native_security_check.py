"""Native C/C++ ソースの危険API呼出しを検出するSecurity Profileの静的検査（Issue #115）。

コメントと文字列リテラルを除いたソースから、規則表に載せたAPIの関数呼出しを検出する。
許可リストに理由付きで登録された呼出しだけを除外し、使われなくなった許可リスト項目も失敗として扱う。
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

NATIVE_SOURCE_SUFFIXES = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"}
)
SEVERITY_ORDER = {"medium": 1, "high": 2}
MINIMUM_ALLOWLIST_REASON_LENGTH = 10
DEFAULT_ALLOWLIST = Path("config/native_security_allowlist.json")


@dataclass(frozen=True)
class NativeSecurityRule:
    rule_id: str
    severity: str
    functions: frozenset[str]
    message: str


RULES: tuple[NativeSecurityRule, ...] = (
    NativeSecurityRule(
        "NSC001",
        "high",
        frozenset(
            {
                "gets",
                "strcpy",
                "strcat",
                "sprintf",
                "vsprintf",
                "wcscpy",
                "wcscat",
                "swprintf",
                "vswprintf",
                "_mbscpy",
                "_mbscat",
                "lstrcpy",
                "lstrcpyA",
                "lstrcpyW",
                "lstrcat",
                "lstrcatA",
                "lstrcatW",
                "wsprintf",
                "wsprintfA",
                "wsprintfW",
                "StrCpy",
                "StrCat",
            }
        ),
        "境界を持たない文字列/書式APIです。std::string、std::format、snprintf等の容量付きAPIを使ってください",
    ),
    NativeSecurityRule(
        "NSC002",
        "high",
        frozenset(
            {
                "system",
                "_wsystem",
                "popen",
                "_popen",
                "_wpopen",
                "WinExec",
                "ShellExecuteA",
                "ShellExecuteW",
                "ShellExecuteExA",
                "ShellExecuteExW",
                "CreateProcessA",
                "CreateProcessW",
            }
        ),
        "外部コマンド/プロセス起動APIです。Native Runtimeから起動する場合は許可リストへ理由を登録してください",
    ),
    NativeSecurityRule(
        "NSC003",
        "high",
        frozenset({"alloca", "_alloca", "_malloca"}),
        "スタック上の可変長確保です。入力サイズ由来のスタック溢れを避けるためstd::vector等を使ってください",
    ),
    NativeSecurityRule(
        "NSC004",
        "medium",
        frozenset({"atoi", "atol", "atoll", "atof", "strtok", "rand", "srand"}),
        "失敗を検出できない変換、または再入不能/予測可能なAPIです。std::from_chars、std::mt19937等を使ってください",
    ),
    NativeSecurityRule(
        "NSC005",
        "medium",
        frozenset(
            {
                "memcpy",
                "memmove",
                "memset",
                "wmemcpy",
                "wmemmove",
                "CopyMemory",
                "MoveMemory",
                "RtlCopyMemory",
                "RtlMoveMemory",
            }
        ),
        "生のメモリ転送APIです。直前の容量検査を確認し、許可リストへ根拠を登録してください",
    ),
    NativeSecurityRule(
        "NSC006",
        "medium",
        frozenset(
            {"malloc", "calloc", "realloc", "free", "_aligned_malloc", "_aligned_free"}
        ),
        "手動のヒープ管理です。所有権をRAII（std::vector、std::unique_ptr等）へ移してください",
    ),
)

_FUNCTION_TO_RULE = {function: rule for rule in RULES for function in rule.functions}
_CALL_PATTERN = re.compile(
    r"(?<![\w.>:])(?:(?:std)?::)?(?P<name>"
    + "|".join(sorted(_FUNCTION_TO_RULE, key=len, reverse=True))
    + r")\s*\("
)
_RAW_STRING_PREFIX = re.compile(r'(?:u8|u|U|L)?R"(?P<delimiter>[^()\\\s"]{0,16})\(')


@dataclass(frozen=True)
class NativeSecurityFinding:
    rule: str
    severity: str
    path: str
    line_number: int
    function: str
    code: str
    message: str


@dataclass(frozen=True)
class AllowlistEntry:
    path: str
    rule: str
    function: str
    code: str
    reason: str


def strip_comments_and_literals(source: str) -> str:
    """C/C++ソースからコメントと文字列・文字リテラルの中身を空白へ置換し、行番号を保ったまま返す。"""
    output: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
            output.append(" " * (end - index))
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end == -1 else end + 2
            output.append(_blank(source[index:end]))
            index = end
            continue
        raw_match = _RAW_STRING_PREFIX.match(source, index)
        if raw_match is not None and (
            index == 0 or not (source[index - 1].isalnum() or source[index - 1] == "_")
        ):
            terminator = ")" + raw_match.group("delimiter") + '"'
            end = source.find(terminator, raw_match.end())
            end = length if end == -1 else end + len(terminator)
            output.append(_blank(source[index:end]))
            index = end
            continue
        if character == "'" and _is_digit_separator(source, index):
            output.append(character)
            index += 1
            continue
        if character in {'"', "'"}:
            end = _find_quoted_end(source, index)
            output.append(_blank(source[index:end]))
            index = end
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _blank(text: str) -> str:
    return "".join("\n" if character == "\n" else " " for character in text)


def _is_digit_separator(source: str, index: int) -> bool:
    # C++14 の桁区切り（1'000）は数値トークン内の ' であり、文字リテラルの開始ではない。
    token_start = index
    while token_start > 0 and (
        source[token_start - 1].isalnum() or source[token_start - 1] in "'_"
    ):
        token_start -= 1
    return (
        token_start < index
        and source[token_start].isdigit()
        and index + 1 < len(source)
        and source[index + 1].isalnum()
    )


def _find_quoted_end(source: str, start: int) -> int:
    quote = source[start]
    index = start + 1
    while index < len(source):
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character == quote or character == "\n":
            return index + 1
        index += 1
    return len(source)


def scan_source(relative_path: str, source: str) -> list[NativeSecurityFinding]:
    """1ファイル分のソース文字列から規則に該当する関数呼出しを列挙する。

    関数名と `(` の間の改行も検出するため、行ごとではなく除去済みソース全体へ照合し、
    報告する行番号とコードは関数名がある行とする。
    """
    # CRLFを先に正規化し、文字列内の行継続（\ + CRLF）を通常の改行と同じに扱う。
    stripped = strip_comments_and_literals(source.replace("\r\n", "\n"))
    stripped_lines = stripped.split("\n")
    findings: list[NativeSecurityFinding] = []
    for match in _CALL_PATTERN.finditer(stripped):
        function = match.group("name")
        rule = _FUNCTION_TO_RULE[function]
        line_number = stripped.count("\n", 0, match.start("name")) + 1
        findings.append(
            NativeSecurityFinding(
                rule.rule_id,
                rule.severity,
                relative_path,
                line_number,
                function,
                _normalize_code(stripped_lines[line_number - 1]),
                rule.message,
            )
        )
    return findings


def _normalize_code(line: str) -> str:
    return " ".join(line.split())


def iter_native_sources(paths: Iterable[Path]) -> list[Path]:
    """引数のファイル/ディレクトリから検査対象のC/C++ソースを決定的な順序で返す。"""
    collected: set[Path] = set()
    for path in paths:
        if path.is_dir():
            collected.update(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix.lower() in NATIVE_SOURCE_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in NATIVE_SOURCE_SUFFIXES:
            collected.add(path)
    return sorted(collected)


def load_allowlist(path: Path) -> list[AllowlistEntry]:
    """許可リストJSONを読み、必須項目と理由の長さを検証して返す。"""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"{path}: schema_version 1 のオブジェクトが必要です")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: entries は配列である必要があります")
    entries: list[AllowlistEntry] = []
    for position, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{path}: entries[{position}] はオブジェクトである必要があります"
            )
        values = {
            key: raw.get(key) for key in ("path", "rule", "function", "code", "reason")
        }
        missing = [
            key
            for key, value in values.items()
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise ValueError(
                f"{path}: entries[{position}] に文字列 {', '.join(missing)} が必要です"
            )
        reason = str(values["reason"]).strip()
        if len(reason) < MINIMUM_ALLOWLIST_REASON_LENGTH:
            raise ValueError(f"{path}: entries[{position}] の reason が短すぎます")
        entries.append(
            AllowlistEntry(
                _normalize_relative_path(str(values["path"])),
                str(values["rule"]),
                str(values["function"]),
                _normalize_code(str(values["code"])),
                reason,
            )
        )
    return entries


def _normalize_relative_path(path: str) -> str:
    # 許可リストの `./native/...` や `native\\...` を検出側の相対POSIXパスと同じ形にそろえる。
    return posixpath.normpath(path.replace("\\", "/")).removeprefix("./")


def apply_allowlist(
    findings: Sequence[NativeSecurityFinding], entries: Sequence[AllowlistEntry]
) -> tuple[list[NativeSecurityFinding], list[AllowlistEntry]]:
    """許可リストに一致した検出を除き、残った検出と一度も一致しなかった許可リスト項目を返す。

    1項目は1件の検出だけを許可する。同じ内容の行が複数ある場合は、その件数分の項目が必要。
    """
    used: set[int] = set()
    remaining: list[NativeSecurityFinding] = []
    for finding in findings:
        matched = False
        for position, entry in enumerate(entries):
            if (
                position not in used
                and entry.path == finding.path
                and entry.rule == finding.rule
                and entry.function == finding.function
                and entry.code == finding.code
            ):
                used.add(position)
                matched = True
                break
        if not matched:
            remaining.append(finding)
    stale = [entry for position, entry in enumerate(entries) if position not in used]
    return remaining, stale


def run_check(
    paths: Sequence[Path], root: Path, allowlist_path: Path | None, fail_on: str
) -> tuple[
    list[NativeSecurityFinding], list[NativeSecurityFinding], list[AllowlistEntry]
]:
    """対象ソースを走査し、(blocking検出, 報告のみの検出, 未使用の許可リスト項目) を返す。

    検査対象のソースが0件の場合は、空の検査を成功扱いしないため ValueError を送出する。
    """
    sources = iter_native_sources(paths)
    if not sources:
        raise ValueError("検査対象のC/C++ソースが0件です")
    findings: list[NativeSecurityFinding] = []
    for source_path in sources:
        relative = source_path.resolve().relative_to(root.resolve()).as_posix()
        findings.extend(
            scan_source(
                relative, source_path.read_text(encoding="utf-8", errors="replace")
            )
        )
    entries = load_allowlist(allowlist_path) if allowlist_path is not None else []
    remaining, stale = apply_allowlist(findings, entries)
    threshold = SEVERITY_ORDER[fail_on]
    blocking = [
        finding
        for finding in remaining
        if SEVERITY_ORDER[finding.severity] >= threshold
    ]
    advisory = [
        finding for finding in remaining if SEVERITY_ORDER[finding.severity] < threshold
    ]
    return blocking, advisory, stale


def _format_finding(finding: NativeSecurityFinding) -> str:
    return (
        f"{finding.path}:{finding.line_number}: {finding.rule} [{finding.severity}] "
        f"{finding.function}(): {finding.message}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Native C/C++ の危険API呼出しを検出する（Issue #115）"
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("native")])
    parser.add_argument(
        "--root", type=Path, default=Path("."), help="報告と許可リストのパス基準"
    )
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--no-allowlist",
        action="store_true",
        help="許可リストを使わずに全検出を報告する",
    )
    parser.add_argument("--fail-on", choices=sorted(SEVERITY_ORDER), default="medium")
    parser.add_argument("--output", type=Path, help="検出結果のJSONレポート出力先")
    args = parser.parse_args(argv)

    allowlist_path = None if args.no_allowlist else args.allowlist
    try:
        blocking, advisory, stale = run_check(
            args.paths, args.root, allowlist_path, args.fail_on
        )
    except (OSError, ValueError) as error:
        print(f"native-security-check: {error}", file=sys.stderr)
        return 2

    for finding in blocking:
        print(_format_finding(finding), file=sys.stderr)
    for finding in advisory:
        print("advisory: " + _format_finding(finding))
    for entry in stale:
        print(
            f"{entry.path}: 許可リスト項目 {entry.rule} {entry.function}() が現在のソースに一致しません。"
            "削除または更新してください",
            file=sys.stderr,
        )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": 1,
            "fail_on": args.fail_on,
            "blocking": [finding.__dict__ for finding in blocking],
            "advisory": [finding.__dict__ for finding in advisory],
            "stale_allowlist": [entry.__dict__ for entry in stale],
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if blocking or stale:
        return 1
    print("native-security-check: no blocking findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
