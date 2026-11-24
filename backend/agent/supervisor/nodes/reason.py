# Supervisor의 유일한 LLM 판단 노드: 경로와 다음 작업을 선택한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from typing import Any, TYPE_CHECKING
from ..contracts import SupervisorContractError, validate_decision
from ..state import SupervisorWorkingState
from ..tools.profile_fields import profile_availability, source_with_reads
from ..tools.memory import merge_memory
from copy import deepcopy
if TYPE_CHECKING:
    from ..supervisor_main import Supervisor


def run(supervisor: "Supervisor", state: SupervisorWorkingState) -> dict[str, Any]:
    count = state.get("reason_calls", 0) + 1
    if count > supervisor.max_reason_calls:
        raise SupervisorContractError("Supervisor LLM 판단 호출 상한을 초과했습니다.")
    # ToolObservation은 Supervisor 내부에서만 보존한다.
    source = source_with_reads(state)
    keys = ("conversation_summary", "phase", "intent", "scenario_state", "execution_plan", "pending_input")
    view = {k: deepcopy(source[k]) for k in keys if k in source}
    view["messages"] = []
    for msg in source.get("messages", [])[-8:]:
        kind = msg.get("type", msg.get("role")) if isinstance(msg, dict) else getattr(msg, "type", None)
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if kind in {"human", "user", "ai", "assistant"} and isinstance(content, str):
            view["messages"].append({"type": kind, "content": content})
    view["agent_results"] = {k: {f: v[f] for f in ("run_id", "plan_revision", "scenario_id", "status", "summary", "needed_fields", "warnings") if f in v}
                             for k, v in (source.get("agent_results") or {}).items()}
    view["review_results"] = deepcopy(source.get("review_results") or {})
    view["memory"] = {k: v for k, v in merge_memory(source, state.get("observations", [])).items() if k in {"last_advice_ref", "retrieval_status", "retrieved", "prefetch_status"}}
    context = {
        "main_state": view,
        "profile_availability": profile_availability(source),
        "connected_services": {"profile_prepare": supervisor.ports.profile_agent is not None,
                               "profile_read": supervisor.ports.get_profile_fields is not None,
                               "memory_search": supervisor.ports.retrieve_memory is not None},
        "observations": [{**obs, "result": {k: v for k, v in obs["result"].items() if k not in {"verified_fields", "verified_read", "retrieved"}}} for obs in state.get("observations", [])],
        "issues": state.get("issues", []),
        "call_index": count,
    }
    # A targeted lookup may exceed the merged memory budget. Keep its newest
    # sanitized result available for reasoning, without leaking denied contents.
    if view["memory"].get("retrieval_status") != "access_denied":
        latest_detail = next((obs["result"] for obs in reversed(state.get("observations", [])) if obs["name"] == "retrieve_memory"), None)
        if latest_detail is not None:
            context["latest_memory_detail"] = deepcopy(latest_detail)
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
