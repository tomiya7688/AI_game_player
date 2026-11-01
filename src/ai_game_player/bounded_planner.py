"""期限前の完全深さreportだけを採用する、停止可能な制限process planner。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
from types import MappingProxyType
from typing import Any

from ai_game_player.belief_contract import BeliefWeightKind
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_validation import belief_id
from ai_game_player.planning_contract import PlanningRegistration, SearchBudget
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_file_reader import read_planning_bytes
from ai_game_player.planning_result import PlanningCounters, PlanningResult
from ai_game_player.planning_worker_diagnostic import (
    collect_worker_diagnostic,
    new_parent_timing,
    mark_parent_timing,
    record_parent_io_error,
    merge_parent_timing,
)
from ai_game_player.planning_wire import (
    publish_planning_json,
    decode_planning_json,
    read_planning_json,
    result_from_dict,
)
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.world_state_hypothesis import HypothesisOrigin


# {
#   責務: [BoundedExpectimaxPlanner: 純粋な探索callbackを親の停止/観測/メモリ境界から分離する]
#   フィールド: [registrations: trustedコードだけが作るfactory登録, clock_id: 観測と共有する親monotonic origin,
#     controller: 既存Run世代, _busy: 同時workerを一つへ制限, _poisoned: 回収不能worker後に新規起動しない,
#     last_worker_diagnostic: 有界固定metadata。入力許可や学習factに使用しない]
# }
class BoundedExpectimaxPlanner:
    # {
    #   責務: [__init__: trusted登録を不変化しRunControllerと同時worker上限を接続する]
    #   引数: [registrations, clock_id, controller] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def __init__(
        self,
        registrations: tuple[PlanningRegistration, ...],
        *,
        clock_id: str,
        controller: RunController | None = None,
    ) -> None:
        if (
            type(registrations) is not tuple
            or len(registrations) > 128
            or any(not isinstance(item, PlanningRegistration) for item in registrations)
            or len({item.registration_id for item in registrations})
            != len(registrations)
        ):
            raise ValueError("planning registration must be a bounded trusted tuple")
        self.registrations = MappingProxyType(
            {item.registration_id: item for item in registrations}
        )
        self.clock_id = belief_id(clock_id)
        self.controller = controller or RunController()
        self._busy = threading.Lock()
        self._poisoned = False
        self.last_worker_diagnostic: dict[str, Any] | None = None

    # {
    #   責務: [plan: 観測・許可候補・予算を検査し隔離workerの完全深さ結果を返す]
    #   引数: [belief, allowed_action_ids, registration_id, budget, current_belief] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def plan(
        self,
        belief: BeliefSnapshot[Any],
        allowed_action_ids: tuple[str, ...],
        *,
        registration_id: str,
        budget: SearchBudget | None = None,
        current_belief: Callable[[], BeliefSnapshot[Any] | None] | None = None,
    ) -> PlanningResult:
        self.last_worker_diagnostic = None
        if not isinstance(belief, BeliefSnapshot):
            raise ValueError("planner requires a typed belief snapshot")
        if (
            type(allowed_action_ids) is not tuple
            or len(allowed_action_ids) > 32
            or len(set(allowed_action_ids)) != len(allowed_action_ids)
        ):
            raise ValueError("planner requires bounded unique verified action IDs")
        for action in allowed_action_ids:
            belief_id(action)
        if budget is not None and not isinstance(budget, SearchBudget):
            raise ValueError("planner requires a typed budget")
        budget = budget or SearchBudget()
        registration = self.registrations.get(registration_id)
        token = self.controller.rearm_token
        started = time.monotonic_ns()
        cutoff = started + budget.wall_time_ms * 1_000_000

        # {
        #   責務: [fallback: 現在requestへ束縛した固定reasonの非選択結果を作る]
        #   引数: [reason] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
        # }
        def fallback(reason: str) -> PlanningResult:
            return self._fallback(
                belief, allowed_action_ids, registration, token, cutoff, reason
            )

        if belief.context.clock_id != self.clock_id:
            return fallback("clock_origin_mismatch")
        if registration is None:
            return fallback("transition_unavailable")
        if registration.game_id != belief.context.game_id or any(
            item.state_ref.state_schema_id != registration.state_schema_id
            for item in belief.candidates
            if item.weight.value > 0
        ):
            return fallback("registered_state_mismatch")
        if not allowed_action_ids or len(allowed_action_ids) > budget.max_actions:
            return fallback("no_allowed_action_or_budget")
        if not any(item.weight.value > 0 for item in belief.candidates):
            return fallback("no_belief_candidates")
        if any(
            item.origin == HypothesisOrigin.PREDICTION
            for item in belief.candidates
            if item.weight.value > 0
        ):
            return replace(
                fallback("prediction_requires_observation"),
                outcome=PlanningOutcome.OBSERVE_MORE,
            )
        if any(
            len(item.state_ref.canonical_bytes) > budget.max_state_bytes
            for item in belief.candidates
            if item.weight.value > 0
        ):
            return fallback("state_byte_budget")
        invalid = self._current_error(belief, token, current_belief)
        if invalid:
            return fallback(invalid)
        if self._poisoned or not self._busy.acquire(blocking=False):
            return fallback("planner_unavailable_or_busy")
        parent_timing = new_parent_timing(started, cutoff)
        try:
            with _planning_directory(parent_timing) as directory_name:
                mark_parent_timing(parent_timing, "request_build_started_at_ns")
                directory = Path(directory_name)
                nonce = uuid4().hex
                request = {
                    "schema_version": 1,
                    "nonce": nonce,
                    "registration": asdict(registration),
                    "budget": asdict(budget),
                    "context": belief.context.to_dict(),
                    "belief_version": belief.hypothesis_set_version,
                    "weight_kind": belief.weight_kind.value,
                    "residual_unknown_mass": belief.residual_unknown_mass,
                    "expires_at_ns": belief.expires_at_ns,
                    "compute_cutoff_at_ns": cutoff,
                    "run_generation": token,
                    "allowed_action_ids": list(allowed_action_ids),
                    "hypotheses": [
                        {
                            "hypothesis_id": item.hypothesis_id,
                            "state_json": item.state_ref.canonical_bytes.decode(
                                "ascii"
                            ),
                            "digest": item.state_ref.digest,
                            "weight": item.weight.value,
                        }
                        for item in belief.candidates
                        if item.weight.value > 0
                    ],
                }
                mark_parent_timing(parent_timing, "request_build_finished_at_ns")
                try:
                    publish_planning_json(
                        directory / "request.json",
                        request,
                        budget.max_request_bytes,
                        parent_timing=parent_timing,
                    )
                except (ValueError, OSError) as error:
                    record_parent_io_error(parent_timing, "request_publish", error)
                    return fallback("request_byte_or_io_budget")
                return self._run(
                    directory,
                    nonce,
                    belief,
                    allowed_action_ids,
                    registration,
                    budget,
                    token,
                    cutoff,
                    current_belief,
                    parent_timing,
                )
        except OSError:
            return fallback("worker_io_or_cleanup_failed")
        finally:
            self.last_worker_diagnostic = merge_parent_timing(
                self.last_worker_diagnostic, parent_timing
            )
            self._busy.release()

    # {
    #   責務: [_run: 親deadlineとStopを監視し受領済みcheckpointだけを採用する]
    #   引数: [directory, nonce, belief, allowed, registration, budget, token, cutoff, current, parent_timing: 採用に使わない任意の親工程診断] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _run(
        self,
        directory: Path,
        nonce: str,
        belief: BeliefSnapshot[Any],
        allowed: tuple[str, ...],
        registration: PlanningRegistration,
        budget: SearchBudget,
        token: int,
        cutoff: int,
        current: Callable[[], BeliefSnapshot[Any] | None] | None,
        parent_timing: dict[str, Any] | None = None,
    ) -> PlanningResult:
        enforcement = "not_applied"

        # {
        #   責務: [fallback: requestへ実際のguard適用状況を付けて非選択結果を返す]
        #   引数: [reason: 固定理由] 戻り値: [PlanningResult: 実入力を許可しない結果]
        # }
        def fallback(reason: str) -> PlanningResult:
            return replace(
                self._fallback(belief, allowed, registration, token, cutoff, reason),
                limit_enforcement=enforcement,
            )

        if time.monotonic_ns() >= cutoff:
            return fallback("compute_budget")
        command = (
            [sys.executable, "--planning-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "ai_game_player.planning_worker"]
        )
        command += [
            str(directory),
            "--memory-bytes",
            str(budget.worker_memory_bytes),
            "--wall-time-ms",
            str(budget.wall_time_ms),
        ]
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                source_root,
                *[str(path) for path in sys.path],
                environment.get("PYTHONPATH", ""),
            ]
        )
        mark_parent_timing(parent_timing, "spawn_started_at_ns")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=(0x08000000 | 0x00000200) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError) as error:
            record_parent_io_error(parent_timing, "worker_start", error)
            return fallback("worker_start_failed")
        mark_parent_timing(parent_timing, "spawn_returned_at_ns")
        latest: PlanningResult | None = None
        last_bytes: bytes | None = None
        reason = "compute_budget"
        invalid = None
        io_phase = "ready_read"
        try:
            while True:
                invalid = self._current_error(belief, token, current)
                if invalid:
                    break
                now = time.monotonic_ns()
                if now >= cutoff:
                    break
                ready_path = directory / "ready.json"
                if ready_path.exists() and enforcement == "not_applied":
                    io_phase = "ready_read"
                    mark_parent_timing(parent_timing, "ready_read_started_at_ns")
                    ready = read_planning_json(ready_path, 4096)
                    if (
                        set(ready)
                        != {"nonce", "limit_enforcement", "worker_memory_bytes"}
                        or ready["nonce"] != nonce
                        or ready["worker_memory_bytes"] != budget.worker_memory_bytes
                        or ready["limit_enforcement"]
                        not in {"linux_rlimit_as", "windows_job_commit"}
                    ):
                        raise ValueError("invalid resource limit acknowledgement")
                    enforcement = ready["limit_enforcement"]
                    mark_parent_timing(parent_timing, "ready_received_at_ns")
                checkpoint_path = directory / "checkpoint.json"
                if checkpoint_path.exists():
                    mark_parent_timing(parent_timing, "checkpoint_first_seen_at_ns")
                    io_phase = "checkpoint_raw_read"
                    mark_parent_timing(
                        parent_timing, "checkpoint_raw_read_started_at_ns"
                    )
                    raw = read_planning_bytes(checkpoint_path, budget.max_result_bytes)
                    mark_parent_timing(
                        parent_timing, "checkpoint_raw_read_finished_at_ns"
                    )
                    if raw != last_bytes:
                        if len(raw) > budget.max_result_bytes:
                            raise ValueError("oversized planning checkpoint")
                        io_phase = "checkpoint_json_read"
                        mark_parent_timing(
                            parent_timing, "checkpoint_json_read_started_at_ns"
                        )
                        message = decode_planning_json(raw, budget.max_result_bytes)
                        mark_parent_timing(
                            parent_timing, "checkpoint_json_read_finished_at_ns"
                        )
                        received = time.monotonic_ns()
                        mark_parent_timing(
                            parent_timing,
                            "first_checkpoint_received_at_ns",
                            observed_ns=received,
                        )
                        if received < cutoff:
                            io_phase = "checkpoint_validate"
                            latest = self._validate_checkpoint(
                                message,
                                nonce,
                                belief,
                                allowed,
                                registration,
                                budget,
                                token,
                                cutoff,
                                received,
                            )
                            mark_parent_timing(
                                parent_timing,
                                "first_valid_checkpoint_received_at_ns",
                                observed_ns=received,
                            )
                            mark_parent_timing(
                                parent_timing, "first_checkpoint_validated_at_ns"
                            )
                            last_bytes = raw
                finished_path = directory / "finished.json"
                if finished_path.exists():
                    mark_parent_timing(parent_timing, "finished_first_seen_at_ns")
                    io_phase = "finished_read"
                    mark_parent_timing(parent_timing, "finished_read_started_at_ns")
                    finished = read_planning_json(finished_path, 4096)
                    mark_parent_timing(parent_timing, "finished_read_finished_at_ns")
                    if (
                        set(finished) != {"nonce", "reason", "budget_exit"}
                        or finished["nonce"] not in (nonce, None)
                        or type(finished["budget_exit"]) is not bool
                    ):
                        raise ValueError("invalid planning completion")
                    reason = finished["reason"]
                    if (
                        reason
                        not in {
                            "node_budget",
                            "compute_budget",
                            "depth_budget",
                            "worker_memory_limit",
                            "no_common_future_sequence",
                            "no_common_legal_action",
                        }
                        or not finished["budget_exit"]
                    ):
                        invalid = (
                            reason
                            if reason
                            in {
                                "unsupported_resource_limits",
                                "worker_failed",
                                "invalid_model_or_quality",
                            }
                            else "worker_protocol_failure"
                        )
                    if (
                        invalid is None
                        and checkpoint_path.exists()
                        and time.monotonic_ns() < cutoff
                    ):
                        mark_parent_timing(parent_timing, "checkpoint_first_seen_at_ns")
                        io_phase = "checkpoint_json_read"
                        mark_parent_timing(
                            parent_timing, "checkpoint_json_read_started_at_ns"
                        )
                        message = read_planning_json(
                            checkpoint_path, budget.max_result_bytes
                        )
                        mark_parent_timing(
                            parent_timing, "checkpoint_json_read_finished_at_ns"
                        )
                        received = time.monotonic_ns()
                        mark_parent_timing(
                            parent_timing,
                            "first_checkpoint_received_at_ns",
                            observed_ns=received,
                        )
                        if received < cutoff:
                            io_phase = "checkpoint_validate"
                            candidate = self._validate_checkpoint(
                                message,
                                nonce,
                                belief,
                                allowed,
                                registration,
                                budget,
                                token,
                                cutoff,
                                received,
                            )
                            mark_parent_timing(
                                parent_timing,
                                "first_valid_checkpoint_received_at_ns",
                                observed_ns=received,
                            )
                            mark_parent_timing(
                                parent_timing, "first_checkpoint_validated_at_ns"
                            )
                            if (
                                latest is None
                                or candidate.completed_depth > latest.completed_depth
                            ):
                                latest = candidate
                    break
                if process.poll() is not None:
                    # A final checkpoint and finished marker are atomic; retry once before failure.
                    if finished_path.exists():
                        continue
                    invalid = "worker_failed"
                    break
                time.sleep(
                    min(0.002, max(0.0, (cutoff - time.monotonic_ns()) / 1_000_000_000))
                )
        except (OSError, ValueError, TypeError, OverflowError) as error:
            record_parent_io_error(parent_timing, io_phase, error)
            invalid = "worker_protocol_failure"
        finally:
            try:
                exit_seen = process.poll() is not None
            except Exception:
                exit_seen = False
            mark_parent_timing(parent_timing, "process_cleanup_started_at_ns")
            try:
                self._poisoned = not _stop_owned_process(process)
            except OSError as error:
                record_parent_io_error(parent_timing, "process_cleanup", error)
                raise
            finally:
                mark_parent_timing(parent_timing, "process_cleanup_finished_at_ns")
            try:
                self.last_worker_diagnostic = collect_worker_diagnostic(
                    directory,
                    process.poll(),
                    cleanup_requested=not exit_seen,
                    exit_seen_before_cleanup=exit_seen,
                    highest_received_depth=latest.completed_depth
                    if latest is not None
                    else 0,
                )
            except Exception:
                self.last_worker_diagnostic = None
        if self._poisoned:
            return fallback("worker_cleanup_incomplete")
        invalid = invalid or self._current_error(belief, token, current)
        if invalid:
            return fallback(invalid)
        if latest is None:
            return fallback(reason)
        # The checkpoint receipt time stays separate from the compute cutoff and observation expiry.
        result = replace(
            latest, reason=reason if reason != "depth_budget" else latest.reason
        )
        committed = []
        try:
            self.controller.run_if_current(token, lambda: committed.append(result))
        except ExecutionCancelled:
            return fallback("stopped_or_rearmed")
        return committed[0]

    # {
    #   責務: [_validate_checkpoint: child JSONをsource・版・世代・質量・予算と厳密照合する]
    #   引数: [message, nonce, belief, allowed, registration, budget, token, cutoff, received] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _validate_checkpoint(
        self,
        message: dict[str, Any],
        nonce: str,
        belief: BeliefSnapshot[Any],
        allowed: tuple[str, ...],
        registration: PlanningRegistration,
        budget: SearchBudget,
        token: int,
        cutoff: int,
        received: int,
    ) -> PlanningResult:
        if set(message) != {"nonce", "result"} or message["nonce"] != nonce:
            raise ValueError("planning checkpoint request mismatch")
        result = result_from_dict(message["result"])
        if (
            result.context != belief.context
            or result.belief_version != belief.hypothesis_set_version
            or result.allowed_action_ids != allowed
            or result.run_generation != token
            or result.compute_cutoff_at_ns != cutoff
            or result.expires_at_ns != belief.expires_at_ns
            or result.created_at_ns != 0
        ):
            raise ValueError("planning checkpoint source mismatch")
        expected_continuation = (
            "one_step"
            if result.completed_depth == 1
            else "state_observed_expectimax"
            if registration.future_state_observable
            else "open_loop_mpc"
        )
        if result.continuation_semantics != expected_continuation:
            raise ValueError("planning continuation observability mismatch")
        actual_meta = (
            result.model_id,
            result.model_version,
            result.evaluator_id,
            result.evaluator_version,
            result.quality_projection_version,
            result.quality_profile_version,
            result.value_unit,
        )
        expected_meta = (
            registration.model_id,
            registration.model_version,
            registration.evaluator_id,
            registration.evaluator_version,
            registration.quality_projection_version,
            registration.quality_profile_version,
            registration.value_unit,
        )
        if actual_meta != expected_meta or result.limit_enforcement not in {
            "linux_rlimit_as",
            "windows_job_commit",
        }:
            raise ValueError("planning checkpoint registered evaluator mismatch")
        expected_semantics = (
            "calibrated"
            if belief.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
            else "conditional_score"
        )
        if (
            result.probability_semantics != expected_semantics
            or result.residual_unknown_mass != belief.residual_unknown_mass
            or not 1 <= result.completed_depth <= budget.max_depth
            or result.counters.nodes_visited > budget.max_nodes
            or result.counters.peak_cache_entries > budget.max_cache_entries
            or result.counters.cache_bytes > budget.max_cache_bytes
        ):
            raise ValueError("planning checkpoint budget or probability mismatch")
        weights = {
            item.hypothesis_id: item.weight.value
            for item in belief.candidates
            if item.weight.value > 0
        }
        for action in result.action_values:
            if len(action.outcomes) > budget.max_trace_values or {
                item.hypothesis_id for item in action.hypothesis_values
            } != set(weights):
                raise ValueError("planning checkpoint hypothesis set mismatch")
            for item in action.hypothesis_values:
                if (
                    len(item.conditional_outcomes) > budget.max_trace_values
                    or (
                        item.belief_weight
                        if expected_semantics == "calibrated"
                        else item.relative_score
                    )
                    != weights[item.hypothesis_id]
                ):
                    raise ValueError("planning checkpoint hypothesis weight mismatch")
        return replace(result, created_at_ns=received)

    # {
    #   責務: [_current_error: 短いmetadata読取でStop・観測鮮度・現在仮説版を確認する]
    #   引数: [belief, token, current] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _current_error(
        self,
        belief: BeliefSnapshot[Any],
        token: int,
        current: Callable[[], BeliefSnapshot[Any] | None] | None,
    ) -> str | None:
        try:
            self.controller.ensure_running(token)
        except ExecutionCancelled:
            return "stopped_or_rearmed"
        if current is not None:
            try:
                active = current()
            except Exception:
                return "current_belief_unavailable"
            if (
                not isinstance(active, BeliefSnapshot)
                or active.context != belief.context
                or active.hypothesis_set_version != belief.hypothesis_set_version
            ):
                return "stale_belief_version"
        if not belief.created_at_ns <= time.monotonic_ns() < belief.expires_at_ns:
            return "stale_observation"
        return None

    # {
    #   責務: [_fallback: モデル実行不能時も元contextと数値版を持つ結果を返す]
    #   引数: [belief, allowed, registration, token, cutoff, reason] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _fallback(
        self,
        belief: BeliefSnapshot[Any],
        allowed: tuple[str, ...],
        registration: PlanningRegistration | None,
        token: int,
        cutoff: int,
        reason: str,
    ) -> PlanningResult:
        return PlanningResult(
            PlanningOutcome.ABSTAIN,
            None,
            (),
            belief.context,
            belief.hypothesis_set_version,
            allowed,
            "unknown",
            belief.residual_unknown_mass,
            0,
            reason,
            registration.model_id if registration else "unavailable",
            registration.model_version if registration else "unavailable",
            registration.evaluator_id if registration else "unavailable",
            registration.evaluator_version if registration else "unavailable",
            registration.quality_projection_version if registration else 1,
            registration.quality_profile_version if registration else "default.1",
            registration.value_unit if registration else "normalized_quality.v1",
            time.monotonic_ns(),
            belief.expires_at_ns,
            cutoff,
            PlanningCounters(),
            "not_applied",
            token,
        )


# {
#   責務: [_stop_owned_process: このplannerが起動したworkerだけを有限waitで回収する]
#   処理: [terminate→100ms wait→kill→100ms wait。Stop用Run lockやmodel callbackを保持しない]
#   引数: [process: 所有した子process] 戻り値: [bool: 回収済みならTrue]
# }
def _stop_owned_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=0.1)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return process.poll() is not None
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=0.1)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None


# {
#   責務: [_planning_directory: 元TemporaryDirectoryのcleanupを同じ例外契約で計測する]
#   処理: [retry/無視/期限延長なし。診断はcleanupの開始/完了と固定IOcodeだけを保持]
# }
@contextmanager
def _planning_directory(parent_timing: dict[str, Any] | None) -> Iterator[str]:
    mark_parent_timing(parent_timing, "directory_create_started_at_ns")
    try:
        directory = tempfile.TemporaryDirectory(prefix="kadoka-planning-")
    except OSError as error:
        record_parent_io_error(parent_timing, "directory_create", error)
        raise
    mark_parent_timing(parent_timing, "directory_created_at_ns")
    try:
        yield directory.name
    finally:
        mark_parent_timing(parent_timing, "directory_cleanup_started_at_ns")
        try:
            directory.cleanup()
        except OSError as error:
            record_parent_io_error(parent_timing, "directory_cleanup", error)
            raise
        finally:
            mark_parent_timing(parent_timing, "directory_cleanup_finished_at_ns")
