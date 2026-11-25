# Supervisor 내부 노드 및 MainState 반환의 쓰기 경계를 검사한다.
# 새 State 키를 추가하거나 금융 판단을 수행하지 않는다.
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import SupervisorContractError, INTENT_FIELDS
from .tools.profile_fields import attach_read_snapshot, validate_overlay

# Reason/Tools/Plan/Finalize의 유일한 쓰기 영역. source는 초기화 이후 읽기 전용이다.
NODE_WRITES = {
    "reason": frozenset({"decision", "reason_calls", "retry"}),
    "tools": frozenset({"observations", "tool_calls"}),
    "plan": frozenset({"candidate_patch", "candidate_error"}),
    "finalize": frozenset({"main_patch", "next_node", "retry", "done", "issues", "repair_count", "candidate_patch"}),
}

_COMMON = frozenset({"phase", "intent", "memory"})
RETURN_WRITES = {
    "plan_validator": _COMMON | {"execution_plan", "scenario_state"},
    "ask_user": _COMMON | {"pending_input", "scenario_state", "basic_profile_changes", "financial_profile_changes"},
    "final_advice": _COMMON,
}
RETURN_REQUIRED = {
    "plan_validator": {"phase", "intent", "execution_plan"},
    "ask_user": {"phase", "pending_input"},
    "final_advice": {"phase"},
}
RETURN_PHASE = {"plan_validator": "planning", "ask_user": "waiting_for_user", "final_advice": "composing"}


def checked_node_update(name: str, update: Any) -> dict[str, Any]:
    """내부 Graph의 상태 업데이트를 적용하기 전에 허용 목록으로 검사한다."""
    if name not in NODE_WRITES or not isinstance(update, dict):
        raise SupervisorContractError("Invalid Supervisor node update")
    unknown = set(update) - NODE_WRITES[name]
    if unknown:
        raise SupervisorContractError(f"{name} node cannot write State keys: {', '.join(sorted(map(str, unknown)))}")
    if name == "finalize":
        expected = ({"main_patch", "next_node", "retry", "done"} if update.get("done") is True else
                    {"issues", "repair_count", "retry", "done", "candidate_patch"})
    else:
        expected = NODE_WRITES[name]
    if set(update) != expected:
        raise SupervisorContractError(f"{name} node returned an incomplete or mixed State update")
    return deepcopy(update)


def finalize_patch(state: dict[str, Any], patch: Any, route: str) -> dict[str, Any]:
    """검증된 부분 패치만 통과시킨다. 프로필 보충은 별도 서버 조회 경로만 허용한다."""
    if route not in RETURN_WRITES or not isinstance(patch, dict):
        raise SupervisorContractError("Invalid Supervisor return route or patch")
    unknown = set(patch) - RETURN_WRITES[route]
    if unknown:
        raise SupervisorContractError(f"{route} cannot write MainState keys: {', '.join(sorted(map(str, unknown)))}")
    if not RETURN_REQUIRED[route] <= set(patch) or patch.get("phase") != RETURN_PHASE[route]:
        raise SupervisorContractError("MainState patch does not match its return route")
    if "intent" in patch:
        intent = patch["intent"]
        if not isinstance(intent, dict) or set(intent) - INTENT_FIELDS:
            raise SupervisorContractError("intent contains unsupported fields")
        if "supplied_values" in intent:
            validate_overlay(intent["supplied_values"], supplied=True)
    # 원본 프로필의 직접 변경은 위 allowlist에서 금지되며, 아래는 미로딩 필드만 보충한다.
    clean = deepcopy(patch)
    attach_read_snapshot(state, clean)
    return clean
