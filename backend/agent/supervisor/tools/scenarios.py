# Supervisor 내부 시나리오 준비 도구. 가상 행동을 정규화할 뿐 계산하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from typing import Any
from copy import deepcopy
import math
from decimal import Decimal

from ..contracts import MAX_SCENARIOS, SupervisorContractError

_ALLOWED_ACTIONS = ("keep", "terminate", "subscribe")
from .profile_fields import validate_overlay

# Fixed schema keys are shared by the API schema and the Python validator.
ASSUMPTION_PROPERTIES = {
    "horizon_months": {"type": "integer", "minimum": 1, "maximum": 600},
    "allocation_pct": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100}},
    "goal_mode": {"type": "string", "enum": ["merge", "replace"]},
    "notes": {"type": "string", "description": "Additional hypothetical conditions; specialist interprets them, not a State patch"},
}
CRITERIA_PROPERTIES = {
    "monthly_contribution_krw": {"type": "integer", "minimum": 1},
    "term_months": {"type": "integer", "minimum": 1, "maximum": 600},
    "annual_rate_pct": {"type": "number"},
    "product_type": {"type": "string"},
    "product_ref": {"type": "string", "description": "Search hint only"},
    "notes": {"type": "string"},
}
ACTION_PROPERTIES = {
    "action": {"type": "string", "enum": list(_ALLOWED_ACTIONS)},
    "holding_ref": {"type": ["string", "null"]},
    "status": {"type": ["string", "null"], "enum": ["hypothetical", "planned", None]},
    "target_criteria": {"type": "object", "properties": CRITERIA_PROPERTIES, "additionalProperties": False},
}
ASSUMPTION_PROPERTIES["holding_actions"] = {"type": "array", "items": {"type": "object", "properties": ACTION_PROPERTIES,
                                            "required": ["action"], "additionalProperties": False}}


def assumption_json_schema() -> dict[str, Any]:
    return {"type": "object", "properties": deepcopy(ASSUMPTION_PROPERTIES), "additionalProperties": False}


