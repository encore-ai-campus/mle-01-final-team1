# 공통 프로필 스키마와 조회 필드를 검증한다.
# 목표 병합과 임시 View 구성은 원본 변경 없이 처리한다.
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime
from typing import Any
from ..contracts import SupervisorContractError

# Supervisor는 TypedDict 자체가 아니라 v3.4의 허용 필드만 검사한다.
# 공통 모델을 복제하거나 다른 Agent를 import하지 않는다. 정합성은 테스트로 확인한다.
FIELDS = {
    "basic_profile": frozenset({
        "birth_date",
        "region_sido",
        "region_sigungu",
        "employment_status",
        "has_regular_income",
        "household_size",
        "owns_home",
        "marital_status",
        "risk_tolerance",
        "goals",
        "snapshot_at",
        "version",
    }),
    "financial_profile": frozenset({
        "monthly_net_income_krw",
        "annual_gross_income_krw",
        "monthly_living_expenses_krw",
        "total_assets_krw",
        "available_assets_krw",
        "total_debt_krw",
        "monthly_debt_payment_krw",
        "holdings",
        "liabilities",
        "snapshot_at",
        "version",
    }),
}
RECORD_FIELDS = {
    "goals": frozenset({
        "goal_id",
        "goal_type",
        "target_amount_krw",
        "target_amount_type",
        "target_date",
        "target_date_precision",
        "priority",
        "status",
    }),
    "holdings": frozenset({
        "holding_id",
        "product_type",
        "product_ref",
        "balance_krw",
        "monthly_contribution_krw",
        "annual_rate_pct",
        "opened_at",
        "maturity_date",
        "status",
    }),
    "liabilities": frozenset({
        "liability_id",
        "liability_type",
        "balance_krw",
        "annual_rate_pct",
        "monthly_payment_krw",
        "repayment_method",
        "maturity_date",
        "status",
    }),
}
METADATA = {"snapshot_at", "version"}
READ_FIELDS = frozenset(f"{d}.{f}" for d, fs in FIELDS.items() for f in fs - METADATA)
REPRESENTATIVE = (FIELDS["basic_profile"] | FIELDS["financial_profile"]) - METADATA - {"holdings", "liabilities"}


def _fail(field: str, issue: str) -> None:
    raise SupervisorContractError(f"{field}: {issue}")


def as_date(value: Any) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    _fail("date", "ISO date required")


def add_months(value: date, months: int) -> date:
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    month += 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def target_period_months(as_of: date, target: date) -> int:
    months = max(0, (target.year-as_of.year)*12 + target.month-as_of.month)
    return max(0, months - (add_months(as_of, months) > target))


def _scalar(field: str, value: Any) -> None:
    if value is None:
        return
    if field.endswith("_krw") or field in {"version", "priority", "household_size", "age"}:
        if type(value) is not int or value < 0 or (field == "household_size" and value == 0) or (field == "age" and value > 120):
            _fail(field, "nonnegative integer required")
    elif field in {"owns_home", "has_regular_income"}:
        if type(value) is not bool:
            _fail(field, "bool required")
    elif field in {"birth_date", "opened_at", "maturity_date", "target_date"}:
        as_date(value)
    elif field == "snapshot_at":
        try:
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        except (TypeError, ValueError):
            _fail(field, "ISO datetime required")
        if dt.tzinfo is None:
            _fail(field, "timezone required")
    elif field == "annual_rate_pct":
        import math
        if type(value) not in (int, float) or not math.isfinite(value):
            _fail(field, "finite number required")
    elif not isinstance(value, str) or not value.strip():
        _fail(field, "nonempty string required")
    allowed = {"employment_status": {"employed", "self_employed", "student", "job_seeking", "unemployed", "other"},
               "target_date_precision": {"year", "month", "day"}}
    if field in allowed and value not in allowed[field]:
        _fail(field, "unknown value")


