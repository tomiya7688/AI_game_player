"""有界JSONで一つのpure planning requestを処理する制限process entrypoint。"""

import argparse
from pathlib import Path
import sys
from typing import Any

from ai_game_player.planning_worker_limits import apply_worker_limits
from ai_game_player.planning_worker_diagnostic import WorkerDiagnostic, WorkerPhase


# {
#   責務: [main: factoryをimportする前にOS予算を設定し、一requestだけを処理する]
#   処理: [bootstrap後の限界は明記し、制限不能時は固定fallbackを出す]
#   引数: [argv: 親が作る有界のrequestdirectory/memory/time引数]
#   戻り値: [int: 成功/予算打切り0、契約/実行失敗1]
# }
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--wall-time-ms", type=int, required=True)
    arguments = parser.parse_args(argv)
    if (
        not 64 * 1024**2 <= arguments.memory_bytes <= 2 * 1024**3
        or not 10 <= arguments.wall_time_ms <= 60000
    ):
        return 2
    directory = arguments.directory
    diagnostic = WorkerDiagnostic()
    diagnostic.mark(WorkerPhase.RESOURCE_LIMITS)
    try:
        method, handle = apply_worker_limits(
            arguments.memory_bytes, arguments.wall_time_ms
        )
    except (OSError, RuntimeError, ValueError) as error:
        diagnostic.record_exception(directory, error)
        _finish(directory, "unsupported_resource_limits")
        return 1
    # Keep the Job handle alive until process exit; all factory imports follow the guard.
    _ = handle
    reserve = bytearray(65536)
    try:
        return _execute(directory, method, diagnostic)
    except MemoryError as error:
        del reserve
        diagnostic.record_exception(directory, error)
        _finish(directory, "worker_memory_limit")
        return 1
    except Exception as error:
        diagnostic.record_exception(directory, error)
        _finish(directory, "worker_failed")
        return 1


