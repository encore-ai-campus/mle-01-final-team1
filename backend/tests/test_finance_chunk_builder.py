import json
from pathlib import Path

import pytest

from services.finance_chunk_builder import build_finance_chunks


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_empty_inputs(path: Path) -> None:
    for filename in (
        "deposit_products.json",
        "deposit_rates.json",
        "savings_products.json",
        "savings_rates.json",
        "loan_products.json",
        "loan_rates.json",
        "youth_policy.json",
    ):
        if not (path / filename).exists():
            write_json(path / filename, {"records": []})


def test_builds_separate_product_and_rate_chunks_with_source_metadata(tmp_path):
    write_json(
        tmp_path / "deposit_products.json",
        {
            "records": [
                {
                    "product_id": "dep-1",
                    "institution_name": "테스트은행",
                    "product_name": "테스트예금",
                    "product_type": "deposit",
                    "disclosure_month": "202610",
                    "join_method": ["인터넷"],
                    "special_condition": "없음",
                    "additional_note": "최소가입금액 1만원",
                }
            ]
        },
    )
    write_json(
        tmp_path / "deposit_rates.json",
        {
            "records": [
                {
                    "product_id": "dep-1",
                    "institution_id": "bank-1",
                    "product_code": "D-1",
                    "disclosure_month": "202610",
                    "term_months": 12,
                    "rate_type_code": "S",
                    "rate_type": "단리",
                    "base_rate": 3.6,
                    "max_rate": 3.6,
                    "max_rate_status": "present",
                },
                {
                    "product_id": "dep-1",
                    "institution_id": "bank-1",
                    "product_code": "D-1",
                    "disclosure_month": "202610",
                    "term_months": 24,
                    "rate_type_code": "S",
                    "rate_type": "단리",
                    "base_rate": 3.7,
                    "max_rate": None,
                    "max_rate_status": "unknown",
                },
            ]
        },
    )

    write_empty_inputs(tmp_path)
    evidence, chunks = build_finance_chunks(tmp_path)

    rate_chunks = [row for row in chunks if row["chunk_type"] == "rate_option"]
    assert len(rate_chunks) == 2
    assert {row["metadata"]["term_months"] for row in rate_chunks} == {12, 24}
    assert all(row["source_file"] == "deposit_rates.json" for row in rate_chunks)
    assert all(row["source_path"].startswith("records[") for row in rate_chunks)
    assert any("12개월" in row["text"] and "3.6" in row["text"] for row in rate_chunks)
    assert any(row["metadata"]["max_rate_status"] == "unknown" for row in rate_chunks)
    twelve = next(row for row in rate_chunks if row["metadata"]["term_months"] == 12)
    assert twelve["metadata"]["disclosure_id"] == "dep-1:202610"
    assert twelve["metadata"]["rate_option_id"] == "dep-1:202610:S:12"
    assert len(evidence) == len(chunks)


def test_builds_policy_condition_benefit_and_application_chunks(tmp_path):
    write_json(
        tmp_path / "youth_policy.json",
        {
            "records": [
                {
                    "policy_id": "policy-1",
                    "policy_name": "청년 테스트 지원",
                    "policy_description": "청년을 지원합니다.",
                    "support_content": "1회 200만원 지원",
                    "min_age": 19,
                    "max_age": 34,
                    "min_age_status": "present",
                    "max_age_status": "present",
                    "min_income": None,
                    "min_income_status": "not_provided",
                    "region_code": ["11"],
                    "application_start_date": None,
                    "application_end_date": None,
                    "application_period_status": "unknown",
                    "application_method": "온라인 신청",
                    "submission_document": None,
                    "submission_document_status": "not_provided",
                }
            ]
        },
    )

    write_empty_inputs(tmp_path)
    _, chunks = build_finance_chunks(tmp_path)
    types = {row["chunk_type"] for row in chunks}

    assert {"policy_overview", "policy_eligibility", "policy_benefit", "policy_application"} <= types
    assert all(row["metadata"]["policy_id"] == "policy-1" for row in chunks)
    eligibility = next(row for row in chunks if row["chunk_type"] == "policy_eligibility")
    application = next(row for row in chunks if row["chunk_type"] == "policy_application")
    assert "not_provided" in eligibility["text"]
    assert "소득 제한 여부는 원천 데이터 해석이 필요하다" in eligibility["text"]
    assert eligibility["metadata"]["income_limit_interpretation"] == "unresolved"
    assert "unknown" in application["text"]


