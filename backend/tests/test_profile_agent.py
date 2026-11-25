
from copy import deepcopy

from agent.profile.profile_agent import run_profile_agent


def run(instruction, **kwargs):
    return run_profile_agent({
        "instruction": instruction,
        "is_temporary": False,
        **kwargs,
    })


def test_tc01_consultation_income_extraction():
    result = run("세후 월급 300만 원으로 계산해 줘")

    assert result["changes"] == []
    assert result["extracted_info"][0]["amount_krw"] == 3000000
    assert result["extracted_info"][0]["target_field"] == (
        "financial_profile.monthly_net_income_krw"
    )


def test_tc02_ambiguous_income_not_saved():
    result = run("월급 350만 원으로 저장해 줘")

    assert result["profile"] == {}
    assert result["changes"] == []
    assert result["ambiguities"]
    assert result["extracted_info"][0]["amount_krw"] == 3500000
    assert result["extracted_info"][0]["tax_basis"] == "unknown"


def test_tc03_hypothetical_not_saved():
    result = run("세후 월급 350만 원이라면?")

    assert result["changes"] == []
    assert result["extracted_info"][0]["amount_krw"] == 3500000


def test_tc04_missing_information():
    result = run(
        "매달 얼마를 저축할 수 있어?",
        current_profile={
            "financial_profile": {
                "monthly_net_income_krw": 3000000,
                "monthly_living_expenses_krw": None,
                "monthly_debt_payment_krw": None,
            }
        },
    )

    assert result["missing_info"] == [
        "monthly_living_expenses_krw",
        "monthly_debt_payment_krw",
    ]
    assert result["changes"] == []


def test_tc05_goal_addition_preserves_existing():
    original = {
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
    }
    before = deepcopy(original)

    result = run(
        "비상금 1,000만 원 목표도 추가해 줘",
        current_profile=original,
    )

    assert original == before
    assert result["missing_info"] == []
    assert len(result["changes"]) == 1

    change = result["changes"][0]
    assert change["domain"] == "basic"
    assert change["field"] == "goals"

    goals = change["value"]
    assert len(goals) == 2
    assert goals[0] == before["basic_profile"]["goals"][0]
    assert goals[1]["goal_type"] == "emergency_fund"
    assert goals[1]["target_amount_krw"] == 10000000
    assert goals[1]["goal_id"] is None


def test_tc06_net_income_save_candidate():
    result = run("세후 월급 300만 원 저장해 줘")

    assert len(result["changes"]) == 1

    change = result["changes"][0]
    assert change["change_id"].startswith("change_")
    assert change["domain"] == "financial"
    assert change["field"] == "monthly_net_income_krw"
    assert change["value"] == 3000000


def test_tc07_gross_annual_income_save_candidate():
    result = run("세전 연봉 4,500만 원 저장해 줘")

    assert len(result["changes"]) == 1

    change = result["changes"][0]
    assert change["field"] == "annual_gross_income_krw"
    assert change["value"] == 45000000


def test_tc08_gross_monthly_income_not_converted():
    result = run("세전 월급 350만 원 저장해 줘")

    assert result["changes"] == []
    assert result["extracted_info"][0]["amount_krw"] == 3500000
    assert result["extracted_info"][0]["target_field"] is None
    assert result["ambiguities"]
