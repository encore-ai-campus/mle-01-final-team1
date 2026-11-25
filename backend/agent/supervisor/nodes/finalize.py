# 출력 경계에서 MainState 변경 범위와 라우팅을 검증한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from copy import deepcopy
from typing import Any
from ..state import SupervisorWorkingState, ToolObservation
from ..contracts import (
    SupervisorContractError,
    validate_intent_scenarios,
)
from .plan import attach_memory, merged_intent, last_result, server_profile_update_status
from ..candidate_budget import validate_candidate_limits
from ..clarification import merge_pending_questions, has_answered_waiting_turn
from ..tools.profile_fields import FIELDS, METADATA, validate_profile, source_with_reads
from ..state_policy import finalize_patch

_ALLOWED_INTENT_STATUS = {"not_needed", "pending", "waiting", "done"}


def _profile_pending_changes(observations: list[ToolObservation], source: dict[str, Any]) -> dict[str, Any]:
    """영구 변경 제안만 승인 대기 상태로 보관. 원본 프로필 저장은 금지한다."""
    basic: list[dict[str, Any]] = []
    financial: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], Any] = {}
    used_change_ids: set[str] = set()
    for obs in observations:
        if obs["name"] != "profile_agent":
            continue
        req = obs.get("request", {})
        if req.get("operation", "prepare") != "prepare" or req.get("is_temporary") is not False:
            continue
        if obs["result"].get("changes") and req.get("change_requested") is not True:
            raise SupervisorContractError("Profile changes require explicit change_requested before approval")
        for ch in obs["result"].get("changes", []):
            field = ch.get("field")
            domain = ch.get("domain")
            if not isinstance(field, str) or not field or domain not in ("basic", "financial"):
                raise SupervisorContractError("Profile 실제 변경안에 domain/field가 필요합니다.")
            if not isinstance(ch.get("change_id"), str) or not ch["change_id"]:
                raise SupervisorContractError("Profile 실제 변경안에는 고유 change_id가 필요합니다.")
            domain_key = f"{domain}_profile"
            if field not in FIELDS[domain_key] - METADATA or "value" not in ch:
                raise SupervisorContractError("Invalid profile change field (v3.4)")
            validate_profile(domain_key, {field: ch["value"]})
            if field == "goals":
                base = (source.get("basic_profile") or {}).get("goals")
                if not isinstance(base, list):
                    raise SupervisorContractError("Load existing goals before proposing a saved change")
                new_ids = {g.get("goal_id") for g in (ch["value"] or [])}
                old_ids = {g.get("goal_id") for g in base if g.get("goal_id")}
                removal = ch.get("remove_goal_ids", [])
                if not isinstance(removal, list) or any(not isinstance(ref, str) for ref in removal):
                    raise SupervisorContractError("remove_goal_ids must be a string list")
                removed = set(removal)
                if ch["value"] is None or old_ids - new_ids != removed or not removed <= old_ids:
                    raise SupervisorContractError("Goal proposal would drop existing goals without explicit removal")
            key = (domain, field)
            if key in seen:
                if seen[key] != ch.get("value"):
                    raise SupervisorContractError("한 번의 요청에 충돌하는 프로필 변경안이 있습니다.")
                continue
            if ch["change_id"] in used_change_ids:
                raise SupervisorContractError("서로 다른 프로필 변경안의 change_id가 중복되었습니다.")
            used_change_ids.add(ch["change_id"])
            seen[key] = ch.get("value")
            # 요청만으로 applied가 되지 않도록 강제한다.
            item = {"change_id": ch["change_id"],
                    "field": field, "value": ch.get("value"), "status": "needs_confirmation"}
            (basic if domain == "basic" else financial).append(item)
    result: dict[str, Any] = {}
    if basic:
        result["basic_profile_changes"] = basic
    if financial:
        result["financial_profile_changes"] = financial
    return result


