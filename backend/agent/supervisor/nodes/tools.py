# Profile/Memory 도구와 시나리오 도구를 실행한다. 저장·금융 계산은 하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from typing import Any, TYPE_CHECKING
from ..contracts import SupervisorContractError
from ..state import SupervisorWorkingState
from ..tools.ports import (
    normalize_profile_result,
    memory_request_key, validate_memory_request, validate_profile_request,
)
from ..tools.scenarios import prepare_verified_scenarios
from ..tools.memory import call_memory, merge_memory
from ..tools.profile_fields import source_with_reads, FIELDS
from ..tools.profile_inspection import inspect_fields
from copy import deepcopy
if TYPE_CHECKING:
    from ..supervisor_main import Supervisor


def run(supervisor: "Supervisor", state: SupervisorWorkingState) -> dict[str, Any]:
    if state.get("tool_calls", 0) >= supervisor.max_tool_calls:
        raise SupervisorContractError("Supervisor 내부 Tool 호출 상한 초과")
    decision = state["decision"]
    name = decision["tool_name"]
    args = decision["tool_input"]
    if name == "profile_agent":
        validate_profile_request(args)
        if "fields" in args:
            result = inspect_fields(supervisor, state, args["fields"])
        else:
            if supervisor.ports.profile_agent is None:
                return {"observations": [*state.get("observations", []), {"name": name, "request": deepcopy(args), "result": {"status": "failed", "error_code": "profile_service_not_connected"}}], "tool_calls": state.get("tool_calls", 0) + 1}
            try:
                current = source_with_reads(state)
                payload = deepcopy(args)
                # Source only from the real snapshot; never from memory or scenario overrides.
                snapshot = {d: deepcopy(current[d]) for d in FIELDS if d in current}
                payload["current_profile"] = deepcopy(snapshot)
                # 텍스트에서 저장 의도를 다시 추정하지 않도록 명시 플래그를 항상 주입한다.
                payload["change_requested"] = args.get("change_requested", False)
                raw = supervisor.ports.profile_agent(payload)
            except Exception:
                result = {"status": "failed", "error_code": "profile_service_error"}
            else:
                if isinstance(raw, dict) and raw.get("status") in ("failed", "access_denied", "unavailable"):
                    result = {"status": "failed", "error_code": f"profile_{raw['status']}"}
                else:
                    result = normalize_profile_result(raw, current_profile=snapshot,
                                                      required_fields=args.get("required_fields"))
                    if result.get("changes") and (args["is_temporary"] or args.get("change_requested") is not True):
                        raise SupervisorContractError("Profile changes require an explicit non-temporary change_requested")
    elif name == "retrieve_memory":
        validate_memory_request(args)
        key = memory_request_key(args)
        cached = None
        for obs in reversed(state.get("observations", [])):
            if obs["name"] != name:
                continue
            try:
                old_key = memory_request_key(obs.get("request") or {})
            except SupervisorContractError:
                continue
            if old_key == key:
                cached = obs["result"]
                break
        if merge_memory(state["source"], state.get("observations", [])).get("retrieval_status") == "access_denied":
            result = {"retrieval_status": "access_denied", "retrieved": []}
        elif cached is not None:
            result = deepcopy(cached)
        else:
            result = call_memory(supervisor.ports.retrieve_memory, args)
    elif name == "prepare_scenarios":
        result = prepare_verified_scenarios(args, state)
    else:
        raise SupervisorContractError("지원되지 않는 내부 도구입니다.")
    observations = [*state.get("observations", []), {
        "name": name, "request": args, "result": result,
    }]
    return {"observations": observations, "tool_calls": state.get("tool_calls", 0) + 1}
