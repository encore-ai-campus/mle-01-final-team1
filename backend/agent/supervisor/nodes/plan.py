# 전문 Agent 작업계획을 MainState 패치 후보로 정규화한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from copy import deepcopy
from typing import Any, TYPE_CHECKING
from ..state import SupervisorWorkingState, ToolObservation
from ..contracts import (
    SupervisorContractError, normalize_plan, validate_intent_scenarios,
)
from ..tools.profile_fields import normalize_supplied_values, merge_goals, source_with_reads
from ..tools.memory import merge_memory
from ..tools.advice import selected_scenario
from ..candidate_budget import apply_candidate_limits
from ..clarification import has_answered_waiting_turn
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


def merged_intent(
    source: dict[str, Any], decision: dict[str, Any], *,
    consultation_context: list[str] | None = None,
) -> dict[str, Any] | None:
    if "intent" not in decision:
        return None
    old = source.get("intent")
    intent = deepcopy(old) if isinstance(old, dict) else {}
    update = deepcopy(decision["intent"])
    if "supplied_values" in update:
        incoming, consultation = normalize_supplied_values(update.pop("supplied_values"))
        previous_values = intent.get("supplied_values")
        values = deepcopy(previous_values) if isinstance(previous_values, dict) else {}
        if consultation:
            # 이전 LLM의 잘못 배치된 상담 수치를 버리거나 실제 목표 레코드로 만들지 않는다.
            # 저장/구조화 목표 편집과 섞이면 의미가 불명확하므로 자동 복구하지 않는다.
            status = update.get("profile_update_status", intent.get("profile_update_status"))
            if "goals" in incoming or decision.get("goal_mode") == "replace" or status not in (None, "not_needed"):
                raise SupervisorContractError("supplied_values: separate consultation amount/period from structured goals or saved profile changes")
            purpose = update.get("purpose", intent.get("purpose"))
            if not isinstance(purpose, str) or not purpose.strip():
                raise SupervisorContractError("intent.purpose required to preserve consultation amount/period")
            conditions = []
            if "target_amount_krw" in consultation:
                conditions.append(f"목표 금액 {consultation['target_amount_krw']}원")
            if "horizon_months" in consultation:
                conditions.append(f"분석 기간 {consultation['horizon_months']}개월")
            suffix = " [전체 상담 조건: " + ", ".join(conditions) + "]"
            update["purpose"] = purpose if suffix in purpose else purpose + suffix
            if consultation_context is not None:
                consultation_context.append(suffix)
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
        selected = selected_scenario(decision, state)
        if selected is not None:
            scenario = selected
        elif "scenario_state" in decision:
            scenario = prepare_verified_scenarios(decision["scenario_state"], state)["scenario_state"]
        elif scenario is not None:
            scenario = prepare_scenarios(scenario)["scenario_state"]
        scenario_changed = scenario != previous_scenario
        consultation_context: list[str] = []
        intent = merged_intent(src, decision, consultation_context=consultation_context)
        validate_intent_scenarios(intent, scenario)
        for branch in (scenario or {}).get("scenarios", []):
            overrides = branch.get("overrides", {})
            if "goals" in overrides:
                supplied = ((intent or src.get("intent") or {}).get("supplied_values") or {})
                base_goals = supplied.get("goals", (src.get("basic_profile") or {}).get("goals"))
                merge_goals(base_goals, overrides["goals"], replace=branch.get("assumptions", {}).get("goal_mode") == "replace")
        input_changed = ((intent is not None and intent != src.get("intent"))
                         or any(src.get(domain) != state["source"].get(domain) for domain in ("basic_profile", "financial_profile"))
                         or bool(src.get("execution_plan") and has_answered_waiting_turn(src)))
        draft = decision.get("execution_plan")
        # 잘못 배치됐던 공통 상담 수치도 Dispatcher의 objective에서 확인할 수 있게 한다.
        # 작업별 가정/의존관계/ID는 변경하지 않으며, 정상 출력에는 추가하지 않는다.
        if consultation_context and isinstance(draft, dict) and isinstance(draft.get("tasks"), list):
            draft = deepcopy(draft)
            suffix = consultation_context[0]
            for task in draft["tasks"]:
                if isinstance(task, dict) and isinstance(task.get("objective"), str) and suffix not in task["objective"]:
                    task["objective"] += suffix
        plan = normalize_plan(
            draft, src.get("execution_plan"), scenario_changed,
            input_changed=input_changed,
            review_rejected=has_current_review_rejection(src),
        )
        plan = apply_candidate_limits(plan, scenario, src.get("execution_plan"))
        patch: dict[str, Any] = {"phase": "planning", "execution_plan": plan, "intent": intent}
        if "scenario_state" in decision or scenario_result is not None or scenario_changed or selected is not None:
            patch["scenario_state"] = scenario
        attach_memory(src, state.get("observations", []), patch)
        return {"candidate_patch": patch, "candidate_error": None}
    except (KeyError, TypeError, SupervisorContractError) as exc:
        return {"candidate_patch": {}, "candidate_error": str(exc)}