# {
#   責務: [_execute: 型付きrequestを復号して完全深さcheckpointだけを保存する]
#   処理: [動的import先は親のtrusted登録。state codecをworker内で照合し、任意pickleを使わない]
#   引数: [directory: 親所有一時directory, method: 実適用したOS制限方式, diagnostic: 固定phaseだけの一時診断]
#   戻り値: [int: worker終了code]
# }
def _execute(directory: Path, method: str, diagnostic: WorkerDiagnostic) -> int:
    import importlib
    import math

    from ai_game_player.belief_contract import BeliefWeightKind
    from ai_game_player.belief_validation import (
        belief_id,
        belief_integer,
        belief_number,
    )
    from ai_game_player.expectimax_search import (
        ExpectimaxSearch,
        SearchCutoff,
        SearchFailure,
        SearchHypothesis,
    )
    from ai_game_player.planning_contract import (
        PlanningDomain,
        PlanningRegistration,
        SearchBudget,
    )
    from ai_game_player.planning_outcome import PlanningOutcome
    from ai_game_player.planning_result import PlanningResult
    from ai_game_player.planning_wire import (
        context_from_dict,
        publish_planning_json,
        read_planning_json,
        result_to_dict,
    )
    from ai_game_player.world_state_hypothesis import TypedStateRef

    diagnostic.mark(WorkerPhase.REQUEST_READ)
    request = read_planning_json(directory / "request.json", 16 * 1024 * 1024)
    expected = {
        "schema_version",
        "nonce",
        "registration",
        "budget",
        "context",
        "belief_version",
        "weight_kind",
        "residual_unknown_mass",
        "expires_at_ns",
        "compute_cutoff_at_ns",
        "run_generation",
        "allowed_action_ids",
        "hypotheses",
    }
    if (
        set(request) != expected
        or type(request["schema_version"]) is not int
        or request["schema_version"] != 1
    ):
        raise ValueError("invalid planning request schema")
    nonce = belief_id(request["nonce"])
    registration = PlanningRegistration(**request["registration"])
    budget = SearchBudget(**request["budget"])
    if len((directory / "request.json").read_bytes()) > budget.max_request_bytes:
        raise ValueError("planning request exceeds declared byte budget")
    context = context_from_dict(request["context"])
    if context.game_id != registration.game_id:
        raise ValueError("registered planning game mismatch")
    belief_integer(request["belief_version"], 1)
    belief_integer(request["expires_at_ns"])
    belief_integer(request["compute_cutoff_at_ns"])
    belief_integer(request["run_generation"])
    residual = belief_number(request["residual_unknown_mass"], 1)
    kind = BeliefWeightKind(request["weight_kind"])
    if (
        not isinstance(request["allowed_action_ids"], list)
        or not 1 <= len(request["allowed_action_ids"]) <= budget.max_actions
        or len(set(request["allowed_action_ids"])) != len(request["allowed_action_ids"])
    ):
        raise ValueError("planning allowed action tuple invalid")
    allowed = tuple(belief_id(action) for action in request["allowed_action_ids"])
    if (
        not isinstance(request["hypotheses"], list)
        or not 1 <= len(request["hypotheses"]) <= 256
    ):
        raise ValueError("planning hypothesis count invalid")
    diagnostic.mark(WorkerPhase.READY_PUBLISH)
    publish_planning_json(
        directory / "ready.json",
        {
            "nonce": nonce,
            "limit_enforcement": method,
            "worker_memory_bytes": budget.worker_memory_bytes,
        },
        4096,
    )
    diagnostic.mark(WorkerPhase.FACTORY_IMPORT)
    if registration.module_name == "ai_game_player.planning_2048":
        # Static edge includes the optional codec/model in a self-contained frozen bundle.
        from ai_game_player import planning_2048

        module = planning_2048
    else:
        module = importlib.import_module(registration.module_name)
    factory: Any = module
    for part in registration.factory_name.split("."):
        factory = getattr(factory, part)
    diagnostic.mark(WorkerPhase.FACTORY_CREATE)
    domain = factory()
    if (
        not isinstance(domain, PlanningDomain)
        or domain.future_state_observable != registration.future_state_observable
    ):
        raise ValueError("planning factory must return a typed domain")
    hypotheses = []
    identities = set()
    diagnostic.mark(WorkerPhase.STATE_DECODE)
    for item in request["hypotheses"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"hypothesis_id", "state_json", "digest", "weight"}
            or not isinstance(item["state_json"], str)
            or len(item["state_json"]) > budget.max_state_bytes
        ):
            raise ValueError("planning hypothesis fields invalid")
        identity = belief_id(item["hypothesis_id"])
        if identity in identities:
            raise ValueError("duplicate planning hypothesis")
        identities.add(identity)
        raw = item["state_json"].encode("ascii")
        state = domain.decode_state(raw)
        reference = TypedStateRef(
            registration.game_id, registration.state_schema_id, domain.state_type, state
        )
        if reference.canonical_bytes != raw or reference.digest != item["digest"]:
            raise ValueError("planning state codec changed canonical state")
        weight = belief_number(
            item["weight"],
            1 if kind == BeliefWeightKind.CALIBRATED_PROBABILITY else float("inf"),
        )
        hypotheses.append(SearchHypothesis(identity, reference, weight))
    if kind == BeliefWeightKind.CALIBRATED_PROBABILITY:
        if not math.isclose(
            math.fsum(item.weight for item in hypotheses) + residual,
            1,
            rel_tol=0,
            abs_tol=1e-10,
        ):
            raise ValueError("planning world probability mass invalid")
    elif kind != BeliefWeightKind.RELATIVE_SCORE or residual != 1:
        raise ValueError("uncalibrated planning must preserve unknown probability")
    engine = ExpectimaxSearch(
        domain, registration, budget, tuple(hypotheses), allowed, kind, residual
    )
    diagnostic.mark(WorkerPhase.SEARCH)
    try:
        for depth, values, counters in engine.deepen():
            diagnostic.mark(WorkerPhase.RESULT_BUILD)
            best = max(
                values,
                key=lambda item: (item.ranking_score, -allowed.index(item.action_id)),
            )
            conditional = kind == BeliefWeightKind.RELATIVE_SCORE
            reason = "completed_depth"
            outcome = PlanningOutcome.SELECT
            if conditional and not budget.allow_conditional_selection:
                outcome, reason = (
                    PlanningOutcome.ABSTAIN,
                    "uncalibrated_world_probability",
                )
            elif not conditional and residual > budget.max_unknown_mass:
                outcome, reason = PlanningOutcome.OBSERVE_MORE, "belief_unknown_mass"
            elif (
                all(item.lower_bound <= -1 and item.upper_bound >= 1 for item in values)
                and not conditional
            ):
                outcome, reason = PlanningOutcome.OBSERVE_MORE, "quality_unavailable"
            result = PlanningResult(
                outcome,
                best.action_id if outcome == PlanningOutcome.SELECT else None,
                values,
                context,
                request["belief_version"],
                allowed,
                "conditional_score" if conditional else "calibrated",
                residual,
                depth,
                reason,
                registration.model_id,
                registration.model_version,
                registration.evaluator_id,
                registration.evaluator_version,
                registration.quality_projection_version,
                registration.quality_profile_version,
                registration.value_unit,
                0,
                request["expires_at_ns"],
                request["compute_cutoff_at_ns"],
                counters,
                method,
                request["run_generation"],
                continuation_semantics="one_step"
                if depth == 1
                else "state_observed_expectimax"
                if domain.future_state_observable
                else "open_loop_mpc",
            )
            diagnostic.mark(WorkerPhase.CHECKPOINT_PUBLISH)
            publish_planning_json(
                directory / "checkpoint.json",
                {"nonce": nonce, "result": result_to_dict(result)},
                budget.max_result_bytes,
            )
            diagnostic.mark(WorkerPhase.SEARCH)
    except SearchCutoff as error:
        diagnostic.mark(WorkerPhase.FINISHED_PUBLISH)
        publish_planning_json(
            directory / "finished.json",
            {"nonce": nonce, "reason": str(error), "budget_exit": True},
            4096,
        )
        return 0
    except (SearchFailure, ValueError, TypeError, OverflowError) as error:
        diagnostic.record_exception(directory, error)
        _finish(directory, "invalid_model_or_quality", nonce)
        return 1
    diagnostic.mark(WorkerPhase.FINISHED_PUBLISH)
    publish_planning_json(
        directory / "finished.json",
        {"nonce": nonce, "reason": "depth_budget", "budget_exit": True},
        4096,
    )
    return 0


# {
#   責務: [_finish: 大きな診断や例外文字列を返さず固定fallbackを保存する]
#   処理: [既存atomic JSONを使い、quota不足でも小さい失敗messageだけを試みる]
#   引数: [directory: 通信先, reason: 固定code, nonce: 読取済みrequest ID]
#   戻り値: [なし。IO自体が失敗した場合は親がprocess失敗を検知する]
# }
def _finish(directory: Path, reason: str, nonce: str | None = None) -> None:
    from ai_game_player.planning_wire import publish_planning_json

    try:
        publish_planning_json(
            directory / "finished.json",
            {
                "nonce": nonce,
                "reason": reason,
                "budget_exit": reason == "worker_memory_limit",
            },
            4096,
        )
    except (OSError, ValueError, MemoryError):
        pass


if __name__ == "__main__":
    sys.exit(main())
