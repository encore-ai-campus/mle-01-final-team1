# Supervisor 내부 시나리오 준비 도구. 가상 행동을 정규화할 뿐 계산하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from typing import Any
import math

from ..contracts import MAX_SCENARIOS, SupervisorContractError

_ALLOWED_ACTIONS = {"keep", "terminate", "subscribe"}
from .profile_fields import validate_overlay


def prepare_scenarios(args: dict[str, Any]) -> dict[str, Any]:
    """LLM이 제안한 분기를 결정론적으로 검사한다.

    사용자 금융정보 원본은 변경하지 않으며, 실제 상품 존재/자격/수익도 검증하지 않는다.
    """
    items = args.get("scenarios")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SCENARIOS:
        raise SupervisorContractError("시나리오 개수는 1~3개여야 합니다.")
    baseline = args.get("baseline_ref")
    if baseline is not None and (not isinstance(baseline, str) or not baseline.strip()):
        raise SupervisorContractError("baseline_ref 형식이 잘못되었습니다.")
    output: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    horizons: list[int | None] = []
    for item in items:
        if not isinstance(item, dict):
            raise SupervisorContractError("시나리오는 dict이어야 합니다.")
        sid = item.get("id")
        label = item.get("label")
        if not isinstance(sid, str) or not sid.strip() or sid != sid.strip() or sid in used_ids:
            raise SupervisorContractError("시나리오 ID가 비거나 중복되었습니다.")
        if not isinstance(label, str) or not label.strip():
            raise SupervisorContractError("시나리오 표시명이 필요합니다.")
        used_ids.add(sid)
        overrides = item.get("overrides", {})
        assumptions = item.get("assumptions", {})
        validate_overlay(overrides)
        if not isinstance(assumptions, dict):
            raise SupervisorContractError("assumptions는 dict이어야 합니다.")
        horizon = assumptions.get("horizon_months")
        if horizon is not None and (type(horizon) is not int or horizon < 1 or horizon > 600):
            raise SupervisorContractError("분석 기간은 1~600개월이어야 합니다.")
        horizons.append(horizon)
        actions = assumptions.get("holding_actions", [])
        if not isinstance(actions, list):
            raise SupervisorContractError("holding_actions는 list이어야 합니다.")
        normalized_actions: list[dict[str, Any]] = []
        seen_holdings: set[str] = set()
        for a in actions:
            if not isinstance(a, dict) or a.get("action") not in _ALLOWED_ACTIONS:
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
            normalized = dict(a)
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
        normalized_assumptions = dict(assumptions)
        if actions:
            normalized_assumptions["holding_actions"] = normalized_actions
        output.append({
            "id": sid,
            "label": label,
            "overrides": dict(overrides),
            "assumptions": normalized_assumptions,
        })
    # 비교 분기의 평가 기간이 제각각이면 숫자의 단순 비교가 불가능하다.
    if len(output) > 1 and any(v is not None for v in horizons):
        if any(v is None for v in horizons) or len(set(horizons)) > 1:
            raise SupervisorContractError("비교 시나리오의 horizon_months는 동일해야 합니다.")
    state: dict[str, Any] = {"scenarios": output}
    if baseline:
        state["baseline_ref"] = baseline
    return {"scenario_state": state}
