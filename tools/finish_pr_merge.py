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


@dataclass(frozen=True)
class PullRequestReference:
    url: str
    owner: str
    repository: str
    number: int


@dataclass(frozen=True)
class PullRequestSnapshot:
    state: str
    mergeable: str
    head_sha: str


@dataclass(frozen=True)
class UnresolvedReviewThread:
    path: str
    line: int | None
    author: str
    body: str
    url: str


class MergeGateError(RuntimeError):
    pass


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


def _command_details(command_result: subprocess.CompletedProcess[str]) -> str:
    command_text = (command_result.stderr or command_result.stdout or "").strip()
    if not command_text:
        return "GitHub CLI returned no diagnostic text."
    if len(command_text) > MAX_DISPLAYED_TEXT_LENGTH:
        return command_text[:MAX_DISPLAYED_TEXT_LENGTH] + "..."
    return command_text


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


def _decode_json_output(command_result: subprocess.CompletedProcess[str], action_description: str) -> object:
    try:
        return json.loads(command_result.stdout)
    except json.JSONDecodeError as error:
        raise MergeGateError(f"{action_description} returned invalid JSON; PR merge was stopped.") from error


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


def _wait_for_status_checks(pull_request: PullRequestReference) -> None:
    print("[WAIT] GitHub Actions checks are running; waiting for every check to finish.", flush=True)
    _run_successful_gh_command(
        ["pr", "checks", pull_request.url, "--watch", "--fail-fast"],
        CHECK_COMMAND_TIMEOUT_SECONDS,
        "Waiting for pull request status checks",
    )


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
