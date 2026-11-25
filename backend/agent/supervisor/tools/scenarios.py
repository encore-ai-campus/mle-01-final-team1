# Supervisor 내부 시나리오 준비 도구. 가상 행동을 정규화할 뿐 계산하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from typing import Any
from copy import deepcopy
import math
from decimal import Decimal

from ..contracts import MAX_SCENARIOS, SupervisorContractError

_ALLOWED_ACTIONS = {"keep", "terminate", "subscribe"}
from .profile_fields import validate_overlay


def prepare_scenarios(args: dict[str, Any]) -> dict[str, Any]:
    """LLM이 제안한 분기를 결정론적으로 검사한다.

    사용자 금융정보 원본은 변경하지 않으며, 실제 상품 존재/자격/수익도 검증하지 않는다.
    """
    if not isinstance(args, dict):
        raise SupervisorContractError("시나리오 준비 입력은 dict이어야 합니다.")
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
        if not isinstance(item, dict):
            raise SupervisorContractError("시나리오는 dict이어야 합니다.")
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
        if not isinstance(assumptions, dict):
            raise SupervisorContractError("assumptions는 dict이어야 합니다.")
        horizon = assumptions.get("horizon_months")
        if horizon is not None and (type(horizon) is not int or horizon < 1 or horizon > 600):
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
        if not isinstance(actions, list):
            raise SupervisorContractError("holding_actions는 list이어야 합니다.")
        normalized_actions: list[dict[str, Any]] = []
        seen_holdings: set[str] = set()
        for a in actions:
            if not isinstance(a, dict) or not isinstance(a.get("action"), str) or a["action"] not in _ALLOWED_ACTIONS:
                raise SupervisorContractError("잘못된 보유상품 가상 행동입니다.")
            action = a["action"]
            ref = a.get("holding_ref")
            if a.get("status") not in (None, "hypothetical", "planned"):
                raise SupervisorContractError("가상 보유상품 행동을 실제 완료로 기록할 수 없습니다.")
            if action in {"keep", "terminate"}:
                if not isinstance(ref, str) or not ref.strip() or not baseline:
                    raise SupervisorContractError("실제 계약 유지·해지는 baseline_ref와 holding_ref가 필요합니다.")
                if ref in seen_holdings:
                    raise SupervisorContractError("동일 보유상품에 중복 또는 상충하는 행동이 있습니다.")
                seen_holdings.add(ref)
            normalized = deepcopy(a)
            if action == "subscribe":
                # 신규 납입 전략은 기존 holding_actions의 target_criteria에 담는다.
                # 사용자 실제 holdings, 소득, 생활비로 옮기지 않는다.
                criteria = a.get("target_criteria", {})
                if not isinstance(criteria, dict):
                    raise SupervisorContractError("subscribe.target_criteria는 dict이어야 합니다.")
                if ref is not None:
                    raise SupervisorContractError("가상 신규 가입에는 실제 holding_ref를 부여하지 않습니다.")
                if "monthly_contribution_krw" in criteria:
                    amount = criteria["monthly_contribution_krw"]
                    if type(amount) is not int or amount <= 0:
                        raise SupervisorContractError("가상 월 납입액은 양의 정수 원화 금액이어야 합니다.")
                if "term_months" in criteria:
                    term = criteria["term_months"]
                    if type(term) is not int or not 1 <= term <= 600:
                        raise SupervisorContractError("가상 상품 기간은 1~600개월이어야 합니다.")
                if "annual_rate_pct" in criteria:
                    rate = criteria["annual_rate_pct"]
                    if type(rate) not in (int, float) or not math.isfinite(rate):
                        raise SupervisorContractError("가상 금리는 유한한 숫자여야 합니다.")
                # 미래의 신규 가입을 실제 가입으로 표시하지 않는다.
                normalized["status"] = "hypothetical"
            normalized_actions.append(normalized)
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