def _records(field: str, value: Any, *, partial_goals: bool = False) -> None:
    if value is None:
        return
    models = {"goals": ("goal_id", {"goal_type", "status"}, {"active", "paused", "completed"}),
              "holdings": ("holding_id", {"holding_id", "product_type", "status"}, {"active", "matured", "closed"}),
              "liabilities": ("liability_id", {"liability_id", "liability_type", "status"}, {"active", "paid_off"})}
    id_key, required, statuses = models[field]
    if not isinstance(value, list):
        _fail(field, "list or null required")
    seen = set()
    for row in value:
        if not isinstance(row, dict) or set(row) - RECORD_FIELDS[field]:
            _fail(field, "unknown record field")
        if not (partial_goals and field == "goals" and row.get(id_key)):
            if any(row.get(k) is None for k in required):
                _fail(field, "required record field missing")
        for k, v in row.items():
            _scalar(k, v)
        if "status" in row and row["status"] not in statuses:
            _fail(field, "invalid status")
        ref = row.get(id_key)
        if ref is not None:
            if ref in seen:
                _fail(field, "duplicate ID")
            seen.add(ref)
        if field == "goals" and (not partial_goals or not row.get(id_key) or {"target_date", "target_date_precision"} <= row.keys()):
            target, precision = row.get("target_date"), row.get("target_date_precision")
            if (target is None) != (precision is None):
                _fail(field, "target_date and precision must match")
            if target is not None:
                d = as_date(target)
                if precision == "year" and (d.month, d.day) != (12, 31):
                    _fail(field, "year precision requires December 31")
                if precision == "month" and d.day != monthrange(d.year, d.month)[1]:
                    _fail(field, "month precision requires month end")


def validate_profile(domain: str, profile: Any) -> None:
    if not isinstance(profile, dict) or set(profile) - FIELDS[domain]:
        _fail(domain, "unknown profile field (v3.4)")
    for k, v in profile.items():
        if k in {"goals", "holdings", "liabilities"}:
            _records(k, v)
        else:
            _scalar(k, v)


def validate_overlay(values: Any, *, supplied: bool = False) -> None:
    allowed = REPRESENTATIVE | ({"age"} if supplied else set())
    if not isinstance(values, dict) or set(values) - allowed:
        _fail("overrides" if not supplied else "supplied_values", "unknown or protected field (v3.4)")
    for k, v in values.items():
        if k == "goals":
            _records(k, v, partial_goals=True)
        else:
            _scalar(k, v)


def canonical_profile_path(field: Any) -> str:
    """MVP의 짧은 필드명을 공용 프로필 경로로 정규화한다."""
    if isinstance(field, str):
        if field in READ_FIELDS:
            return field
        matches = [path for path in READ_FIELDS if path.rsplit(".", 1)[1] == field]
        if len(matches) == 1:
            return matches[0]
    _fail("field", "use a known basic_profile/financial_profile path")


def validate_read_fields(fields: Any) -> list[str]:
    if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or f not in READ_FIELDS for f in fields):
        _fail("fields", "use allowed basic_profile/financial_profile paths; age is view only")
    return list(dict.fromkeys(fields))


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    # Legacy flat ProfileResult is accepted only for unambiguous canonical fields.
    if set(profile) <= FIELDS.keys():
        result = deepcopy(profile)
    else:
        result = {}
        for k, v in profile.items():
            domains = [d for d, fs in FIELDS.items() if k in fs]
            if len(domains) != 1:
                _fail(k, "use canonical nested profile")
            result.setdefault(domains[0], {})[k] = deepcopy(v)
    for domain, value in result.items():
        validate_profile(domain, value)
    return result


def merge_goals(base: Any, proposed: Any, *, replace: bool = False) -> Any:
    # Null means unknown; it is never interpreted as an empty confirmed list.
    if proposed is None:
        _fail("goals", "null cannot replace saved goals")
    if replace:
        _records("goals", proposed)
        return deepcopy(proposed)
    if not isinstance(base, list):
        _fail("goals", "load existing goals before merging")
    _records("goals", proposed, partial_goals=True)
    _records("goals", base)
    result = deepcopy(base)
    indices = {g["goal_id"]: i for i, g in enumerate(result) if g.get("goal_id")}
    for goal in proposed:
        ref = goal.get("goal_id")
        if ref:
            if ref not in indices:
                _fail("goal_id", "unknown existing goal; new temporary goal must use null")
            i = indices[ref]
            result[i].update(deepcopy(goal))
        elif goal not in result:
            result.append(deepcopy(goal))
    _records("goals", result)
    return result


