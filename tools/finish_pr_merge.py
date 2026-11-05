from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

try:
    from tools.pr_workflow_evidence import REQUIRED_WORKFLOW_PATHS, WorkflowRunGate, WorkflowEvidenceError
except ModuleNotFoundError:
    from pr_workflow_evidence import REQUIRED_WORKFLOW_PATHS, WorkflowRunGate, WorkflowEvidenceError

API_COMMAND_TIMEOUT_SECONDS = 60
CHECK_COMMAND_TIMEOUT_SECONDS = 60 * 60
MAX_REVIEW_THREAD_PAGES = 100
MAX_DISPLAYED_TEXT_LENGTH = 2000
# Waits before the second and third attempt of a read-only GitHub lookup that failed with a
# transient network error. The merge command itself is never repeated.
READ_ONLY_RETRY_DELAYS_SECONDS = (10, 30)
MINIMUM_RETRY_BUDGET_SECONDS = 1
# Lower-case fragments of gh/Go network errors and GitHub gateway failures that are worth a
# bounded retry. Authentication, permission, rate-limit (403/429), 404 and GraphQL errors are
# deliberately absent so they stop immediately.
TRANSIENT_GITHUB_FAILURE_MARKERS = (
    "tls handshake timeout",
    "i/o timeout",
    "timeout awaiting response headers",
    "connection reset",
    "connection refused",
    "unexpected eof",
    "temporary failure in name resolution",
    "no such host",
    "http 502",
    "http 503",
    "http 504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)
_sleep = time.sleep
_monotonic = time.monotonic
GIT_SHA1_HEX_LENGTH = 40
GIT_SHA256_HEX_LENGTH = 64
PR_HEAD_SHA_PATTERN = re.compile(
    rf"^(?:[0-9a-f]{{{GIT_SHA1_HEX_LENGTH}}}|[0-9a-f]{{{GIT_SHA256_HEX_LENGTH}}})$",
    re.IGNORECASE,
)
# Checks posted by optional AI review apps (not by a GitHub Actions workflow). They are
# not required checks (doc/main保護方針.md) and report "skipping" when the free quota
# is exhausted, so only that outcome is tolerated; a failure still blocks the merge.
ADVISORY_REVIEW_CHECK_NAMES = frozenset({"CodeRabbit", "Sourcery review"})
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
#   責務: [
#     PullRequestReference: GitHub pull request URLとAPI照会用の識別情報を一つの値として保持する
#   ],
#   保持状態: [
#     url: GitHub APIとCLIへ渡す正規化済みHTTPS URL,
#     owner: リポジトリを所有するGitHubアカウントまたは組織,
#     repository: API照会対象のリポジトリ名,
#     number: APIが特定する正のpull request番号
#   ]
# }
@dataclass(frozen=True)
class PullRequestReference:
    url: str
    owner: str
    repository: str
    number: int


# {
#   責務: [
#     PullRequestSnapshot: CI・review確認の対象PRについて、merge前に検証した状態を一組で保持する
#   ],
#   保持状態: [
#     state: GitHubが返したPR状態。merge gateではOPENだけを受け入れる,
#     mergeable: GitHubが返した競合状態。merge gateではMERGEABLEだけを受け入れる,
#     head_sha: CIとreview確認を固定するPR先端commitのSHA,
#     head_ref: push Workflow Runを同名branchのrunだけに限定するPR head branch名,
#     is_draft: GitHubが返したDraft状態。merge gateではFalseだけを受け入れる
#   ]
# }
@dataclass(frozen=True)
class PullRequestSnapshot:
    state: str
    mergeable: str
    head_sha: str
    head_ref: str = ""
    is_draft: bool = False


# {
#   責務: [
#     UnresolvedReviewThread: mergeを止めたinline review指摘を、修正場所と投稿内容が分かる形で保持する
#   ],
#   保持状態: [
#     path: 指摘対象ファイルのリポジトリ内パス,
#     line: 指摘行。GitHubが行番号を返さない場合はNone,
#     author: 指摘を投稿したGitHub利用者名,
#     body: 未解決thread内の最初のreviewコメント本文,
#     url: 利用者が指摘を開くためのGitHub URL
#   ]
# }
@dataclass(frozen=True)
class UnresolvedReviewThread:
    path: str
    line: int | None
    author: str
    body: str
    url: str


# {
#   責務: [
#     MergeGateError: PR状態・CI・reviewを安全に確認できずmergeを中止した理由をCLIへ伝える
#   ],
#   保持状態: [
#     args: RuntimeErrorが保持する、利用者の次の確認を決める停止理由
#   ]
# }
class MergeGateError(RuntimeError):
    pass


class GitHubCommandTimeout(MergeGateError):
    """GitHub CLIが制限時間内に終わらなかった。読取専用照会では一時的失敗として再試行できる。"""


class MergeNotPerformedError(MergeGateError):
    """merge後のREST確認で、同じheadのPRがopenかつ未mergeのままだと分かった。"""


# {
#   責務: [
#     parse_pull_request_reference: 入力URLがGitHub上の単一PRを指すことを検証し、API照会用の識別情報へ変換する
#   ],
#   処理: [
#     1: URLを解析し、HTTPSのgithub.com以外や認証情報・port・query・fragment付きURLを拒否する,
#     2: pathからowner・repository・PR番号を分離し、正の番号であることを確認する,
#     3: 正規化URLとGitHub照会用識別情報を返す
#   ],
#   引数: [
#     pull_request_url: finish_pr.batが現在のbranchのPRから取得したURL
#   ],
#   戻り値: [
#     PullRequestReference: canonical URLとowner・repository・番号をまとめた照会対象
#   ],
#   エラー: [
#     MergeGateError: URLのhost・path・PR番号が安全な照会条件を満たさない
#   ]
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
#   責務: [
#     _run_gh: GitHub CLIを個別引数で実行し、shell解釈を介さず実行結果を呼び出し元へ返す
#   ],
#   処理: [
#     1: ghと引数配列を起動し、UTF-8の標準出力・標準エラーを取得する,
#     2: 指定秒数を超えた実行を打ち切り、OS実行失敗とともに停止理由へ変換する
#   ],
#   引数: [
#     arguments: ghへ渡す個別のCLI引数。shell文字列として再解釈しない,
#     timeout_seconds: commandの完了を待つ上限秒数
#   ],
#   戻り値: [
#     CompletedProcess[str]: 標準出力・標準エラー・終了codeを含む実行結果
#   ],
#   エラー: [
#     MergeGateError: ghがPATHにない、OSが起動できない、または制限時間を超過する
#   ]
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
        raise GitHubCommandTimeout(
            f"GitHub CLI did not finish within {timeout_seconds} seconds; PR merge was stopped."
        ) from error
    except OSError as error:
        raise MergeGateError(f"GitHub CLI could not run ({error}); PR merge was stopped.") from error


# {
#   責務: [
#     _command_details: GitHub CLI失敗時の診断文を選び、利用者に表示できる長さへ整える
#   ],
#   処理: [
#     1: stderrを優先し、stderrが空ならstdoutを選ぶ,
#     2: 前後の空白を除き、最大MAX_DISPLAYED_TEXT_LENGTH文字の診断文を残す,
#     3: 標準出力と標準エラーが空なら固定の診断文を返す
#   ],
#   引数: [
#     command_result: _run_ghが返したGitHub CLIの実行結果
#   ],
#   戻り値: [
#     str: trim済みで表示上限内の診断文
#   ]
# }
def _command_details(command_result: subprocess.CompletedProcess[str]) -> str:
    command_text = (command_result.stderr or command_result.stdout or "").strip()
    if not command_text:
        return "GitHub CLI returned no diagnostic text."
    if len(command_text) > MAX_DISPLAYED_TEXT_LENGTH:
        return command_text[:MAX_DISPLAYED_TEXT_LENGTH] + "..."
    return command_text


def _is_transient_github_failure(diagnostic_text: str) -> bool:
    """診断文がTLS handshake timeout等の一時的な通信失敗・gateway障害を示すならTrue。"""
    lowered_text = diagnostic_text.lower()
    return any(marker in lowered_text for marker in TRANSIENT_GITHUB_FAILURE_MARKERS)


def _run_read_only_gh_command(
    arguments: list[str],
    timeout_seconds: int,
    action_description: str,
    retry_timeouts: bool = True,
    share_timeout_budget: bool = False,
) -> subprocess.CompletedProcess[str]:
    """読取専用のGitHub CLI照会を実行し、一時的な通信失敗だけを間隔付きで有界回数再試行する。

    試行は最大`len(READ_ONLY_RETRY_DELAYS_SECONDS) + 1`回。認証・権限・rate limit・GraphQLの
    失敗は即座に停止する。`retry_timeouts`がFalseならtimeoutは再試行しない。`share_timeout_budget`
    では最初の試行開始から`timeout_seconds`を全試行の共通予算とし、各試行へ残り時間を渡す。
    mergeなど状態を変える呼出しには使わない。
    """
    max_attempts = len(READ_ONLY_RETRY_DELAYS_SECONDS) + 1
    deadline = _monotonic() + timeout_seconds if share_timeout_budget else None
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout_seconds
        if deadline is not None:
            attempt_timeout = int(deadline - _monotonic())
            if attempt_timeout < MINIMUM_RETRY_BUDGET_SECONDS:
                raise MergeGateError(f"{action_description} has no time budget left (shared {timeout_seconds}s budget)")
        try:
            command_result = _run_gh(arguments, attempt_timeout)
        except GitHubCommandTimeout as error:
            if not retry_timeouts:
                raise
            diagnostic = str(error)
            failure = MergeGateError(f"{action_description} failed: {diagnostic}")
        else:
            if command_result.returncode == 0:
                return command_result
            diagnostic = _command_details(command_result)
            failure = MergeGateError(
                f"{action_description} failed with GitHub CLI exit code {command_result.returncode}: {diagnostic}"
            )
            if not _is_transient_github_failure(diagnostic):
                raise failure
        if attempt == max_attempts:
            raise MergeGateError(f"{failure} (transient GitHub error persisted after {max_attempts} attempts)")
        delay_seconds = READ_ONLY_RETRY_DELAYS_SECONDS[attempt - 1]
        if deadline is not None and deadline - _monotonic() - delay_seconds < MINIMUM_RETRY_BUDGET_SECONDS:
            raise MergeGateError(f"{failure} (no time budget left for a retry)")
        print(
            f"[RETRY] {action_description} hit a transient GitHub error ({diagnostic.splitlines()[0][:200]}); "
            f"retrying in {delay_seconds}s (attempt {attempt + 1}/{max_attempts}).",
            flush=True,
        )
        _sleep(delay_seconds)
    raise AssertionError("unreachable")


# {
#   責務: [
#     _decode_json_output: GitHub CLIの標準出力をJSONとして読み、呼び出し側が形状を検証できる値へ変換する
#   ],
#   処理: [
#     1: 標準出力をJSON parserへ渡す,
#     2: JSON構文が不正なら操作名を含めてmerge停止理由にする
#   ],
#   引数: [
#     command_result: JSONを標準出力へ返したGitHub CLI実行結果,
#     action_description: 解析失敗に表示する操作名
#   ],
#   戻り値: [
#     object: JSON object・array・scalarのいずれか。具体的な形状は呼び出し側が検証する
#   ],
#   エラー: [
#     MergeGateError: 標準出力が有効なJSONではない
#   ]
# }
def _decode_json_output(command_result: subprocess.CompletedProcess[str], action_description: str) -> object:
    try:
        return json.loads(command_result.stdout)
    except json.JSONDecodeError as error:
        raise MergeGateError(f"{action_description} returned invalid JSON; PR merge was stopped.") from error


# {
#   責務: [
#     _read_pull_request_snapshot: GitHubからPR状態を取得し、OPEN・MERGEABLE・有効なhead SHAを確認する
#   ],
#   処理: [
#     1: gh pr viewからstate・isDraft・mergeable・headRefOid・headRefNameをJSONで取得する。一時的な通信失敗は有界回数再試行する,
#     2: 5項目の存在と型を確認し、stateがOPEN、Draftでない、mergeabilityがMERGEABLEであることをこの順に確かめる,
#     3: headRefOidがSHA-1またはSHA-256形式なら検証済みsnapshotを返す
#   ],
#   引数: [
#     pull_request: 状態を確認するGitHub pull request識別情報
#   ],
#   戻り値: [
#     PullRequestSnapshot: merge gateが検証したPR状態とhead commit
#   ],
#   エラー: [
#     MergeGateError: API失敗、JSON不正、PR終了、Draft、merge不可、head SHA不正、またはhead branch名の欠落
#   ]
# }
def _read_pull_request_snapshot(pull_request: PullRequestReference) -> PullRequestSnapshot:
    command_result = _run_read_only_gh_command(
        ["pr", "view", pull_request.url, "--json", "state,isDraft,mergeable,headRefOid,headRefName"],
        API_COMMAND_TIMEOUT_SECONDS,
        "Pull request state lookup",
    )
    response_data = _decode_json_output(command_result, "Pull request state lookup")
    if not isinstance(response_data, dict):
        raise MergeGateError("Pull request state lookup returned an unexpected JSON shape.")

    state = response_data.get("state")
    mergeability = response_data.get("mergeable")
    head_sha = response_data.get("headRefOid")
    head_ref = response_data.get("headRefName")
    is_draft = response_data.get("isDraft")
    if not isinstance(state, str) or not isinstance(mergeability, str) or not isinstance(head_sha, str):
        raise MergeGateError("Pull request state lookup omitted state, mergeability, or head commit.")
    if not isinstance(head_ref, str) or not head_ref.strip():
        raise MergeGateError("Pull request state lookup omitted the head branch; PR merge was stopped.")
    if state != "OPEN":
        raise MergeGateError(f"Pull request state is {state!r}, not OPEN; PR merge was stopped.")
    if not isinstance(is_draft, bool):
        raise MergeGateError("Pull request state lookup omitted the draft status; PR merge was stopped.")
    if is_draft:
        raise MergeGateError(
            "Pull request is a draft; mark it ready for review on GitHub before running the merge gate "
            "(this tool does not change draft status). PR merge was stopped."
        )
    if mergeability != "MERGEABLE":
        raise MergeGateError(
            f"Pull request mergeability is {mergeability!r}, not MERGEABLE; PR merge was stopped."
        )
    if PR_HEAD_SHA_PATTERN.fullmatch(head_sha) is None:
        raise MergeGateError("Pull request head commit is missing or invalid; PR merge was stopped.")

    return PullRequestSnapshot(state, mergeability, head_sha, head_ref, is_draft)


# {
#   責務: [
#     _wait_for_status_checks: PR status checkが成功するか失敗を検出するまで待機し、非成功をmerge停止理由にする
#   ],
#   処理: [
#     1: gh pr checks --watch --fail-fastを実行し、最大CHECK_COMMAND_TIMEOUT_SECONDS秒待つ。一時的な通信失敗だけを同じ予算内で再試行し、timeoutは再試行しない,
#     2: ghが失敗またはtimeoutした場合はmergeを中止する
#   ],
#   引数: [
#     pull_request: status checkを待つGitHub pull request識別情報
#   ],
#   戻り値: [
#     なし: ghが成功を返した場合だけ呼び出し元へ戻る
#   ],
#   エラー: [
#     MergeGateError: check失敗、認証/APIエラー、gh起動失敗、または待機timeout。対象PRとcheck一覧の確認commandを表示する
#   ]
# }
def _wait_for_status_checks(pull_request: PullRequestReference) -> None:
    print("[WAIT] GitHub Actions checks are running; waiting for every check to finish.", flush=True)
    try:
        _run_read_only_gh_command(
            ["pr", "checks", pull_request.url, "--watch", "--fail-fast"],
            CHECK_COMMAND_TIMEOUT_SECONDS,
            "Waiting for pull request status checks",
            retry_timeouts=False,
            share_timeout_budget=True,
        )
    except MergeGateError as error:
        raise MergeGateError(
            f"Status checks did not finish successfully for {pull_request.url}; merge was stopped. "
            f"Inspect the checks with 'gh pr checks {pull_request.url}' before retrying. {error}"
        ) from error


# {
#   責務: [
#     _read_status_checks: GitHubの全PR status checkを検証し、すべてpassの場合に限って一覧を返す
#   ],
#   処理: [
#     1: gh pr checksのJSON配列を取得し、空配列でないことを確かめる,
#     2: 各要素のname・state・bucketが文字列であることを確認する,
#     3: pass以外のbucketがあればcheck名と状態を示して停止する。ただし、workflowを持たない任意AIレビューapp（ADVISORY_REVIEW_CHECK_NAMES）のskippingだけは、無料枠切れ等として記録して停止しない,
#     4: 停止条件に当たらなければ検証済み一覧を返す
#   ],
#   引数: [
#     pull_request: status check一覧を取得するGitHub pull request識別情報
#   ],
#   戻り値: [
#     list[dict[str, object]]: name・state・bucketを検証したstatus check一覧
#   ],
#   エラー: [
#     MergeGateError: API・JSON・項目形式が不正、checkが0件、または1件以上がpass以外
#   ]
# }
def _read_status_checks(pull_request: PullRequestReference) -> list[dict[str, object]]:
    command_result = _run_read_only_gh_command(
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
    advisory_skipped: list[str] = []
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
        if check_bucket == "pass":
            continue
        # An Actions job could reuse an advisory name; only app checks that report an empty
        # workflow qualify. A missing or non-string workflow field is not trusted.
        if check_name in ADVISORY_REVIEW_CHECK_NAMES and check_bucket == "skipping" and check.get("workflow") == "":
            advisory_skipped.append(f"{check_name}: {check_bucket} ({check_state})")
            continue
        invalid_checks.append(f"{check_name}: {check_bucket} ({check_state})")

    if invalid_checks:
        details = "; ".join(invalid_checks)
        raise MergeGateError(
            "Every status check must pass; pending, failed, cancelled, skipped, and unknown checks "
            f"block merge. Current non-passing checks: {details}"
        )

    if advisory_skipped:
        print(
            "[INFO] Optional AI review checks did not run (for example, free quota exhausted): "
            + "; ".join(advisory_skipped),
            flush=True,
        )
        passed_count = len(validated_checks) - len(advisory_skipped)
        if passed_count == 0:
            raise MergeGateError("Only optional AI review checks were reported; CI is unverified and merge was stopped.")
        print(f"[OK] All {passed_count} other reported status checks passed.", flush=True)
        return validated_checks
    print(f"[OK] All {len(validated_checks)} reported status checks passed.", flush=True)
    return validated_checks


# {
#   責務: [
#     _decode_review_thread_page: GraphQL応答の1ページを検証し、未解決指摘とpagination状態を取り出す
#   ],
#   処理: [
#     1: repositoryとPRが取得でき、PR stateがOPENであることを確認する,
#     2: 各threadの解決状態を確認し、未解決threadの位置・投稿者・本文・URLを検証する,
#     3: 次ページ有無とcursorの型を確認して検証済み情報を返す
#   ],
#   引数: [
#     response_data: gh api graphqlが返した1ページ分のJSON値
#   ],
#   戻り値: [
#     tuple[list[UnresolvedReviewThread], bool, str | None]: 未解決指摘、次ページ有無、次ページcursor
#   ],
#   エラー: [
#     MergeGateError: GraphQLエラー、必須項目欠落、または想定外のJSON形状
#   ]
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
#   責務: [
#     _read_unresolved_review_threads: PRのinline review threadを全ページ取得し、未解決指摘だけを返す
#   ],
#   処理: [
#     1: GraphQL APIをcursor paginationで呼び出してthreadページを取得する,
#     2: 各ページを検証し、未解決threadだけを一覧に加える,
#     3: 最終ページまで取得できた場合に一覧を返す,
#     4: cursorが進まない、ページ上限を超える、または応答を検証できない場合は停止する
#   ],
#   引数: [
#     pull_request: review threadを確認するGitHub pull request識別情報
#   ],
#   戻り値: [
#     list[UnresolvedReviewThread]: 全ページから取得した未解決inline review指摘
#   ],
#   エラー: [
#     MergeGateError: GitHub APIまたはpagination応答を完全に確認できない
#   ]
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

        command_result = _run_read_only_gh_command(
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
#   責務: [
#     _stop_for_unresolved_review_threads: 未解決inline review指摘を修正に必要な情報とともに表示してmergeを止める
#   ],
#   処理: [
#     1: 指摘一覧が空なら追加のKadoka reviewを待たずに呼び出し元へ戻る,
#     2: 指摘があればファイル・行・投稿者・本文・URLをstderrへ表示して停止する
#   ],
#   引数: [
#     threads: GitHubから取得した未解決inline review thread一覧
#   ],
#   戻り値: [
#     なし: 指摘一覧が空の場合だけ呼び出し元へ戻る
#   ],
#   エラー: [
#     MergeGateError: 1件以上の未解決inline review threadがある
#   ]
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


def _require_workflow_runs(pull_request: PullRequestReference, head_sha: str, head_ref: str) -> None:
    """check一覧と別に、固定headのpush/PR必須workflowの最新attemptをREST APIで検証する。

    push runはPRのhead branch名と一致するものだけを使い、同じcommitのtag runで失敗を隠さない。
    """
    def read_json(endpoint: str) -> object:
        result = _run_read_only_gh_command(
            ["api", "--method", "GET", endpoint], API_COMMAND_TIMEOUT_SECONDS, "Workflow Run lookup"
        )
        return _decode_json_output(result, "Workflow Run lookup")

    try:
        WorkflowRunGate(
            f"{pull_request.owner}/{pull_request.repository}", pull_request.number, head_sha, head_ref, read_json
        ).verify()
    except WorkflowEvidenceError as error:
        raise MergeGateError(str(error)) from error
    print(
        f"[OK] Required {len(REQUIRED_WORKFLOW_PATHS)} workflows passed for both push and pull_request events.",
        flush=True,
    )


# {
#   責務: [
#     merge_pull_request_when_safe: PR状態・CI・inline reviewを確認し、安全条件を満たした同一commitだけmergeする
#   ],
#   処理: [
#     1: PR URLを検証し、初期状態とhead commitを記録する,
#     2: status checkの完了を待ち、すべてpassであることを再確認する,
#     3: 全inline review threadを取得し、未解決指摘がないことを確認する,
#     4: 固定headのpush（PR head branchのrunだけ）・PR各eventで必須workflowの最新attemptがすべて成功したことをREST APIで検証する,
#     5: PR状態とhead commitが確認開始時から変わっていないことを確かめる,
#     6: 確認済みhead SHAを--match-head-commitへ渡し、ローカルbranch削除を伴わずmergeする,
#     7: CLI終了値にかかわらずREST APIで同じPRとheadの統合済み状態を確認する。mergeは再実行しない,
#     8: CLIが本文付きで非0終了し、APIが同じheadのopen・未mergeを返したらGitHubの拒否本文を表示して停止する。それ以外の未確認は結果不明として停止する
#   ],
#   引数: [
#     pull_request_url: finish_pr.batが現在のbranchから取得したmerge対象PRのURL
#   ],
#   戻り値: [
#     なし: gateとmergeがすべて成功した場合に完了する
#   ],
#   エラー: [
#     MergeGateError: PR状態・CI・review・head commit・GitHub CLI・mergeが安全条件を満たさない
#   ]
# }
def merge_pull_request_when_safe(pull_request_url: str) -> None:
    pull_request = parse_pull_request_reference(pull_request_url)
    initial_snapshot = _read_pull_request_snapshot(pull_request)
    _wait_for_status_checks(pull_request)
    _read_status_checks(pull_request)
    unresolved_threads = _read_unresolved_review_threads(pull_request)
    _stop_for_unresolved_review_threads(unresolved_threads)
    _require_workflow_runs(pull_request, initial_snapshot.head_sha, initial_snapshot.head_ref)

    current_snapshot = _read_pull_request_snapshot(pull_request)
    if current_snapshot.head_sha != initial_snapshot.head_sha:
        raise MergeGateError(
            "Pull request head changed while CI or review status was being checked; "
            "PR merge was stopped and the checks must be repeated."
        )

    print("[WAIT] All checks passed and no unresolved inline review comments remain; merging the PR.", flush=True)
    merge_diagnostic = None
    github_rejected_merge = False
    try:
        merge_result = _run_gh(
            [
                "pr", "merge", pull_request.url, "--merge",
                "--match-head-commit", current_snapshot.head_sha,
            ],
            API_COMMAND_TIMEOUT_SECONDS,
        )
        if merge_result.returncode != 0:
            merge_diagnostic = _command_details(merge_result)
            # A network failure may hide a request GitHub already accepted, so only a non-transient error
            # text counts as GitHub rejecting the merge.
            github_rejected_merge = bool(
                (merge_result.stderr or merge_result.stdout or "").strip()
            ) and not _is_transient_github_failure(merge_diagnostic)
    except MergeGateError as error:
        merge_diagnostic = str(error)
    unconfirmed_prefix = f"Merge command failed: {merge_diagnostic}. " if merge_diagnostic is not None else ""
    try:
        merge_sha = _confirm_remote_merge(pull_request, current_snapshot.head_sha)
    except MergeGateError as error:
        # gh pr mergeが本文付きで非0終了したのはGitHubの同期的な拒否。timeout・起動失敗・0終了では
        # 遅延反映を排除できないため、open/未mergeでも結果不明として扱い、mergeは再実行しない。
        if isinstance(error, MergeNotPerformedError) and github_rejected_merge:
            raise MergeGateError(
                f"PR was not merged: {pull_request.url} is still open and unmerged after the merge command "
                f"failed with: {merge_diagnostic}. Fix the reported cause, then rerun the merge gate "
                "(it re-verifies the PR state before merging)."
            ) from error
        raise MergeGateError(
            f"{unconfirmed_prefix}Remote merge outcome is unconfirmed for {pull_request.url}; "
            "do not repeat the merge blindly. "
            f"Inspect: gh pr view {pull_request.url} --json state,headRefOid,mergeCommit. "
            f"Confirmation: {error}; merge command: {merge_diagnostic or 'exit code 0'}"
        ) from error
    if merge_diagnostic is not None:
        print(f"[WARN] Merge command reported an error, but API confirmed the fixed-head merge: {merge_diagnostic}")
    print(f"[MERGED] Remote PR merge confirmed: {merge_sha}. Local checkout/sync is a separate step.")


def _confirm_remote_merge(pull_request: PullRequestReference, expected_head: str) -> str:
    """PR番号・固定head・統合済み状態・merge SHAをRESTで検証し、merge SHAを返す。"""
    endpoint = f"repos/{pull_request.owner}/{pull_request.repository}/pulls/{pull_request.number}"
    response = _decode_json_output(
        _run_read_only_gh_command(["api", "--method", "GET", endpoint], API_COMMAND_TIMEOUT_SECONDS, "Merge confirmation"),
        "Merge confirmation",
    )
    if not isinstance(response, dict):
        raise MergeGateError("Merge confirmation did not return an object")
    head = response.get("head")
    merge_sha = response.get("merge_commit_sha")
    if (
        type(response.get("number")) is int
        and response["number"] == pull_request.number
        and response.get("state") == "open"
        and response.get("merged") is False
        and isinstance(head, dict)
        and head.get("sha") == expected_head
    ):
        raise MergeNotPerformedError("API reports the PR with the verified head is still open and unmerged")
    if (
        type(response.get("number")) is not int
        or response["number"] != pull_request.number
        or response.get("merged") is not True
        or response.get("state") != "closed"
        or not isinstance(head, dict)
        or head.get("sha") != expected_head
        or not isinstance(merge_sha, str)
        or PR_HEAD_SHA_PATTERN.fullmatch(merge_sha) is None
    ):
        raise MergeGateError("API did not confirm a closed, merged PR with the verified head and valid merge SHA")
    return merge_sha


# {
#   責務: [
#     main: CLIで受け取ったPR URLにmerge gateを適用し、結果をprocess終了codeで表す
#   ],
#   処理: [
#     1: argparseで必須のPR URLを読み取り、merge gateを呼び出す,
#     2: 成功時は終了code 0を返す,
#     3: MergeGateErrorの理由をstderrへ表示し、終了code 3を返す
#   ],
#   引数: [
#     arguments: テスト用CLI引数。Noneならargparseが現在のprocess引数を読む
#   ],
#   戻り値: [
#     int: merge成功なら0、安全停止なら3
#   ]
# }
def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wait for GitHub PR checks, require successful push and PR workflow runs, "
            "reject unresolved inline review threads, "
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