def _require_current_verified_results(source: dict[str, Any]) -> None:
    """Main Graph 검수를 거친 현재 run만 Final 생성 단계로 통과시킨다."""
    plan = source.get("execution_plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise SupervisorContractError("검증된 분석 결과 없이 Final로 진입할 수 없습니다 (현재 계획 없음).")
    if any(q.get("status") == "pending" for q in source.get("pending_input", []) if isinstance(q, dict)):
        raise SupervisorContractError("해결되지 않은 필수 질문이 있어 Final로 진입할 수 없습니다.")
    tasks = plan["tasks"]
    if any(t.get("status") not in {"completed", "skipped", "failed"} for t in tasks):
        raise SupervisorContractError("실행 중이거나 대기 중인 전문 작업이 있습니다.")
    complete = [t for t in tasks if t.get("status") == "completed"]
    if not complete:
        raise SupervisorContractError("검증된 분석 결과 없이 Final로 진입할 수 없습니다.")
    results = source.get("agent_results") or {}
    reviews = source.get("review_results") or {}
    for task in complete:
        task_id = task.get("task_id")
        run_id = task.get("run_id")
        result = results.get(task_id, {})
        review = reviews.get(task_id, {})
        if (not isinstance(run_id, str) or not run_id
                or not isinstance(result, dict)
                or result.get("status") != "completed"
                or result.get("run_id") != run_id
                or result.get("plan_revision") != plan.get("revision")
                or result.get("scenario_id") != task.get("scenario_id")
                or not isinstance(review, dict)
                or review.get("target_run_id") != run_id
                or not isinstance(review.get("evidence"), dict)
                or review["evidence"].get("status") != "passed"
                or not isinstance(review.get("reflection"), dict)
                or review["reflection"].get("status") != "passed"):
            raise SupervisorContractError("현재 실행의 Evidence/Reflection 검증 결과가 부족합니다.")


def run(state: SupervisorWorkingState, *, max_repairs: int) -> dict[str, Any]:
    decision = state.get("decision") or {}
    src = source_with_reads(state)
    kind = decision.get("kind")
    patch: dict[str, Any] = {}
    route: str | None = None
    try:
        if kind == "submit_plan":
            if state.get("candidate_error"):
                raise SupervisorContractError(state["candidate_error"])
            patch = dict(state.get("candidate_patch") or {})
            planned = (patch.get("execution_plan") or {}).get("tasks", [])
            for question in src.get("pending_input", []):
                if not isinstance(question, dict) or question.get("status") != "pending":
                    continue
                target, branch = question.get("target"), question.get("scenario_id")
                if branch is None or any(task.get("scenario_id") == branch or task.get("task_id") == target for task in planned):
                    raise SupervisorContractError("필수 사용자 질문이 해결되지 않아 계획을 발행할 수 없습니다.")
            scenario = patch.get("scenario_state", src.get("scenario_state"))
            if scenario is not None:
                if not isinstance(scenario, dict):
                    raise SupervisorContractError("scenario_state가 올바르지 않습니다.")
                # 기존 MainState에서 넘겨온 분기도 최종 경계에서 같은 규칙으로 확인한다.
                from ..tools.scenarios import prepare_scenarios
                prepare_scenarios(scenario)
            validate_intent_scenarios(patch.get("intent"), scenario)
            validate_candidate_limits(patch.get("execution_plan"), scenario)
            route = "plan_validator"
        elif kind == "ask_user":
            # 같은 turn에서 준비된 분기와 기존 질문/답변을 보존한다.
            prepared = last_result(state.get("observations", []), "prepare_scenarios")
            active_scenario = prepared["scenario_state"] if prepared else src.get("scenario_state")
            pending = merge_pending_questions(decision.get("pending_input"), src, active_scenario)
            patch = {"pending_input": pending, "phase": "waiting_for_user"}
            if prepared:
                patch["scenario_state"] = active_scenario
            if "intent" in decision:
                patch["intent"] = merged_intent(src, decision)
            profile_changes = _profile_pending_changes(state.get("observations", []), src)
            patch.update(profile_changes)
            approval_status = server_profile_update_status(src)
            if profile_changes or approval_status in {"pending", "waiting"}:
                # 새로운 변경안 또는 기존 승인 대기 기록은 서버가 관리한다.
                intent = deepcopy(patch.get("intent") or src.get("intent") or {})
                intent.setdefault("supplied_values", {})
                intent["profile_update_status"] = "waiting" if profile_changes else approval_status
                patch["intent"] = intent
            attach_memory(src, state.get("observations", []), patch)
            route = "ask_user"
        elif kind == "compose":
            if has_answered_waiting_turn(src):
                raise SupervisorContractError("Newly resolved answers require a current plan before composition")
            _require_current_verified_results(src)
            patch = {"phase": "composing"}
            if "intent" in decision:
                patch["intent"] = merged_intent(src, decision)
            attach_memory(src, state.get("observations", []), patch)
            route = "final_advice"
        else:
            raise SupervisorContractError(decision.get("reason", "유효하지 않은 Supervisor 판단"))
        if kind != "ask_user" and (server_profile_update_status(src) in {"pending", "waiting"}
                or _profile_pending_changes(state.get("observations", []), src)):
            raise SupervisorContractError("Profile changes require confirmation before planning/composing")
        intent = patch.get("intent")
        if intent is not None:
            status = intent.get("profile_update_status")
            if status is not None and status not in _ALLOWED_INTENT_STATUS:
                raise SupervisorContractError("profile_update_status가 올바르지 않습니다.")
            if status == "done" and (src.get("intent") or {}).get("profile_update_status") != "done":
                raise SupervisorContractError("프로필 저장 완료를 LLM이 직접 선언할 수 없습니다.")
        patch = finalize_patch(state, patch, route)
        return {"main_patch": patch, "next_node": route, "retry": False, "done": True}
    except (KeyError, TypeError, SupervisorContractError) as exc:
        repairs = state.get("repair_count", 0) + 1
        if repairs > max_repairs:
            raise SupervisorContractError(f"Supervisor 계획 정합성 복구 실패: {exc}") from exc
        return {"issues": [*state.get("issues", []), str(exc)],
                "repair_count": repairs, "retry": True, "done": False,
                "candidate_patch": {}}


def route(state: SupervisorWorkingState) -> str:
    return "reason" if state.get("retry", False) else "end"