def test_rate_text_keeps_savings_option_dimensions_and_product_context(tmp_path):
    write_json(
        tmp_path / "savings_products.json",
        {
            "records": [
                {
                    "product_id": "sav-1",
                    "institution_name": "테스트은행",
                    "product_name": "테스트적금",
                    "disclosure_month": "202610",
                }
            ]
        },
    )
    write_json(
        tmp_path / "savings_rates.json",
        {
            "records": [
                {
                    "product_id": "sav-1",
                    "disclosure_month": "202610",
                    "rate_type_code": "S",
                    "rate_type": "단리",
                    "saving_type_code": "S",
                    "saving_type": "정액적립식",
                    "term_months": 12,
                    "base_rate": 2.4,
                    "max_rate": 3.8,
                },
                {
                    "product_id": "sav-1",
                    "disclosure_month": "202610",
                    "rate_type_code": "S",
                    "rate_type": "단리",
                    "saving_type_code": "F",
                    "saving_type": "자유적립식",
                    "term_months": 12,
                    "base_rate": 2.4,
                    "max_rate": 3.8,
                },
            ]
        },
    )

    write_empty_inputs(tmp_path)
    _, chunks = build_finance_chunks(tmp_path)
    rate_chunks = [row for row in chunks if row["chunk_type"] == "rate_option"]

    assert len(rate_chunks) == 2
    assert all("테스트적금" in row["text"] for row in rate_chunks)
    assert all("테스트은행" in row["text"] for row in rate_chunks)
    assert {row["metadata"]["saving_type_code"] for row in rate_chunks} == {"S", "F"}
    assert {row["metadata"]["rate_option_id"] for row in rate_chunks} == {
        "sav-1:202610:S:12:S",
        "sav-1:202610:S:12:F",
    }


def test_loan_without_term_uses_natural_missing_value_text(tmp_path):
    write_json(
        tmp_path / "loan_products.json",
        {
            "records": [
                {
                    "product_id": "loan-1",
                    "institution_name": "테스트은행",
                    "product_name": "테스트대출",
                }
            ]
        },
    )
    write_json(
        tmp_path / "loan_rates.json",
        {
            "records": [
                {
                    "product_id": "loan-1",
                    "disclosure_month": "202610",
                    "rate_type_code": "C",
                    "rate_type": "변동금리",
                    "repayment_type_code": "S",
                    "repayment_type": "만기일시상환방식",
                    "term_months": None,
                    "min_rate": 4.0,
                    "max_rate": 5.0,
                }
            ]
        },
    )

    write_empty_inputs(tmp_path)
    _, chunks = build_finance_chunks(tmp_path)
    text = next(row["text"] for row in chunks if row["chunk_type"] == "rate_option")

    assert "기간 정보 미제공" in text
    assert "미제공개월" not in text


def test_missing_required_dataset_file_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="deposit_products.json"):
        build_finance_chunks(tmp_path)