def _only_fields(value: Any, allowed: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise SupervisorContractError(f"{label}: object required")
    unknown = set(value) - set(allowed)
    if unknown:
        keys = ", ".join(sorted(str(k)[:64] for k in unknown)[:8])
        raise SupervisorContractError(f"{label}: unsupported keys {keys}; use registered keys or assumptions.notes / objective")


def normalize_holding_actions(actions: Any, baseline: str | None, *, conditional: bool = False) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        raise SupervisorContractError("holding_actions must be a list")
    normalized_actions: list[dict[str, Any]] = []
    seen_holdings: set[str] = set()
    for a in actions:
        _only_fields(a, ACTION_PROPERTIES, "holding_actions")
        action = a.get("action")
        if action not in _ALLOWED_ACTIONS:
            raise SupervisorContractError("Invalid hypothetical holding action")
        ref = a.get("holding_ref")
        if ref is not None and (not isinstance(ref, str) or not ref.strip()):
            raise SupervisorContractError("Invalid holding_ref")
        if a.get("status") not in (None, "hypothetical", "planned"):
            raise SupervisorContractError("A hypothetical action cannot be applied/completed")
        normalized = deepcopy(a)
        if action in ("keep", "terminate"):
            if not conditional and (not ref or not baseline):
                raise SupervisorContractError("keep/terminate requires baseline_ref and holding_ref")
            if ref is not None:
                if ref in seen_holdings:
                    raise SupervisorContractError("Duplicate or conflicting holding actions")
                seen_holdings.add(ref)
            if "target_criteria" in a:
                raise SupervisorContractError("target_criteria is only for subscribe")
        else:
            if ref is not None:
                raise SupervisorContractError("subscribe cannot reference an existing holding")
            criteria = a.get("target_criteria", {})
            _only_fields(criteria, CRITERIA_PROPERTIES, "subscribe.target_criteria")
            for key in ("monthly_contribution_krw", "term_months"):
                if key in criteria and (type(criteria[key]) is not int or criteria[key] < 1 or (key == "term_months" and criteria[key] > 600)):
                    raise SupervisorContractError(f"Invalid {key}: positive integer required")
            if "annual_rate_pct" in criteria and (type(criteria["annual_rate_pct"]) not in (int, float) or not math.isfinite(criteria["annual_rate_pct"])):
                raise SupervisorContractError("Invalid annual_rate_pct: finite number required")
            for key in ("product_type", "product_ref", "notes"):
                if key in criteria and (not isinstance(criteria[key], str) or not criteria[key].strip()):
                    raise SupervisorContractError(f"Invalid target_criteria.{key}: nonempty string required")
            normalized["status"] = "hypothetical"
        normalized_actions.append(normalized)
    return normalized_actions


def prepare_scenarios(args: dict[str, Any]) -> dict[str, Any]:
    """LLM이 제안한 분기를 결정론적으로 검사한다.

    사용자 금융정보 원본은 변경하지 않으며, 실제 상품 존재/자격/수익도 검증하지 않는다.
    """
    _only_fields(args, {"scenarios", "baseline_ref"}, "scenario_state")
    items = args.get("scenarios")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SCENARIOS:
        raise SupervisorContractError("시나리오 개수는 1~3개여야 합니다.")
    baseline = args.get("baseline_ref")
    if baseline is not None and (not isinstance(baseline, str) or not baseline.strip()):
        raise SupervisorContractError("baseline_ref 형식이 잘못되었습니다.")
    # ID는 시스템 식별자다. 먼저 명시 ID를 예약하여 뒤쪽 명시 ID와도 충돌하지 않게 한다.
    # 누락/null/빈 ID만 자동 부여하고, 명시 중복 또는 잘못된 형식은 조용히 변경하지 않는다.
    used_ids: set[str] = set()
    for item in items:
        _only_fields(item, {"id", "label", "overrides", "assumptions"}, "scenario")
        sid = item.get("id")
        if sid is None or sid == "":
            continue
        if not isinstance(sid, str) or not sid.strip() or sid != sid.strip():
            raise SupervisorContractError("명시된 시나리오 ID 형식이 잘못되었습니다.")
        if sid in used_ids:
            raise SupervisorContractError("명시된 시나리오 ID가 중복되었습니다.")
        used_ids.add(sid)
    output: list[dict[str, Any]] = []
    horizons: list[int | None] = []
    allocations: list[set[str] | None] = []
    next_id = 1
    for item in items:
        sid = item.get("id")
        if sid is None or sid == "":
            while f"s_{next_id:02d}" in used_ids:
                next_id += 1
            sid = f"s_{next_id:02d}"
            used_ids.add(sid)
            next_id += 1
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SupervisorContractError("시나리오 표시명이 필요합니다.")
        overrides = item.get("overrides", {})
        assumptions = item.get("assumptions", {})
        validate_overlay(overrides)
        _only_fields(assumptions, ASSUMPTION_PROPERTIES, "assumptions")
        if "goal_mode" in assumptions and assumptions["goal_mode"] not in ("merge", "replace"):
            raise SupervisorContractError("assumptions.goal_mode must be merge or replace")
        if "notes" in assumptions and (not isinstance(assumptions["notes"], str) or not assumptions["notes"].strip()):
            raise SupervisorContractError("assumptions.notes must be a nonempty string")
        horizon = assumptions.get("horizon_months")
        if "horizon_months" in assumptions and (type(horizon) is not int or horizon < 1 or horizon > 600):
            raise SupervisorContractError("분석 기간은 1~600개월이어야 합니다.")
        horizons.append(horizon)
        allocation = assumptions.get("allocation_pct")
        if "allocation_pct" in assumptions:
            if (not isinstance(allocation, dict) or not allocation
                    or any(not isinstance(k, str) or not k.strip() or type(v) not in (int, float)
                           or not 0 <= v <= 100 or not math.isfinite(v) for k, v in allocation.items())
                    or sum((Decimal(str(v)) for v in allocation.values()), Decimal(0)) != 100):
                raise SupervisorContractError("allocation_pct는 0~100의 배분 비율이며 합계는 100이어야 합니다.")
            allocations.append(set(allocation))
        else:
            allocations.append(None)
        actions = assumptions.get("holding_actions", [])
        normalized_actions = normalize_holding_actions(actions, baseline)
        normalized_assumptions = deepcopy(assumptions)
        if actions:
            normalized_assumptions["holding_actions"] = normalized_actions
        output.append({
            "id": sid,
            "label": label,
            "overrides": deepcopy(overrides),
            "assumptions": normalized_assumptions,
        })
    # 비교 분기의 평가 기간이 제각각이면 숫자의 단순 비교가 불가능하다.
    if len(output) > 1 and any(v is not None for v in horizons):
        if any(v is None for v in horizons) or len(set(horizons)) > 1:
            raise SupervisorContractError("비교 시나리오의 horizon_months는 동일해야 합니다.")
    if len(output) > 1 and any(a is not None for a in allocations):
        if any(a != allocations[0] for a in allocations):
            raise SupervisorContractError("비중 비교에서는 동일한 배분 항목을 사용하세요(미배분은 0%).")
    state: dict[str, Any] = {"scenarios": output}
    if baseline:
        state["baseline_ref"] = baseline
    return {"scenario_state": state}


def prepare_verified_scenarios(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """두 제출 경로에서 동일하게 계약 참조의 실제 출처까지 검사한다."""
    result = prepare_scenarios(args)
    proposed_baseline = result["scenario_state"].get("baseline_ref")
    if proposed_baseline:
        confirmed = [obs["result"] for obs in state.get("observations", [])
                     if obs["name"] == "profile_agent" and obs.get("request", {}).get("operation") == "inspect"
                     and obs["result"].get("baseline_ref") == proposed_baseline]
        ready_specs = [spec for obs in state.get("observations", []) if obs["name"] == "profile_agent"
                       and obs["result"].get("baseline_ref") == proposed_baseline
                       for spec in obs["result"].get("branch_specs", []) if spec.get("status") == "ready"]
        existing = state["source"].get("scenario_state") or {}
        if not confirmed and not ready_specs and existing.get("baseline_ref") != proposed_baseline:
            raise SupervisorContractError("Unverified baseline_ref")
        refs = {h["holding_id"] for r in confirmed
                for h in (r.get("profile", {}).get("financial_profile", {}).get("holdings") or [])
                if h.get("status") == "active"}
        if existing.get("baseline_ref") == proposed_baseline:
            refs.update(a["holding_ref"] for branch in existing.get("scenarios", [])
                        for a in branch.get("assumptions", {}).get("holding_actions", [])
                        if a.get("action") in {"keep", "terminate"} and a.get("holding_ref"))
        for branch in result["scenario_state"]["scenarios"]:
            for a in branch.get("assumptions", {}).get("holding_actions", []):
                if a.get("action") not in {"keep", "terminate"}:
                    continue
                branch_verified = any(spec["id"] == branch["id"] and any(isinstance(x, dict) and x.get("holding_ref") == a.get("holding_ref") and x.get("action") == a["action"] for x in spec.get("holding_actions", [])) for spec in ready_specs)
                if a.get("holding_ref") not in refs and not branch_verified:
                    raise SupervisorContractError("holding_ref not present in verified baseline")
    return result
