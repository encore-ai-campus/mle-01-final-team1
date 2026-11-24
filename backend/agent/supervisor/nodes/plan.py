# 전문 Agent 작업계획을 MainState 패치 후보로 정규화한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from copy import deepcopy
from typing import Any, TYPE_CHECKING
from ..state import SupervisorWorkingState, ToolObservation
from ..contracts import (
    SupervisorContractError, normalize_plan, validate_plan, validate_intent_scenarios,
)
from ..tools.profile_fields import validate_overlay, merge_goals, source_with_reads
from ..tools.memory import merge_memory
from ..tools.scenarios import prepare_scenarios, prepare_verified_scenarios
if TYPE_CHECKING:
    from ..supervisor_main import Supervisor


def last_result(observations: list[ToolObservation], name: str) -> dict[str, Any] | None:
    for obs in reversed(observations):
        if obs["name"] == name and obs["result"].get("status") != "failed":
            return obs["result"]
    return None


def attach_memory(
    source: dict[str, Any], observations: list[ToolObservation], patch: dict[str, Any],
) -> None:
    if (source.get("memory") or {}).get("retrieval_turn_id") or any(obs["name"] == "retrieve_memory" for obs in observations):
        patch["memory"] = merge_memory(source, observations)


def merged_intent(source: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any] | None:
    if "intent" not in decision:
        return None
    old = source.get("intent")
    intent = deepcopy(old) if isinstance(old, dict) else {}
    update = deepcopy(decision["intent"])
    if "supplied_values" in update:
        incoming = update.pop("supplied_values")
        if not isinstance(incoming, dict):
            raise SupervisorContractError("intent.supplied_values는 dict이어야 합니다.")
        previous_values = intent.get("supplied_values")
        values = deepcopy(previous_values) if isinstance(previous_values, dict) else {}
        validate_overlay(incoming, supplied=True)
        if "goals" in incoming:
            base = values.get("goals", (source.get("basic_profile") or {}).get("goals"))
            incoming["goals"] = merge_goals(base, incoming["goals"], replace=decision.get("goal_mode") == "replace")
        values.update(incoming)
        intent["supplied_values"] = values
    intent.update(update)
    return intent


def has_current_review_rejection(source: dict[str, Any]) -> bool:
    """실제 현재 run에 반려가 있을 때만 외부 재계획 횟수를 센다."""
    plan = source.get("execution_plan") or {}
    reviews = source.get("review_results") or {}
    if not isinstance(plan, dict) or not isinstance(reviews, dict):
        return False
    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue
        review = reviews.get(task.get("task_id")) or {}
        if (not isinstance(review, dict)
                or not task.get("run_id")
                or review.get("target_run_id") != task.get("run_id")):
            continue
        for phase in ("evidence", "reflection"):
            data = review.get(phase)
            if isinstance(data, dict) and data.get("status") == "revise":
                return True
    return False


def run(supervisor: "Supervisor", state: SupervisorWorkingState) -> dict[str, Any]:
    try:
        decision = state["decision"]
        src = source_with_reads(state)
        scenario_result = last_result(state.get("observations", []), "prepare_scenarios")
        scenario = scenario_result["scenario_state"] if scenario_result else src.get("scenario_state")
        previous_scenario = src.get("scenario_state")
        # 새 사실을 얻지 않는 정규화는 Plan에서 바로 실행한다.
        # 별도 Tool 경로와 동일한 baseline/holding 출처 검사로 우회 제출을 차단한다.
        if "scenario_state" in decision:
            scenario = prepare_verified_scenarios(decision["scenario_state"], state)["scenario_state"]
        elif scenario is not None:
            scenario = prepare_scenarios(scenario)["scenario_state"]
        scenario_changed = scenario != previous_scenario
        intent = merged_intent(src, decision)
        validate_intent_scenarios(intent, scenario)
        for branch in (scenario or {}).get("scenarios", []):
            overrides = branch.get("overrides", {})
            if "goals" in overrides:
                supplied = ((intent or src.get("intent") or {}).get("supplied_values") or {})
                base_goals = supplied.get("goals", (src.get("basic_profile") or {}).get("goals"))
                merge_goals(base_goals, overrides["goals"], replace=branch.get("assumptions", {}).get("goal_mode") == "replace")
        input_changed = ((intent is not None and intent != src.get("intent"))
                         or any(src.get(domain) != state["source"].get(domain) for domain in ("basic_profile", "financial_profile")))
        plan = normalize_plan(
            decision.get("execution_plan"), src.get("execution_plan"), scenario_changed,
            input_changed=input_changed,
            review_rejected=has_current_review_rejection(src),
        )
        validate_plan(plan, scenario)
        patch: dict[str, Any] = {"phase": "planning", "execution_plan": plan, "intent": intent}
        if "scenario_state" in decision or scenario_result is not None or scenario_changed:
            patch["scenario_state"] = scenario
        attach_memory(src, state.get("observations", []), patch)
        return {"candidate_patch": patch, "candidate_error": None}
    except (KeyError, TypeError, SupervisorContractError) as exc:
        return {"candidate_patch": {}, "candidate_error": str(exc)}