def source_with_reads(state: dict[str, Any]) -> dict[str, Any]:
    # Dedicated, authenticated read port fills un-loaded keys only. It never saves DB data.
    source = deepcopy(state["source"])
    for obs in state.get("observations", []):
        result = obs.get("result", {})
        if obs.get("name") != "profile_agent" or not result.get("verified_read"):
            continue
        for domain, values in result["verified_fields"].items():
            target = source.setdefault(domain, {})
            for key, value in values.items():
                if key not in target:
                    target[key] = deepcopy(value)
    return source


def attach_read_snapshot(state: dict[str, Any], patch: dict[str, Any]) -> None:
    actual = source_with_reads(state)
    for domain in FIELDS:
        if domain in actual and actual[domain] != state["source"].get(domain):
            patch[domain] = actual[domain]


def profile_availability(source: dict[str, Any]) -> dict[str, str]:
    status = {}
    for domain, fields in FIELDS.items():
        profile = source.get(domain, {})
        for field in sorted(fields - METADATA):
            status[f"{domain}.{field}"] = ("not_loaded" if field not in profile else
                                          "unknown" if profile[field] is None else "available")
    return status


def select_fields(source: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    output = {}
    for path in validate_read_fields(fields):
        domain, key = path.split(".")
        if key in source.get(domain, {}):
            output.setdefault(domain, {})[key] = deepcopy(source[domain][key])
    return output


PROFILE_FIELD_SETS = {
    "general": ((), ()),
    "policy": (("birth_date", "age", "region_sido", "region_sigungu", "employment_status", "household_size", "owns_home", "marital_status", "goals"), ("annual_gross_income_krw",)),
    "product_savings": (("goals", "risk_tolerance"), ("available_assets_krw", "monthly_net_income_krw")),
    "product_loan": (("goals", "employment_status", "owns_home"), ("annual_gross_income_krw", "total_debt_krw", "monthly_debt_payment_krw")),
    "fund": (("goals", "has_regular_income", "risk_tolerance"), tuple(sorted(FIELDS["financial_profile"] - METADATA - {"holdings", "liabilities"}))),
}


def build_profile_view(source: dict[str, Any], task_type: str, *, scenario: dict[str, Any] | None = None, goal_view: dict[str, Any] | None = None, policy_as_of: date | None = None, include_holdings: bool = False, include_liabilities: bool = False) -> dict[str, Any]:
    """Pure dispatcher helper: merge only known values then project by task type."""
    fields = PROFILE_FIELD_SETS[task_type]
    if task_type == "general":
        return {"basic_profile": {}, "financial_profile": {}}
    # Detail lists are included only when the dispatcher explicitly needs them.
    fields = (fields[0], fields[1] + (("holdings",) if include_holdings else ()) + (("liabilities",) if include_liabilities else ()))
    supplied = (source.get("intent") or {}).get("supplied_values", {})
    overrides = (scenario or {}).get("overrides", {})
    validate_overlay(supplied, supplied=True)
    validate_overlay(overrides)
    overlay = {**supplied, **overrides}
    if "goals" in overlay and "goals" in fields[0] and (goal_view is None or "goals" not in goal_view):
        _fail("goals", "explicit merged goal_view required")
    overlay.pop("goals", None)
    view = {}
    for domain, allowed in zip(FIELDS, fields):
        merged = {**source.get(domain, {}), **{k: v for k, v in overlay.items() if k in allowed}}
        if domain == "basic_profile" and goal_view is not None and "goals" in goal_view:
            _records("goals", goal_view["goals"])
            merged["goals"] = goal_view["goals"]
        view[domain] = {k: deepcopy(merged[k]) for k in allowed if k in merged}
    if task_type == "policy" and policy_as_of is not None:
        basic = view["basic_profile"]
        birth = basic.get("birth_date")
        if birth is not None:
            born = as_date(birth)
            if born > policy_as_of:
                _fail("birth_date", "after policy reference date")
            age = policy_as_of.year - born.year - ((policy_as_of.month, policy_as_of.day) < (born.month, born.day))
            if basic.get("age") is not None and basic["age"] != age:
                _fail("age", "supplied age conflicts with birth_date at policy reference date")
            basic["age"] = age
    return view
