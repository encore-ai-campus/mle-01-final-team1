
from copy import deepcopy

from agent.profile.profile_agent import run_profile_agent


def test_tc01_income_extraction():
    result = run_profile_agent({
        "instruction": "세후 월급 300만 원으로 계산해 줘",
        "is_temporary": False,
    })

    assert result["profile"]["financial_profile"]["monthly_net_income_krw"] == 3000000
    assert result["changes"] == []
    assert result["missing_info"] == []


def test_tc02_income_save_candidate():
    result = run_profile_agent({
        "instruction": "월급 350만 원으로 저장해 줘",
        "is_temporary": False,
    })

    assert result["profile"] == {}
    assert len(result["changes"]) == 1

    change = result["changes"][0]
    assert change["change_id"].startswith("change_")
    assert change["domain"] == "financial"
    assert change["field"] == "monthly_net_income_krw"
    assert change["value"] == 3500000


def test_tc03_hypothetical_not_saved():
    result = run_profile_agent({
        "instruction": "내년 월급이 350만 원이라면?",
        "is_temporary": False,
    })

    assert result["profile"]["financial_profile"]["monthly_net_income_krw"] == 3500000
    assert result["changes"] == []


def test_tc04_missing_information():
    result = run_profile_agent({
        "instruction": "매달 얼마를 저축할 수 있어?",
        "is_temporary": False,
        "current_profile": {
            "financial_profile": {
                "monthly_net_income_krw": 3000000,
                "monthly_living_expenses_krw": None,
                "monthly_debt_payment_krw": None,
            }
        },
    })

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

    result = run_profile_agent({
        "instruction": "비상금 1,000만 원 목표도 추가해 줘",
        "is_temporary": False,
        "current_profile": original,
    })

    assert original == before  # 원본 프로필 변경 금지
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
