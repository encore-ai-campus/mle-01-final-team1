
import re
from copy import deepcopy
from typing import Any, NotRequired, TypedDict
from uuid import uuid4
from agent.profile.profile_validation import inspect_profile_fields
from agent.profile.income_extraction import extract_income_info
from agent.profile.profile_change_policy import build_income_changes


class ProfileRequest(TypedDict):
    instruction: str
    is_temporary: bool
    current_profile: NotRequired[dict[str, Any]]
    required_fields: NotRequired[list[str]]
    change_requested: NotRequired[bool]


class ProfileResult(TypedDict):
    profile: dict[str, Any]
    missing_info: list[str]
    changes: NotRequired[list[dict[str, Any]]]
    baseline_ref: NotRequired[str]
    field_status: NotRequired[dict[str, str]]
    extracted_info: NotRequired[list[dict[str, Any]]]
    ambiguities: NotRequired[list[dict[str, Any]]]


def extract_monthly_income(text: str) -> int | None:
    """월급이나 월소득을 만 원 단위 표현에서 추출한다."""
    pattern = (
        r"(?:세후\s*)?(?:월급|월\s*소득)"
        r"\s*(?:은|이|을|을\s*기준으로|이\s*되면)?"
        r"\s*(\d[\d,]*)\s*만\s*원"
    )
    match = re.search(pattern, text)
    if not match:
        return None
    return int(match.group(1).replace(",", "")) * 10000


def extract_emergency_goal(text: str) -> dict[str, Any] | None:
    """'비상금 1,000만 원 목표' 형태를 추출한다."""
    match = re.search(
        r"비상금\s*(\d[\d,]*)\s*만\s*원",
        text,
    )
    if not match:
        return None

    amount = int(match.group(1).replace(",", "")) * 10000

    return {
        "goal_id": None,
        "goal_type": "emergency_fund",
        "target_amount_krw": amount,
        "target_amount_type": "target_balance",
        "target_date": None,
        "target_date_precision": None,
        "priority": None,
        "status": "active",
    }


def find_missing_info(
    instruction: str,
    profile: dict[str, Any],
) -> list[str]:
    is_savings_question = bool(
        re.search(r"저축|모을\s*수|얼마.*남", instruction)
        and re.search(r"얼마|가능|수\s*있", instruction)
    )
    if not is_savings_question:
        return []

    required_fields = [
        "monthly_net_income_krw",
        "monthly_living_expenses_krw",
        "monthly_debt_payment_krw",
    ]
    financial = profile.get("financial_profile") or {}

    return [
        field
        for field in required_fields
        if financial.get(field) is None
    ]


def run_profile_agent(request: ProfileRequest) -> ProfileResult:
    instruction = request["instruction"]
    is_temporary = request["is_temporary"]

    current_profile = deepcopy(request.get("current_profile") or {})
    basic = current_profile.get("basic_profile") or {}
    financial = current_profile.get("financial_profile") or {}
    

    result: ProfileResult = {
        "profile": {},
        "missing_info": [],
        "changes": [],
    }

    hypothetical = is_temporary or bool(
        re.search(r"이라면|라면|된다면|되면|가정", instruction)
    )
    
    # Explicit instruction takes precedence over legacy text detection.
    change_requested = request.get("change_requested")

    if change_requested is not None and type(change_requested) is not bool:
        raise ValueError("change_requested must be a boolean")

    save_requested = (
        change_requested
        if change_requested is not None
        else bool(re.search(r"저장해|저장할|기록해|등록해|수정해", instruction))
    )


    # 1. Income extraction and normalization
    income_facts = extract_income_info(instruction)

    result["extracted_info"] = income_facts
    result["ambiguities"] = []

    for fact in income_facts:
        target_field = fact["target_field"]

        # No supported DB field: do not invent or convert values.
        if target_field is None:
            reason = (
                "tax_basis_unknown"
                if fact["tax_basis"] == "unknown"
                else "unsupported_income_field"
            )

            result["ambiguities"].append({
                "type": reason,
                "raw_text": fact["raw_text"],
                "amount_krw": fact["amount_krw"],
            })
            continue

        field = target_field.split(".", 1)[1]
        amount = fact["amount_krw"]

        # Hypothetical information must never become a saved change.
        if hypothetical:
            result["profile"].setdefault(
                "financial_profile", {}
            )[field] = amount


    # Generate proposals separately from income extraction.
    income_changes = build_income_changes(
        income_facts,
        change_requested=save_requested,
        is_temporary=hypothetical,
    )

    result["changes"].extend(income_changes)

    # 2. 비상금 목표 추가 처리
    new_goal = extract_emergency_goal(instruction)

    goal_add_requested = bool(
        re.search(r"추가|등록|저장", instruction)
    )

    if new_goal is not None and goal_add_requested:
        existing_goals = basic.get("goals")

        if existing_goals is None:
            # 기존 목록이 미확인인 경우 덮어쓰지 않는다.
            result["missing_info"].append("goals")
        else:
            updated_goals = deepcopy(existing_goals)

            # 단순 동일 목표 중복 추가 방지
            is_duplicate = any(
                goal.get("goal_type") == "emergency_fund"
                and goal.get("target_amount_krw")
                == new_goal["target_amount_krw"]
                for goal in updated_goals
            )

            if not is_duplicate:
                updated_goals.append(new_goal)

                if hypothetical:
                    result["profile"]["basic_profile"] = {
                        "goals": updated_goals
                    }
                else:
                    result["changes"].append({
                        "change_id": f"change_{uuid4().hex}",
                        "domain": "basic",
                        "field": "goals",
                        "value": updated_goals,
                    })

    # 3. 기존 누락 정보 검사
    effective_profile = {
        "basic_profile": basic,
        "financial_profile": financial,
    }

    for field in find_missing_info(instruction, effective_profile):
        if field not in result["missing_info"]:
            result["missing_info"].append(field)

    # 4. Supervisor가 지정한 프로필 필드 검증
    required_fields = request.get("required_fields")

    if required_fields is not None:
        validation = inspect_profile_fields(
            required_fields=required_fields,
            profile=effective_profile,
        )

        result["field_status"] = validation["field_status"]

        for field in validation["missing_info"]:
            if field not in result["missing_info"]:
                result["missing_info"].append(field)

    return result


if __name__ == "__main__":
    examples = [
        {
            "instruction": "세후 월급 300만 원으로 계산해 줘",
            "is_temporary": False,
        },
        {
            "instruction": "월급 350만 원으로 저장해 줘",
            "is_temporary": False,
        },
        {
            "instruction": "내년 월급이 350만 원이라면?",
            "is_temporary": False,
        },
        {
            "instruction": "매달 얼마를 저축할 수 있어?",
            "is_temporary": False,
            "current_profile": {
                "financial_profile": {
                    "monthly_net_income_krw": 3000000,
                    "monthly_living_expenses_krw": None,
                    "monthly_debt_payment_krw": None,
                }
            },
        },
        {
            "instruction": "비상금 1,000만 원 목표도 추가해 줘",
            "is_temporary": False,
            "current_profile": {
                "basic_profile": {
                    "goals": [
                        {
                            "goal_id": "goal_01",
                            "goal_type": "marriage",
                            "target_amount_krw": 50000000,
                            "status": "active",
                        }
                    ]
                }
            },
        },
    ]

    for i, example in enumerate(examples, start=1):
        result = run_profile_agent(example)
        print(f"\nTC-0{i}: {example['instruction']}")
        print(result)
