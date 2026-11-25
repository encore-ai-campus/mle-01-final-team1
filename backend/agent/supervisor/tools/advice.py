"""후속 상담의 검증된 이전 시나리오를 읽고 선택·재분기한다.

저장소와 인증은 서버의 읽기 Port가 담당한다. 기억 요약에서 과거 조건을 추정하지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..contracts import SupervisorContractError
from .scenarios import prepare_scenarios, prepare_verified_scenarios


def prior_advice_observation(source: dict[str, Any], port: Any) -> dict[str, Any] | None:
    """새 turn에서 서버가 보존한 직전 advice_ref만 한 번 조회한다."""
    memory = source.get("memory") or {}
    ref = memory.get("last_advice_ref")
    if not isinstance(ref, str) or not ref.strip() or source.get("execution_plan") or source.get("scenario_state"):
        return None
    result: dict[str, Any] = {"advice_ref": ref, "status": "unavailable"}
    if memory.get("retrieval_status") == "access_denied":
        result = {"status": "access_denied"}
    elif port is not None:
        try:
            raw = port(ref)
            if not isinstance(raw, dict) or raw.get("advice_ref") != ref:
                raise SupervisorContractError("상담 참조 불일치")
            if raw.get("status") == "access_denied":
                result = {"status": "access_denied"}
            elif raw.get("status") == "found":
                scenario = raw.get("scenario_state")
                if scenario is None:
                    result = {"advice_ref": ref, "status": "no_scenarios"}
                else:
                    normalized = prepare_scenarios(scenario)["scenario_state"]
                    result = {"advice_ref": ref, "status": "found", "scenario_state": normalized}
        except Exception:
            # 예외 본문이나 다른 사용자의 스냅샷을 LLM에 제공하지 않는다.
            result = {"advice_ref": ref, "status": "unavailable"}
    return {"name": "prior_advice_snapshot", "result": result}


def selected_scenario(decision: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """선택된 저장 분기를 그대로 복원하거나 변경된 가정만 덮어 새 분기들을 만든다."""
    sid = decision.get("selected_scenario_id")
    if sid is None:
        return None
    intent = decision.get("intent") or {}
    ref = intent.get("target_advice_ref")
    if intent.get("operation") not in {"continue", "revise", "compare"}:
        raise SupervisorContractError("이전 시나리오 선택에는 continue/revise/compare가 필요합니다.")
    if any(o.get("name") == "retrieve_memory" and o.get("result", {}).get("retrieval_status") == "access_denied" for o in state.get("observations", [])):
        raise SupervisorContractError("이전 상담 접근이 거부되었습니다.")
    snapshot = next((o["result"] for o in state.get("observations", [])
                     if o.get("name") == "prior_advice_snapshot"), {})
    if snapshot.get("status") != "found" or not ref or snapshot.get("advice_ref") != ref:
        raise SupervisorContractError("선택한 이전 상담 스냅샷을 확인할 수 없습니다. 필요한 조건을 사용자에게 확인하세요.")
    previous = snapshot["scenario_state"]
    base = next((b for b in previous["scenarios"] if b["id"] == sid), None)
    if base is None:
        raise SupervisorContractError("선택한 이전 시나리오 ID가 존재하지 않습니다.")
    draft = decision.get("scenario_state")
    if draft is None:
        proposed = {"scenarios": [deepcopy(base)]}
    else:
        items = draft.get("scenarios")
        if not isinstance(items, list) or not items:
            raise SupervisorContractError("선택안의 세부 분석에는 유효한 시나리오 초안이 필요합니다.")
        proposed = {"scenarios": []}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("overrides", {}), dict) or not isinstance(item.get("assumptions", {}), dict):
                raise SupervisorContractError("선택안 변경 필드 형식 오류")
            proposed["scenarios"].append({
                **deepcopy(item),
                "overrides": {**deepcopy(base["overrides"]), **deepcopy(item.get("overrides", {}))},
                "assumptions": {**deepcopy(base["assumptions"]), **deepcopy(item.get("assumptions", {}))},
            })
    baseline = (draft or {}).get("baseline_ref", previous.get("baseline_ref"))
    # 과거 baseline은 현재 계약의 증명이 아니다. 실제 keep/terminate가 남으면
    # 기존 검증 경로에서 현재 확인된 동일 참조가 있어야만 통과한다.
    if any(a.get("action") in {"keep", "terminate"}
           for b in proposed["scenarios"]
           for a in b.get("assumptions", {}).get("holding_actions", []) if isinstance(a, dict)):
        if baseline:
            proposed["baseline_ref"] = baseline
    return prepare_verified_scenarios(proposed, state)["scenario_state"]
