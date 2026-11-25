# 전문 Agent 작업계획을 MainState 패치 후보로 정규화한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from copy import deepcopy
from typing import Any, TYPE_CHECKING
from ..state import SupervisorWorkingState, ToolObservation
from ..contracts import (
    SupervisorContractError, normalize_plan, validate_intent_scenarios, PROFILE_UPDATE_STATUSES,
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


def server_profile_update_status(source: dict[str, Any]) -> str:
    """Only trusted source state and recorded proposals determine approval progress."""
    prior = (source.get("intent") or {}).get("profile_update_status", "not_needed")
    if not isinstance(prior, str) or prior not in PROFILE_UPDATE_STATUSES:
        raise SupervisorContractError("Invalid stored profile_update_status")
    changes = [c for key in ("basic_profile_changes", "financial_profile_changes")
               for c in source.get(key) or [] if isinstance(c, dict)]
    if any(c.get("status") == "needs_confirmation" for c in changes):
        return "waiting"
    if any(c.get("status") == "pending" for c in changes):
        return "pending"
    # Approval or rejection is performed by the authenticated caller, not by the model.
    return prior


def merged_intent(
    source: dict[str, Any], decision: dict[str, Any], *,
    consultation_context: list[str] | None = None,
) -> dict[str, Any] | None:
    if "intent" not in decision:
        return None
    old = source.get("intent")
    intent = deepcopy(old) if isinstance(old, dict) else {}
    update = deepcopy(decision["intent"])
    legacy_status = update.pop("profile_update_status", None)
    status = server_profile_update_status(source)
    if legacy_status is not None and (not isinstance(legacy_status, str) or legacy_status not in PROFILE_UPDATE_STATUSES):
        raise SupervisorContractError("Invalid profile_update_status")
    if legacy_status == "done" and status != "done":
        raise SupervisorContractError("프로필 저장 완료를 LLM이 직접 선언할 수 없습니다.")
    # Omission is not a command to erase prior values; the old overlay stays intact.
    if "supplied_values" not in intent:
        intent["supplied_values"] = {}
    elif not isinstance(intent["supplied_values"], dict):
        raise SupervisorContractError("Invalid stored supplied_values")
    if "supplied_values" in update:
        incoming, consultation = normalize_supplied_values(update.pop("supplied_values"))
        previous_values = intent.get("supplied_values")
        values = deepcopy(previous_values) if isinstance(previous_values, dict) else {}
        if consultation:
            # 이전 LLM의 잘못 배치된 상담 수치를 버리거나 실제 목표 레코드로 만들지 않는다.
            # 저장/구조화 목표 편집과 섞이면 의미가 불명확하므로 자동 복구하지 않는다.
            if ("goals" in incoming or decision.get("goal_mode") == "replace"
                    or status != "not_needed" or legacy_status not in (None, "not_needed")):
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
    intent["profile_update_status"] = status
    return intent


def current_review_rejections(source: dict[str, Any]) -> tuple[str, ...]:
    """Reject only a real completed result belonging to the same revision/run/scenario."""
    plan = source.get("execution_plan") or {}
    results = source.get("agent_results") or {}
    reviews = source.get("review_results") or {}
    if not all(isinstance(x, dict) for x in (plan, results, reviews)):
        return ()
    ids = []
    for task in plan.get("tasks", []):
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            continue
        tid, run_id = task.get("task_id"), task.get("run_id")
        result, review = results.get(tid), reviews.get(tid)
        if (task.get("status") != "completed" or not isinstance(run_id, str) or not run_id
                or not isinstance(result, dict) or result.get("task_id") != tid
                or result.get("status") != "completed" or result.get("run_id") != run_id
                or type(result.get("plan_revision")) is not int
                or result.get("plan_revision") != plan.get("revision")
                or result.get("scenario_id") != task.get("scenario_id")
                or not isinstance(review, dict) or review.get("target_run_id") != run_id):
            continue
        if any(isinstance(review.get(stage), dict) and review[stage].get("status") == "revise"
               for stage in ("evidence", "reflection")):
            ids.append(tid)
    return tuple(ids)


def has_current_review_rejection(source: dict[str, Any]) -> bool:
    return bool(current_review_rejections(source))


def execution_retry_candidates(source: dict[str, Any]) -> tuple[str, ...]:
    """Parent workflow requests a replan while executing; no automatic retry on ordinary revisit.

    A Collector failure may have no result (missing/duplicate envelope); the issued run_id
    and failed status are server-owned. needs_input requires new information, not a blind retry.
    """
    if source.get("phase") != "executing":
        return ()
    return tuple(t["task_id"] for t in (source.get("execution_plan") or {}).get("tasks", [])
                 if isinstance(t, dict) and t.get("status") == "failed"
                 and isinstance(t.get("task_id"), str) and t["task_id"]
                 and isinstance(t.get("run_id"), str) and t["run_id"])


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
        requested = decision.get("retry_task_ids", [])
        allowed = set(execution_retry_candidates(src))
        if set(requested) - allowed:
            raise SupervisorContractError("retry_task_ids must reference issued failed tasks in the current execution")
        review_ids = current_review_rejections(src)
        # A changed plan may intentionally remove a rejected task. Only surviving review roots
        # need resetting in an unchanged plan; changed inputs already invalidate the revision.
        draft_ids = {t["task_id"] for t in (draft or {}).get("tasks", [])
                     if isinstance(t, dict) and isinstance(t.get("task_id"), str)} if isinstance(draft, dict) else set()
        if set(requested) - draft_ids:
            raise SupervisorContractError("Retry target is absent from the submitted plan")
        roots = tuple(dict.fromkeys([*requested, *(tid for tid in review_ids if tid in draft_ids)]))
        plan = normalize_plan(
            draft, src.get("execution_plan"), scenario_changed,
            input_changed=input_changed,
            review_rejected=bool(review_ids),
            retry_task_ids=roots,
        )
        plan = apply_candidate_limits(plan, scenario, src.get("execution_plan"))
        patch: dict[str, Any] = {"phase": "planning", "execution_plan": plan, "intent": intent}
        if "scenario_state" in decision or scenario_result is not None or scenario_changed or selected is not None:
            patch["scenario_state"] = scenario
        attach_memory(src, state.get("observations", []), patch)
        return {"candidate_patch": patch, "candidate_error": None}
    except (KeyError, TypeError, SupervisorContractError) as exc:
        return {"candidate_patch": {}, "candidate_error": str(exc)}
