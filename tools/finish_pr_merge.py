from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

API_COMMAND_TIMEOUT_SECONDS = 60
CHECK_COMMAND_TIMEOUT_SECONDS = 60 * 60
MAX_REVIEW_THREAD_PAGES = 100
MAX_DISPLAYED_TEXT_LENGTH = 2000
GIT_SHA1_HEX_LENGTH = 40
GIT_SHA256_HEX_LENGTH = 64
PR_HEAD_SHA_PATTERN = re.compile(
    rf"^(?:[0-9a-f]{{{GIT_SHA1_HEX_LENGTH}}}|[0-9a-f]{{{GIT_SHA256_HEX_LENGTH}}})$",
    re.IGNORECASE,
)
REVIEW_THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          isResolved
          path
          line
          originalLine
          comments(first: 1) {
            nodes {
              body
              url
              author { login }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


# {
#   責務: [PullRequestReference: GitHub pull request URLと、API照会に使うowner・repository・番号を同じ識別対象として保持する]
#   保持状態: [url: 正規化したHTTPSのpull request URL, owner: GitHubアカウントまたは組織名, repository: リポジトリ名, number: 正のpull request番号]
# }
@dataclass(frozen=True)
class PullRequestReference:
    url: str
    owner: str
    repository: str
    number: int


# {
#   責務: [PullRequestSnapshot: merge前に確認したpull requestの公開状態・競合状態・head commitを一組で保持する]
#   保持状態: [state: OPENであることを確認した状態名, mergeable: MERGEABLEであることを確認した状態名, head_sha: CIとreview確認の対象となるcommit SHA]
# }
@dataclass(frozen=True)
class PullRequestSnapshot:
    state: str
    mergeable: str
    head_sha: str


# {
#   責務: [UnresolvedReviewThread: mergeを止めた未解決inline review threadの場所と、利用者が修正に必要な投稿情報を保持する]
#   保持状態: [path: 指摘対象のリポジトリ内パス, line: 指摘行または不明値, author: 投稿者名, body: 指摘本文, url: 指摘を開くGitHub URL]
# }
@dataclass(frozen=True)
class UnresolvedReviewThread:
    path: str
    line: int | None
    author: str
    body: str
    url: str


# {
#   責務: [MergeGateError: PRの状態・CI・reviewを安全に確認できずmergeを中止した理由をCLIへ伝える]
#   保持状態: [RuntimeErrorのmessage: 利用者が次の確認を判断するための停止理由]
# }
class MergeGateError(RuntimeError):
    pass


# {
#   責務: [parse_pull_request_reference: 入力文字列がGitHub上の単一PRを指すことを検証し、API照会用の識別情報へ変換する]
#   処理: [HTTPS、github.com、余分な認証情報・port・query・fragmentがないことを確認し、owner・repository・正の番号を抽出する]
#   引数: [pull_request_url: finish_pr.batから渡されたPR URL]
#   戻り値: [PullRequestReference: canonicalなURLとGitHub API照会に使うPR識別情報]
#   エラー: [MergeGateError: URL形式、ホスト、pathまたはPR番号が許可条件を満たさない]
# }
def parse_pull_request_reference(pull_request_url: str) -> PullRequestReference:
    try:
        parsed_url = urlparse(pull_request_url)
        parsed_port = parsed_url.port
    except ValueError as error:
        raise MergeGateError("PR URL contains an invalid host or port.") from error
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_port is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise MergeGateError("PR URL must be an https://github.com/OWNER/REPOSITORY/pull/NUMBER URL.")

    path_parts = parsed_url.path.strip("/").split("/")
    if len(path_parts) != 4 or path_parts[2].lower() != "pull":
        raise MergeGateError("PR URL must identify exactly one GitHub pull request.")

    owner, repository, _, pull_request_number_text = path_parts
    if not owner or not repository or not pull_request_number_text.isdecimal():
        raise MergeGateError("PR URL has an invalid owner, repository, or pull request number.")

    pull_request_number = int(pull_request_number_text)
    if pull_request_number <= 0:
        raise MergeGateError("PR number must be greater than zero.")

    canonical_url = f"https://github.com/{owner}/{repository}/pull/{pull_request_number}"
    return PullRequestReference(canonical_url, owner, repository, pull_request_number)


# {
#   責務: [_run_gh: GitHub CLIを引数配列で実行し、shell解釈を介さず終了結果を呼び出し元へ返す]
#   処理: [UTF-8出力を取得し、指定秒数で打ち切る。実行不能・timeoutもmerge停止理由へ変換する]
#   引数: [arguments: ghの後ろに渡す個別のCLI引数, timeout_seconds: commandを待つ上限秒数]
#   戻り値: [CompletedProcess[str]: 標準出力・標準エラー・終了codeを含む実行結果]
#   エラー: [MergeGateError: ghがPATHにない、OSが起動できない、または制限時間を超過する]
# }
def _run_gh(arguments: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise MergeGateError("GitHub CLI (gh) was not found in PATH; PR merge was stopped.") from error
    except subprocess.TimeoutExpired as error:
        raise MergeGateError(
            f"GitHub CLI did not finish within {timeout_seconds} seconds; PR merge was stopped."
        ) from error
    except OSError as error:
        raise MergeGateError(f"GitHub CLI could not run ({error}); PR merge was stopped.") from error


# {
#   責務: [_command_details: GitHub CLI失敗時に原因を特定できる範囲の診断文を選び、表示上限内に整える]
#   処理: [stderrを優先し、stderrが空ならstdoutを使う。診断文は最大MAX_DISPLAYED_TEXT_LENGTH文字に制限する]
#   引数: [command_result: _run_ghが返した失敗または成功の実行結果]
#   戻り値: [str: trim済みの診断文。出力がない場合はその事実を示す固定文]
# }
def _command_details(command_result: subprocess.CompletedProcess[str]) -> str:
    command_text = (command_result.stderr or command_result.stdout or "").strip()
    if not command_text:
        return "GitHub CLI returned no diagnostic text."
    if len(command_text) > MAX_DISPLAYED_TEXT_LENGTH:
        return command_text[:MAX_DISPLAYED_TEXT_LENGTH] + "..."
    return command_text


# {
#   責務: [_run_successful_gh_command: GitHub CLIを実行し、終了codeが0以外なら診断文付きで処理を中止する]
#   処理: [timeout付き実行の後、成功時だけCompletedProcessを返し、失敗時はaction名・終了code・診断をMergeGateErrorへまとめる]
#   引数: [arguments: ghへ渡す個別のCLI引数, timeout_seconds: command完了を待つ上限秒数, action_description: 失敗理由に表示する操作名]
#   戻り値: [CompletedProcess[str]: 終了codeが0のGitHub CLI実行結果]
#   エラー: [MergeGateError: GitHub CLIが0以外で終了するか起動に失敗する]
# }
def _run_successful_gh_command(
    arguments: list[str], timeout_seconds: int, action_description: str
) -> subprocess.CompletedProcess[str]:
    command_result = _run_gh(arguments, timeout_seconds)
    if command_result.returncode != 0:
        raise MergeGateError(
            f"{action_description} failed with GitHub CLI exit code {command_result.returncode}: "
            f"{_command_details(command_result)}"
        )
    return command_result


# {
#   責務: [_decode_json_output: GitHub CLIの標準出力をJSONとして解析し、後続処理が形状を検証できる値にする]
#   引数: [command_result: JSONを標準出力へ返したGitHub CLI実行結果, action_description: 解析失敗に表示する操作名]
#   戻り値: [object: JSON object・array・scalarのいずれか。具体的な形状検証は呼び出し側が行う]
#   エラー: [MergeGateError: 標準出力が有効なJSONではない]
# }
def _decode_json_output(command_result: subprocess.CompletedProcess[str], action_description: str) -> object:
    try:
        return json.loads(command_result.stdout)
    except json.JSONDecodeError as error:
        raise MergeGateError(f"{action_description} returned invalid JSON; PR merge was stopped.") from error


# {
#   責務: [_read_pull_request_snapshot: GitHubからPR状態を取得し、OPEN・MERGEABLE・有効なhead SHAを確認する]
#   処理: [gh pr viewのJSONでstate、mergeable、headRefOidを読み、欠落や未知状態を許さない]
#   引数: [pull_request: 検査対象のGitHub pull request識別情報]
#   戻り値: [PullRequestSnapshot: merge gateが確認した状態とhead commit]
#   エラー: [MergeGateError: API失敗、JSON不正、PR終了、merge不可、またはhead SHA不正]
# }
def _read_pull_request_snapshot(pull_request: PullRequestReference) -> PullRequestSnapshot:
    command_result = _run_successful_gh_command(
        ["pr", "view", pull_request.url, "--json", "state,mergeable,headRefOid"],
        API_COMMAND_TIMEOUT_SECONDS,
        "Pull request state lookup",
    )
    response_data = _decode_json_output(command_result, "Pull request state lookup")
    if not isinstance(response_data, dict):
        raise MergeGateError("Pull request state lookup returned an unexpected JSON shape.")

    state = response_data.get("state")
    mergeability = response_data.get("mergeable")
    head_sha = response_data.get("headRefOid")
    if not isinstance(state, str) or not isinstance(mergeability, str) or not isinstance(head_sha, str):
        raise MergeGateError("Pull request state lookup omitted state, mergeability, or head commit.")
    if state != "OPEN":
        raise MergeGateError(f"Pull request state is {state!r}, not OPEN; PR merge was stopped.")
    if mergeability != "MERGEABLE":
        raise MergeGateError(
            f"Pull request mergeability is {mergeability!r}, not MERGEABLE; PR merge was stopped."
        )
    if PR_HEAD_SHA_PATTERN.fullmatch(head_sha) is None:
        raise MergeGateError("Pull request head commit is missing or invalid; PR merge was stopped.")

    return PullRequestSnapshot(state, mergeability, head_sha)


# {
#   責務: [_wait_for_status_checks: すべてのPR status checkが完了するまでGitHub CLIで待機し、非成功を停止理由にする]
#   処理: [gh pr checks --watch --fail-fastを最大CHECK_COMMAND_TIMEOUT_SECONDS秒実行する]
#   引数: [pull_request: status checkを待つGitHub pull request識別情報]
#   戻り値: [なし: すべて成功した場合だけ呼び出し元へ戻る]
#   エラー: [MergeGateError: check失敗、認証/APIエラー、gh起動失敗、または待機timeout]
# }
def _wait_for_status_checks(pull_request: PullRequestReference) -> None:
    print("[WAIT] GitHub Actions checks are running; waiting for every check to finish.", flush=True)
    _run_successful_gh_command(
        ["pr", "checks", pull_request.url, "--watch", "--fail-fast"],
        CHECK_COMMAND_TIMEOUT_SECONDS,
        "Waiting for pull request status checks",
    )


# {
#   責務: [_read_status_checks: GitHubが返す全PR status checkの状態bucketを検査し、すべてpassの場合に限って一覧を返す]
#   処理: [空一覧、項目形式不正、name・state・bucket欠落、pass以外のbucketを検出してmergeを停止する]
#   引数: [pull_request: status check一覧を取得するGitHub pull request識別情報]
#   戻り値: [list[dict[str, object]]: name・state・bucketを検証済みのstatus check一覧]
#   エラー: [MergeGateError: API・JSON・項目形式が不正、checkが0件、または1件以上がpass以外]
# }
def _read_status_checks(pull_request: PullRequestReference) -> list[dict[str, object]]:
    command_result = _run_successful_gh_command(
        ["pr", "checks", pull_request.url, "--json", "name,state,bucket,link,workflow"],
        API_COMMAND_TIMEOUT_SECONDS,
        "Pull request status check lookup",
    )
    response_data = _decode_json_output(command_result, "Pull request status check lookup")
    if not isinstance(response_data, list):
        raise MergeGateError("Pull request status check lookup returned an unexpected JSON shape.")
    if not response_data:
        raise MergeGateError("GitHub reported no status checks; PR merge was stopped because CI is unverified.")

    invalid_checks: list[str] = []
    validated_checks: list[dict[str, object]] = []
    for check in response_data:
        if not isinstance(check, dict):
            raise MergeGateError("Pull request status check lookup returned an invalid check entry.")
        check_name = check.get("name")
        check_state = check.get("state")
        check_bucket = check.get("bucket")
        if not isinstance(check_name, str) or not isinstance(check_state, str) or not isinstance(check_bucket, str):
            raise MergeGateError("Pull request status check entry omitted its name, state, or bucket.")
        validated_checks.append(check)
        if check_bucket != "pass":
            invalid_checks.append(f"{check_name}: {check_bucket} ({check_state})")

    if invalid_checks:
        details = "; ".join(invalid_checks)
        raise MergeGateError(
            "Every status check must pass; pending, failed, cancelled, skipped, and unknown checks "
            f"block merge. Current non-passing checks: {details}"
        )

    print(f"[OK] All {len(validated_checks)} reported status checks passed.", flush=True)
    return validated_checks


# {
#   責務: [_decode_review_thread_page: GraphQLの1ページを検証し、そのページの未解決review指摘とpagination状態を取り出す]
#   処理: [PRがOPENであること、各threadの解決状態・位置・最初のcomment、次ページ情報の型を確認する]
#   引数: [response_data: gh api graphqlが返したJSON値]
#   戻り値: [tuple[list[UnresolvedReviewThread], bool, str | None]: 未解決指摘一覧、次ページ有無、次ページcursor]
#   エラー: [MergeGateError: GraphQLエラー、必須項目欠落、または想定外のJSON形状]
# }
def _decode_review_thread_page(
    response_data: object,
) -> tuple[list[UnresolvedReviewThread], bool, str | None]:
    if not isinstance(response_data, dict) or response_data.get("errors"):
        raise MergeGateError("GitHub review-thread API returned an error; PR merge was stopped.")

    data = response_data.get("data")
    if not isinstance(data, dict):
        raise MergeGateError("GitHub review-thread API omitted response data; PR merge was stopped.")
    repository_data = data.get("repository")
    if not isinstance(repository_data, dict):
        raise MergeGateError("GitHub review-thread API could not resolve the repository; PR merge was stopped.")
    pull_request_data = repository_data.get("pullRequest")
    if not isinstance(pull_request_data, dict):
        raise MergeGateError("GitHub review-thread API could not resolve the pull request; PR merge was stopped.")
    if pull_request_data.get("state") != "OPEN":
        raise MergeGateError("GitHub review-thread API did not confirm an open pull request; PR merge was stopped.")

    thread_connection = pull_request_data.get("reviewThreads")
    if not isinstance(thread_connection, dict):
        raise MergeGateError("GitHub review-thread API omitted the thread list; PR merge was stopped.")
    thread_nodes = thread_connection.get("nodes")
    page_info = thread_connection.get("pageInfo")
    if not isinstance(thread_nodes, list) or not isinstance(page_info, dict):
        raise MergeGateError("GitHub review-thread API returned an invalid thread page; PR merge was stopped.")

    unresolved_threads: list[UnresolvedReviewThread] = []
    for thread in thread_nodes:
        if not isinstance(thread, dict) or not isinstance(thread.get("isResolved"), bool):
            raise MergeGateError("GitHub review-thread API returned an invalid thread status; PR merge was stopped.")
        if thread["isResolved"]:
            continue

        path = thread.get("path")
        line = thread.get("line")
        if line is None:
            line = thread.get("originalLine")
        if not isinstance(path, str) or (line is not None and not isinstance(line, int)):
            raise MergeGateError("GitHub returned an unresolved thread without a valid location.")

        comments = thread.get("comments")
        comment_nodes = comments.get("nodes") if isinstance(comments, dict) else None
        if not isinstance(comment_nodes, list) or not comment_nodes or not isinstance(comment_nodes[0], dict):
            raise MergeGateError("GitHub returned an unresolved thread without its review comment content.")
        first_comment = comment_nodes[0]
        body = first_comment.get("body")
        comment_url = first_comment.get("url")
        author_data = first_comment.get("author")
        author = author_data.get("login") if isinstance(author_data, dict) else None
        if not isinstance(body, str) or not isinstance(comment_url, str) or not isinstance(author, str):
            raise MergeGateError("GitHub returned an unresolved review comment with incomplete details.")
        unresolved_threads.append(UnresolvedReviewThread(path, line, author, body, comment_url))

    has_next_page = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next_page, bool):
        raise MergeGateError("GitHub review-thread API omitted pagination status; PR merge was stopped.")
    if end_cursor is not None and not isinstance(end_cursor, str):
        raise MergeGateError("GitHub review-thread API returned an invalid pagination cursor; PR merge was stopped.")
    return unresolved_threads, has_next_page, end_cursor


# {
#   責務: [_read_unresolved_review_threads: PRのinline review threadを全ページ取得し、未解決の指摘だけを返す]
#   処理: [GraphQL cursor paginationを継続し、ページが進まない・上限を超える・API応答を検証できない場合はfail closedで停止する]
#   引数: [pull_request: review threadを確認するGitHub pull request識別情報]
#   戻り値: [list[UnresolvedReviewThread]: 全ページから集めた未解決inline review指摘]
#   エラー: [MergeGateError: GitHub APIまたはpagination応答を完全に確認できない]
# }
def _read_unresolved_review_threads(
    pull_request: PullRequestReference,
) -> list[UnresolvedReviewThread]:
    cursor: str | None = None
    all_unresolved_threads: list[UnresolvedReviewThread] = []

    for _ in range(MAX_REVIEW_THREAD_PAGES):
        command_arguments = [
            "api",
            "graphql",
            "-f",
            f"query={REVIEW_THREADS_QUERY}",
            "-F",
            f"owner={pull_request.owner}",
            "-F",
            f"name={pull_request.repository}",
            "-F",
            f"number={pull_request.number}",
        ]
        if cursor is not None:
            command_arguments.extend(["-F", f"cursor={cursor}"])

        command_result = _run_successful_gh_command(
            command_arguments,
            API_COMMAND_TIMEOUT_SECONDS,
            "Pull request review-thread lookup",
        )
        response_data = _decode_json_output(command_result, "Pull request review-thread lookup")
        page_unresolved_threads, has_next_page, next_cursor = _decode_review_thread_page(response_data)
        all_unresolved_threads.extend(page_unresolved_threads)
        if not has_next_page:
            return all_unresolved_threads
        if not next_cursor or next_cursor == cursor:
            raise MergeGateError("GitHub review-thread pagination did not advance; PR merge was stopped.")
        cursor = next_cursor

    raise MergeGateError(
        f"GitHub returned more than {MAX_REVIEW_THREAD_PAGES} review-thread pages; "
        "PR merge was stopped because the full review state could not be confirmed."
    )


# {
#   責務: [_stop_for_unresolved_review_threads: 未解決inline review指摘を修正に必要な位置・投稿者・本文とともに表示してmergeを止める]
#   処理: [指摘が0件なら追加のKadoka reviewを待たずに続行し、指摘があれば全件をstderrへ表示する]
#   引数: [threads: GitHubから取得した未解決inline review thread一覧]
#   戻り値: [なし: 指摘が0件の場合だけ呼び出し元へ戻る]
#   エラー: [MergeGateError: 1件以上の未解決inline review threadがある]
# }
def _stop_for_unresolved_review_threads(threads: list[UnresolvedReviewThread]) -> None:
    if not threads:
        print("[OK] No unresolved inline review comments were found; no additional Kadoka review is required.")
        return

    print("[STOP] Resolve or address every inline review thread before merging:", file=sys.stderr)
    for thread in threads:
        location = f"{thread.path}:{thread.line}" if thread.line is not None else thread.path
        displayed_body = thread.body[:MAX_DISPLAYED_TEXT_LENGTH]
        if len(thread.body) > MAX_DISPLAYED_TEXT_LENGTH:
            displayed_body += "..."
        print(f"- {location} (@{thread.author}) {thread.url}: {displayed_body!r}", file=sys.stderr)
    raise MergeGateError("Unresolved inline review comments block PR merge.")


# {
#   責務: [merge_pull_request_when_safe: PR状態、CI、未解決inline review、head commitを順に確認し、安全条件を満たした同一commitだけmergeする]
#   処理: [PR URL検証、初期snapshot取得、全status check待機と再検証、全review thread確認、head SHA再確認後にmatch-head-commit付きmergeを行う]
#   引数: [pull_request_url: merge対象としてfinish_pr.batが選んだGitHub pull request URL]
#   戻り値: [なし: gateとmergeがすべて成功した場合に完了する]
#   エラー: [MergeGateError: PR状態、CI、review、head commit、GitHub CLIまたはmergeのいずれかが安全条件を満たさない]
# }
def merge_pull_request_when_safe(pull_request_url: str) -> None:
    pull_request = parse_pull_request_reference(pull_request_url)
    initial_snapshot = _read_pull_request_snapshot(pull_request)
    _wait_for_status_checks(pull_request)
    _read_status_checks(pull_request)
    unresolved_threads = _read_unresolved_review_threads(pull_request)
    _stop_for_unresolved_review_threads(unresolved_threads)

    current_snapshot = _read_pull_request_snapshot(pull_request)
    if current_snapshot.head_sha != initial_snapshot.head_sha:
        raise MergeGateError(
            "Pull request head changed while CI or review status was being checked; "
            "PR merge was stopped and the checks must be repeated."
        )

    print("[WAIT] All checks passed and no unresolved inline review comments remain; merging the PR.", flush=True)
    _run_successful_gh_command(
        [
            "pr",
            "merge",
            pull_request.url,
            "--merge",
            "--match-head-commit",
            current_snapshot.head_sha,
            "--delete-branch",
        ],
        API_COMMAND_TIMEOUT_SECONDS,
        "Pull request merge",
    )
    print("[DONE] Pull request merged after CI and review-thread checks passed.")


# {
#   責務: [main: CLI引数のPR URLにmerge gateを適用し、成功または安全停止をプロセス終了codeで表す]
#   処理: [必須URLをparseし、gate成功時は0、MergeGateError発生時は理由をstderrへ出して3を返す]
#   引数: [arguments: テストから渡すCLI引数。Noneの場合はargparseが実プロセス引数を読む]
#   戻り値: [int: merge成功なら0、安全停止なら3]
# }
def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wait for every GitHub PR status check, reject unresolved inline review threads, "
            "and merge only the verified head commit."
        )
    )
    parser.add_argument("pull_request_url", help="Full HTTPS URL of the pull request to check and merge")
    parsed_arguments = parser.parse_args(arguments)

    try:
        merge_pull_request_when_safe(parsed_arguments.pull_request_url)
    except MergeGateError as error:
        print(f"[STOP] {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