def test_can_select_records_by_stable_entity_ids(tmp_path):
    for filename, records in {
        "deposit_products.json": [
            {"product_id": "dep-keep", "product_name": "유지상품"},
            {"product_id": "dep-skip", "product_name": "제외상품"},
        ],
        "deposit_rates.json": [
            {
                "product_id": "dep-keep",
                "disclosure_month": "202610",
                "rate_type_code": "S",
                "term_months": 12,
                "base_rate": 3.0,
                "max_rate": 3.0,
            },
            {
                "product_id": "dep-skip",
                "disclosure_month": "202610",
                "rate_type_code": "S",
                "term_months": 12,
                "base_rate": 2.0,
                "max_rate": 2.0,
            },
        ],
        "savings_products.json": [],
        "savings_rates.json": [],
        "loan_products.json": [],
        "loan_rates.json": [],
        "youth_policy.json": [
            {"policy_id": "policy-keep", "policy_name": "유지정책"},
            {"policy_id": "policy-skip", "policy_name": "제외정책"},
        ],
    }.items():
        write_json(tmp_path / filename, {"records": records})

    _, chunks = build_finance_chunks(
        tmp_path,
        product_ids={"dep-keep"},
        policy_ids={"policy-keep"},
    )

    assert {row["metadata"].get("product_id") for row in chunks if row["dataset"] == "deposit"} == {
        "dep-keep"
    }
    assert {row["metadata"].get("policy_id") for row in chunks if row["dataset"] == "youth_policy"} == {
        "policy-keep"
    }


def test_ids_are_deterministic_and_sample_limit_is_per_dataset(tmp_path):
    for filename, records in {
        "deposit_products.json": [{"product_id": f"dep-{i}", "product_name": f"예금{i}"} for i in range(3)],
        "deposit_rates.json": [],
        "savings_products.json": [{"product_id": f"sav-{i}", "product_name": f"적금{i}"} for i in range(3)],
        "savings_rates.json": [],
        "loan_products.json": [{"product_id": f"loan-{i}", "product_name": f"대출{i}"} for i in range(3)],
        "loan_rates.json": [],
        "youth_policy.json": [{"policy_id": f"policy-{i}", "policy_name": f"정책{i}"} for i in range(3)],
    }.items():
        write_json(tmp_path / filename, {"records": records})

    first_evidence, first_chunks = build_finance_chunks(tmp_path, limit_per_dataset=1)
    second_evidence, second_chunks = build_finance_chunks(tmp_path, limit_per_dataset=1)

    assert [row["evidence_id"] for row in first_evidence] == [row["evidence_id"] for row in second_evidence]
    assert [row["chunk_id"] for row in first_chunks] == [row["chunk_id"] for row in second_chunks]
    assert len({row["dataset"] for row in first_chunks}) == 4
    assert len(first_chunks) == len(first_evidence)
    assert len({row["chunk_id"] for row in first_chunks}) == len(first_chunks)


def test_stable_v2_policy_ids_do_not_change_when_record_position_changes(tmp_path):
    for filename, records in {
        "deposit_products.json": [],
        "deposit_rates.json": [],
        "savings_products.json": [],
        "savings_rates.json": [],
        "loan_products.json": [],
        "loan_rates.json": [],
        "youth_policy.json": [
            {"policy_id": "policy-before", "policy_name": "앞 정책"},
            {"policy_id": "policy-target", "policy_name": "대상 정책"},
        ],
    }.items():
        write_json(tmp_path / filename, {"records": records})

    _, full_v2 = build_finance_chunks(tmp_path, id_version="stable-v2")
    _, sample_v2 = build_finance_chunks(
        tmp_path, policy_ids={"policy-target"}, id_version="stable-v2"
    )
    full_target = {
        row["chunk_type"]: row["chunk_id"]
        for row in full_v2
        if row["metadata"].get("policy_id") == "policy-target"
    }
    sample_target = {
        row["chunk_type"]: row["chunk_id"]
        for row in sample_v2
        if row["metadata"].get("policy_id") == "policy-target"
    }

    assert full_target == sample_target
    assert all(row["chunk_id_version"] == "stable-v2" for row in sample_v2)

    _, full_v1 = build_finance_chunks(tmp_path, id_version="v1")
    _, sample_v1 = build_finance_chunks(
        tmp_path, policy_ids={"policy-target"}, id_version="v1"
    )
    full_v1_target = {
        row["chunk_type"]: row["chunk_id"]
        for row in full_v1
        if row["metadata"].get("policy_id") == "policy-target"
    }
    sample_v1_target = {
        row["chunk_type"]: row["chunk_id"]
        for row in sample_v1
        if row["metadata"].get("policy_id") == "policy-target"
    }
    assert full_v1_target != sample_v1_target
