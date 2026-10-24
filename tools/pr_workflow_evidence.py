"""固定PR headのpush・PR Workflow Runを独立に検証する。mergeや再実行は行わない。"""

from collections.abc import Callable
from datetime import datetime, timezone


REQUIRED_WORKFLOW_PATHS = frozenset(
    f".github/workflows/{name}.yml"
    for name in ("tests", "language-ci", "safety-ci", "fail-safe-ci", "bug-ci", "performance-ci", "upd-architecture-ci")
)
WORKFLOW_EVENTS = ("push", "pull_request")
PAGE_SIZE = 100
MAX_RUNS_PER_EVENT = 1000


class WorkflowEvidenceError(RuntimeError):
    """Workflow Runの証拠が不足・不正・未成功のためmergeを止める理由。"""


class WorkflowRunGate:
    """対象repo・PR・headと読み取り関数を保持し、必須7 workflow×2 eventを検証する。"""

    def __init__(self, repository: str, pr_number: int, head_sha: str, read_json: Callable[[str], object]) -> None:
        self.repository = repository
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.read_json = read_json

    def verify(self) -> None:
        """eventごとに全pageを読み、各pathの最新runの現在attemptが成功した場合だけ戻る。"""
        seen_ids: set[int] = set()
        for event in WORKFLOW_EVENTS:
            latest: dict[str, tuple[datetime, int, dict]] = {}
            for run in self._read_event(event):
                created = self._validate_run(run, event, seen_ids)
                path = run["path"]
                if path not in REQUIRED_WORKFLOW_PATHS:
                    continue
                candidate = (created, run["id"], run)
                if path not in latest or candidate[:2] > latest[path][:2]:
                    latest[path] = candidate
            missing = REQUIRED_WORKFLOW_PATHS - latest.keys()
            if missing:
                raise WorkflowEvidenceError(f"{event}: required workflow runs are missing: {', '.join(sorted(missing))}")
            for path, (_, run_id, run) in latest.items():
                if run.get("status") != "completed" or run.get("conclusion") != "success":
                    raise WorkflowEvidenceError(
                        f"{event}: {path} run {run_id} attempt {run['run_attempt']} is "
                        f"{run.get('status')!r}/{run.get('conclusion')!r}; merge stopped. "
                        f"Inspect https://github.com/{self.repository}/actions/runs/{run_id}"
                    )

    def _read_event(self, event: str) -> list[dict]:
        """100件ずつ取得し、件数変動・不完全page・1000件上限を成功扱いにしない。"""
        expected_count = None
        collected: list[dict] = []
        page = 1
        while True:
            endpoint = (
                f"repos/{self.repository}/actions/runs?head_sha={self.head_sha}"
                f"&event={event}&per_page={PAGE_SIZE}&page={page}"
            )
            payload = self.read_json(endpoint)
            if not isinstance(payload, dict):
                raise WorkflowEvidenceError(f"{event}: Workflow Run API did not return an object")
            count, runs = payload.get("total_count"), payload.get("workflow_runs")
            if type(count) is not int or not 0 <= count < MAX_RUNS_PER_EVENT or not isinstance(runs, list):
                raise WorkflowEvidenceError(f"{event}: invalid Workflow Run count/list or 1000-run limit reached")
            if expected_count is not None and count != expected_count:
                raise WorkflowEvidenceError(f"{event}: Workflow Run count changed during pagination; repeat checks")
            expected_count = count
            expected_page_size = min(PAGE_SIZE, count - len(collected))
            if len(runs) != expected_page_size or any(not isinstance(run, dict) for run in runs):
                raise WorkflowEvidenceError(f"{event}: incomplete or invalid Workflow Run page")
            collected.extend(runs)
            if len(collected) == count:
                return collected
            page += 1

    def _validate_run(self, run: dict, event: str, seen_ids: set[int]) -> datetime:
        """別head/event/repo/PRや重複IDを拒否し、最新runを決める作成時刻を返す。"""
        run_id, attempt = run.get("id"), run.get("run_attempt")
        if type(run_id) is not int or run_id <= 0 or run_id in seen_ids:
            raise WorkflowEvidenceError(f"{event}: invalid or duplicate Workflow Run ID")
        seen_ids.add(run_id)
        if type(attempt) is not int or attempt <= 0:
            raise WorkflowEvidenceError(f"{event}: invalid current run attempt")
        if run.get("head_sha") != self.head_sha or run.get("event") != event:
            raise WorkflowEvidenceError(f"{event}: Workflow Run belongs to another head/event")
        if not isinstance(run.get("path"), str) or not run["path"]:
            raise WorkflowEvidenceError(f"{event}: Workflow Run omitted its workflow path")
        for key in ("repository", "head_repository"):
            repository = run.get(key)
            full_name = repository.get("full_name") if isinstance(repository, dict) else None
            if not isinstance(full_name, str) or full_name.casefold() != self.repository.casefold():
                raise WorkflowEvidenceError(f"{event}: Workflow Run belongs to another repository")
        if event == "pull_request":
            associated = run.get("pull_requests")
            if not isinstance(associated, list) or not associated:
                raise WorkflowEvidenceError("pull_request: PR association is unavailable; merge stopped")
            matches = False
            for pr in associated:
                if not isinstance(pr, dict) or type(pr.get("number")) is not int or not isinstance(pr.get("head"), dict):
                    raise WorkflowEvidenceError("pull_request: invalid associated PR entry")
                if pr["number"] == self.pr_number and pr["head"].get("sha") == self.head_sha:
                    matches = True
            if not matches:
                raise WorkflowEvidenceError("pull_request: Workflow Run belongs to another PR")
        created_at = run.get("created_at")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if isinstance(created_at, str) else None
            if created is None or created.tzinfo is None:
                raise ValueError("timezone required")
            return created.astimezone(timezone.utc)
        except ValueError as error:
            raise WorkflowEvidenceError(f"{event}: invalid Workflow Run creation time") from error
