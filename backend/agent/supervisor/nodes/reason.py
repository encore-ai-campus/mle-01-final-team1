# Supervisor의 유일한 LLM 판단 노드. 판단에 필요한 최소 Context를 구성한다.
# 원본 State와 도구 기록은 보존하고 LLM 전달용 중복 정보만 줄인다.
from __future__ import annotations
from typing import Any, TYPE_CHECKING
from copy import deepcopy
from ..contracts import SupervisorContractError, validate_decision
from ..state import SupervisorWorkingState
from ..tools.profile_fields import profile_availability, source_with_reads
from ..tools.memory import merge_memory
from ..tools.ports import memory_key
if TYPE_CHECKING:
    from ..supervisor_main import Supervisor


def _messages(source: dict[str, Any]) -> list[dict[str, str]]:
    """상담 요약이 있으면 최근 맥락만, 없으면 기존 범위를 유지한다."""
    messages = []
    for msg in source.get("messages") or []:
        kind = msg.get("type", msg.get("role")) if isinstance(msg, dict) else getattr(msg, "type", None)
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if kind in {"human", "user", "ai", "assistant"} and isinstance(content, str):
            messages.append({"type": kind, "content": content})
    summary = source.get("conversation_summary")
    limit = 4 if isinstance(summary, str) and summary.strip() else 8
    recent = messages[-limit:]
    # 마지막 질문은 assistant 메시지가 연속돼도 반드시 남긴다. 본문을 자르지 않는다.
    if not any(m["type"] in {"human", "user"} for m in recent):
        last_user = next((m for m in reversed(messages) if m["type"] in {"human", "user"}), None)
        if last_user is not None:
            recent = [last_user, *recent]
    return recent


def _availability(source: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for path, status in profile_availability(source).items():
        domain, field = path.split(".", 1)
        grouped.setdefault(domain, {}).setdefault(status, []).append(field)
    return grouped


def _current_results(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """현재 계획·run에 해당하는 전문 결과와 검수만 LLM에 제공한다."""
    plan = source.get("execution_plan") or {}
    results = source.get("agent_results") or {}
    reviews = source.get("review_results") or {}
    current_results, current_reviews = {}, {}
    for task in plan.get("tasks", []):
        tid, run_id = task.get("task_id"), task.get("run_id")
        if not run_id:
            continue
        result = results.get(tid)
        if (isinstance(result, dict) and result.get("run_id") == run_id
                and result.get("plan_revision") == plan.get("revision")
                and result.get("scenario_id") == task.get("scenario_id")):
            current_results[tid] = {f: deepcopy(result[f]) for f in (
                "run_id", "plan_revision", "scenario_id", "status", "summary", "needed_fields", "warnings"
            ) if f in result}
        review = reviews.get(tid)
        if isinstance(review, dict) and review.get("target_run_id") == run_id:
            current_reviews[tid] = deepcopy(review)
    return current_results, current_reviews


def _compact_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """성공한 Scenario 요청은 결과로 대체하고 기억 본문은 단일 위치에서 제공한다."""
    compact: list[dict[str, Any]] = []
    latest_scenario = next((i for i in range(len(observations) - 1, -1, -1)
                            if observations[i]["name"] == "prepare_scenarios"
                            and "scenario_state" in observations[i]["result"]
                            and observations[i]["result"].get("status") != "failed"), None)
    for i, obs in enumerate(observations):
        name, result = obs["name"], obs["result"]
        request = obs.get("request") or {}
        clean_result = {k: deepcopy(v) for k, v in result.items()
                        if k not in {"verified_fields", "retrieved"}}
        if name == "prepare_scenarios" and "scenario_state" in result and result.get("status") != "failed":
            if i != latest_scenario:
                continue
            # 성공 결과에 이미 동일한 가정·ID가 들어 있으므로 요청 원문 불필요.
            item = {"name": name, "result": clean_result}
        else:
            if name == "profile_agent" and request.get("operation") == "inspect" and result.get("status") != "failed":
                request = {k: v for k, v in request.items() if k in {"operation", "is_temporary", "fields"}}
            # 조회 실패 및 Profile 준비의 실제/가상·저장 의도는 제거하지 않는다.
            item = {"name": name, "request": deepcopy(request), "result": clean_result}
        # 완전히 동일한 재조회만 제거한다. 오류 또는 서로 다른 사실을 요약하지 않는다.
        if item in compact:
            compact.remove(item)
        compact.append(item)
    return compact


def build_context(supervisor: "Supervisor", state: SupervisorWorkingState, count: int) -> dict[str, Any]:
    source = source_with_reads(state)
    observations = state.get("observations", [])
    keys = ("conversation_summary", "phase", "intent", "scenario_state", "execution_plan", "pending_input")
    view = {k: deepcopy(source[k]) for k in keys if k in source and source[k] not in (None, {}, [], "")}
    view["messages"] = _messages(source)
    results, reviews = _current_results(source)
    if results:
        view["agent_results"] = results
    if reviews:
        view["review_results"] = reviews
    memory = merge_memory(source, observations)
    view["memory"] = {k: deepcopy(v) for k, v in memory.items()
                      if k in {"last_advice_ref", "retrieval_status", "retrieved", "prefetch_status"}}
    # 기본/상세 조회에 이미 포함된 기억을 한 번만 전달한다. 마지막 상세 조회에서
    # 병합 상한을 넘어 누락된 항목은 별도로 보존한다(권한 거부 시 절대 전달하지 않음).
    context: dict[str, Any] = {
        "main_state": view,
        "profile_availability": _availability(source),
        "connected_services": {"profile_prepare": supervisor.ports.profile_agent is not None,
                               "profile_read": supervisor.ports.get_profile_fields is not None,
                               "memory_search": supervisor.ports.retrieve_memory is not None},
        "observations": _compact_observations(observations),
        "issues": deepcopy(state.get("issues", [])),
        "call_index": count,
    }
    if memory.get("retrieval_status") != "access_denied":
        latest_detail = next((o["result"] for o in reversed(observations) if o["name"] == "retrieve_memory"), None)
        if latest_detail and latest_detail.get("retrieval_status") == "found":
            seen = {memory_key(i) for i in memory.get("retrieved", [])}
            extra = [deepcopy(i) for i in latest_detail.get("retrieved", []) if memory_key(i) not in seen]
            if extra:
                context["latest_memory_detail"] = {"retrieval_status": "found", "retrieved": extra}
    return context


def run(supervisor: "Supervisor", state: SupervisorWorkingState) -> dict[str, Any]:
    count = state.get("reason_calls", 0) + 1
    if count > supervisor.max_reason_calls:
        raise SupervisorContractError("Supervisor LLM 판단 호출 상한을 초과했습니다.")
    context = build_context(supervisor, state, count)
    try:
        decision = supervisor.decider(context)
        validate_decision(decision)
    except SupervisorContractError as exc:
        return {"decision": {"kind": "invalid", "reason": str(exc)},
                "reason_calls": count, "retry": False}
    return {"decision": dict(decision), "reason_calls": count, "retry": False}


def route(state: SupervisorWorkingState) -> str:
    kind = state["decision"].get("kind")
    if kind == "call_tool":
        return "tools"
    if kind == "submit_plan":
        return "plan"
    return "finalize"
