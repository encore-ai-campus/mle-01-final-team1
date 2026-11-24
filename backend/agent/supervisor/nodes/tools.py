# Profile/Memory 도구와 시나리오 도구를 실행한다. 저장·금융 계산은 하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations
from typing import Any, TYPE_CHECKING
from ..contracts import SupervisorContractError
from ..state import SupervisorWorkingState
from ..tools.ports import (
    normalize_profile_result,
    validate_memory_request, validate_profile_request,
)
from ..tools.scenarios import prepare_verified_scenarios
from ..tools.memory import call_memory, merge_memory
from ..tools.profile_fields import select_fields, validate_read_fields, source_with_reads, FIELDS, METADATA
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
            paths = validate_read_fields(args["fields"])
            # Reuse current State and earlier read observations; fetch only un-loaded keys.
            available = select_fields(source_with_reads(state), paths)
            attempted = set()
            for obs in state.get("observations", []):
                if obs["name"] == "profile_agent" and obs.get("request", {}).get("operation") == "inspect" and obs["result"].get("status") != "failed":
                    attempted.update(obs.get("request", {}).get("fields", []))
                    if "fields" in obs.get("request", {}):
                        for domain, data in select_fields(obs["result"].get("profile", {}), paths).items():
                            target = available.setdefault(domain, {})
                            for key, value in data.items():
                                target.setdefault(key, value)
            needed = [f for f in paths if f not in attempted and f.split(".")[1] not in available.get(f.split(".")[0], {})]
            if needed:
                if supervisor.ports.get_profile_fields is None:
                    # The extraction MVP cannot certify a DB read. Return only loaded facts.
                    result = {"profile": available, "missing_info": [f.split(".")[1] for f in paths if available.get(f.split(".")[0], {}).get(f.split(".")[1]) is None], "unloaded_fields": needed}
                    return {"observations": [*state.get("observations", []), {"name": name, "request": deepcopy(args), "result": result}], "tool_calls": state.get("tool_calls", 0) + 1}
                try:
                    raw = supervisor.ports.get_profile_fields(needed)
                    # The read service is bound to the authenticated user AND a fixed snapshot.
                    raw = {"profile": raw, "missing_info": []}
                except Exception:
                    result = {"status": "failed", "error_code": "profile_service_error"}
                else:
                    read = normalize_profile_result(raw)
                    if supervisor.ports.get_profile_fields is not None:
                        for domain in FIELDS:
                            old = state["source"].get(domain, {})
                            fresh = read["profile"].get(domain, {})
                            for key in METADATA:
                                if old.get(key) is not None and fresh.get(key) is not None and old[key] != fresh[key]:
                                    raise SupervisorContractError("Profile read snapshot mismatch; refresh via Context Loader")
                    for domain, data in select_fields(read["profile"], needed).items():
                        available.setdefault(domain, {}).update(data)
                    result = {"profile": available, "missing_info": [f.split(".")[1] for f in paths if available.get(f.split(".")[0], {}).get(f.split(".")[1]) is None]}
                    # Only the dedicated, trusted read port is allowed to hydrate MainState.
                    if supervisor.ports.get_profile_fields is not None:
                        result["verified_read"] = True
                        result["verified_fields"] = select_fields(read["profile"], needed)
            else:
                result = {"profile": available, "missing_info": [f.split(".")[1] for f in paths if available.get(f.split(".")[0], {}).get(f.split(".")[1]) is None]}
        else:
            if supervisor.ports.profile_agent is None:
                return {"observations": [*state.get("observations", []), {"name": name, "request": deepcopy(args), "result": {"status": "failed", "error_code": "profile_service_not_connected"}}], "tool_calls": state.get("tool_calls", 0) + 1}
            try:
                current = source_with_reads(state)
                payload = deepcopy(args)
                # Source only from the real snapshot; never from memory or scenario overrides.
                payload["current_profile"] = {d: deepcopy(current[d]) for d in FIELDS if d in current}
                raw = supervisor.ports.profile_agent(payload)
            except Exception:
                result = {"status": "failed", "error_code": "profile_service_error"}
            else:
                result = normalize_profile_result(raw)
    elif name == "retrieve_memory":
        validate_memory_request(args)
        cached = next((obs["result"] for obs in reversed(state.get("observations", [])) if obs["name"] == name and obs.get("request") == args), None)
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
